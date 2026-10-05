from __future__ import annotations

"""BUY evaluation + Thailand job enqueue (pipeline) / job execution (worker)."""

import logging
from pathlib import Path
from typing import Any, Sequence

import config
from buy_opportunity import (
    DEFAULT_STATE_PATH,
    BuySignal,
    record_signal_enqueue_failed,
    record_signal_enqueued,
    record_thailand_job_completed,
    record_thailand_retry_result,
    select_buy_signals,
    select_thailand_retry,
    with_state,
)
from deal_ranking import RankedDeal
from russian_deals import load_russian_ranked_deals
from telegram_sender import MessageSender, TelegramSender
from thailand.formatting import (
    format_buy_opportunity_message,
    format_target_model_message,
    format_thailand_alternatives_message,
    format_thailand_comparison_message,
)
from thailand.job_models import (
    TRIGGER_BUY,
    TRIGGER_MANUAL_CHECK,
    TRIGGER_MANUAL_COMPARE,
    TRIGGER_MANUAL_MODEL,
    build_job,
    russian_context_to_deal,
    signal_from_dict,
)
from thailand.target_model import build_target_model
from thailand.job_queue import enqueue_job
from version import get_version

logger = logging.getLogger(__name__)


def _dedupe_key_for_signal(signal: BuySignal, trigger_type: str) -> str:
    return f"{trigger_type}|{signal.fingerprint}"


def _safe_enqueue(
    signal: BuySignal,
    *,
    deals: Sequence[RankedDeal],
    jobs_dir: Path | str | None,
    source_pipeline_run_id: int | None,
    instance_id: str | None,
) -> dict[str, Any]:
    """enqueue_job, with filesystem/permission errors reported as a failed result."""
    try:
        matched = next((d for d in deals if d.cluster_name == signal.cluster_name), None)
        chosen = matched if matched is not None else (deals[0] if deals else None)
        target = build_target_model(chosen, level=signal.level) if chosen is not None else None
        job = build_job(
            trigger_type=TRIGGER_BUY,
            signals=[signal],
            russian_deals=deals,
            dedupe_key=_dedupe_key_for_signal(signal, TRIGGER_BUY),
            source_pipeline_run_id=source_pipeline_run_id,
            app_version=get_version(),
            instance_id=instance_id or config.get_instance_id(),
            requested_chat_id=None,
            target_model=target,
        )
        enq = enqueue_job(job, root=jobs_dir)
        if enq.get("ok") and not enq.get("job_id"):
            enq["job_id"] = job["job_id"]
        return enq
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def _retry_thailand_enqueue(
    retry: tuple[BuySignal, str],
    *,
    deals: Sequence[RankedDeal],
    out: dict[str, Any],
    state_path: Path | str,
    jobs_dir: Path | str | None,
    source_pipeline_run_id: int | None,
    instance_id: str | None,
) -> None:
    """Retry only the Thailand enqueue for an already announced BUY. No Telegram."""
    signal, entry_key = retry
    enq = _safe_enqueue(
        signal,
        deals=deals,
        jobs_dir=jobs_dir,
        source_pipeline_run_id=source_pipeline_run_id,
        instance_id=instance_id,
    )
    queued = bool(enq.get("ok") or enq.get("duplicate"))
    job_id = str(enq.get("job_id") or "") if queued else None
    error = None if queued else str(enq.get("error") or "unknown")

    def _mut(st: dict[str, Any]) -> dict[str, Any]:
        return record_thailand_retry_result(
            st, entry_key=entry_key, queued=queued, job_id=job_id, error=error
        )

    try:
        state = with_state(_mut, state_path)
        status = (state.get("models") or {}).get(entry_key, {}).get(
            "last_thailand_enqueue_status"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("buy state retry save failed: %s", type(exc).__name__)
        status = "queued" if queued else "pending_retry"
    out["thailand_retry"] = {"model_key": entry_key, "status": status, "error": error}
    out["thailand_scan_triggered"] = queued
    out["thailand_scan_status"] = "queued" if queued else "enqueue_failed"
    out["thailand_job_id"] = job_id
    if not queued:
        logger.warning("thailand enqueue retry failed: %s status=%s", error, status)


def evaluate_buy_opportunity_flow(
    *,
    russian_deals: Sequence[RankedDeal] | None = None,
    db_path: Path | str | None = None,
    sender: MessageSender | None = None,
    deliver: bool = True,
    state_path: Path | str = DEFAULT_STATE_PATH,
    jobs_dir: Path | str | None = None,
    source_pipeline_run_id: int | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """
    Evaluate BUY / STRONG_BUY, send Russian BUY message, enqueue Thailand job.

    Never calls run_thailand_scan / JIB / FX.
    """
    out: dict[str, Any] = {
        "buy_signals": 0,
        "buy_actionable": 0,
        "thailand_scan_triggered": False,
        "thailand_scan_status": None,
        "thailand_job_id": None,
        "messages": [],
        "messages_sent": 0,
        "messages_failed": 0,
        "snapshot_path": None,
    }
    try:
        deals = list(russian_deals) if russian_deals is not None else []
        if not deals and db_path is not None:
            deals = load_russian_ranked_deals(db_path)

        # Lightweight state read for evaluation (short lock).
        from buy_opportunity import load_state, state_process_lock

        with state_process_lock(state_path):
            state = load_state(state_path)
            actionable, all_signals, top1_key = select_buy_signals(
                deals, state=state, manual=False
            )
        out["buy_signals"] = len(all_signals)
        out["buy_actionable"] = len(actionable)

        if not actionable:
            retry = select_thailand_retry(all_signals, state)
            if retry is not None:
                _retry_thailand_enqueue(
                    retry,
                    deals=deals,
                    out=out,
                    state_path=state_path,
                    jobs_dir=jobs_dir,
                    source_pipeline_run_id=source_pipeline_run_id,
                    instance_id=instance_id,
                )
            return out

        primary = actionable[0]
        job_id: str | None = None

        # Build + enqueue job BEFORE Telegram so message note is accurate.
        enq = _safe_enqueue(
            primary,
            deals=deals,
            jobs_dir=jobs_dir,
            source_pipeline_run_id=source_pipeline_run_id,
            instance_id=instance_id,
        )
        # A duplicate pending/processing job is success-equivalent.
        queued = bool(enq.get("ok") or enq.get("duplicate"))
        enqueue_failed = not queued
        if queued:
            job_id = str(enq.get("job_id") or "")
            out["thailand_scan_triggered"] = True
            out["thailand_scan_status"] = "queued"
            out["thailand_job_id"] = job_id

            def _mut(st: dict[str, Any]) -> dict[str, Any]:
                return record_signal_enqueued(
                    st, primary, job_id=job_id, top1_key=top1_key
                )

        else:
            error = str(enq.get("error") or "unknown")
            out["thailand_scan_triggered"] = False
            out["thailand_scan_status"] = "enqueue_failed"
            logger.warning("thailand enqueue failed: %s", error)

            # The BUY message still goes out once; only the enqueue is retried later.
            def _mut(st: dict[str, Any]) -> dict[str, Any]:
                return record_signal_enqueue_failed(
                    st, primary, error=error, top1_key=top1_key
                )

        try:
            with_state(_mut, state_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("buy state save failed: %s", type(exc).__name__)

        text = format_buy_opportunity_message(
            primary,
            thailand_queued=queued and not enqueue_failed,
            thailand_enqueue_failed=enqueue_failed,
        )
        out["messages"] = [text]
        if deliver:
            transport = sender
            owns = False
            if transport is None:
                try:
                    token, chat = config.get_telegram_credentials()
                    transport = TelegramSender(token, str(chat))
                    owns = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "buy telegram sender init failed: %s", type(exc).__name__
                    )
                    transport = None
            if transport is not None:
                try:
                    res = transport.send_message(text)
                    if getattr(res, "ok", False):
                        out["messages_sent"] += 1
                    else:
                        out["messages_failed"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("buy telegram send failed: %s", type(exc).__name__)
                    out["messages_failed"] += 1
                finally:
                    if owns:
                        try:
                            transport.close()
                        except Exception:
                            pass
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("evaluate_buy_opportunity_flow failed: %s", type(exc).__name__)
        out["error"] = type(exc).__name__
        return out


def enqueue_manual_thailand_job(
    *,
    compare: bool,
    chat_id: str | int,
    db_path: Path | str | None = None,
    russian_deals: Sequence[RankedDeal] | None = None,
    jobs_dir: Path | str | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """
    Create manual Thailand job without network scan / without BUY cooldown mutation.
    """
    out: dict[str, Any] = {
        "ok": False,
        "thailand_job_id": None,
        "thailand_scan_status": None,
        "trigger_type": TRIGGER_MANUAL_COMPARE if compare else TRIGGER_MANUAL_CHECK,
    }
    try:
        deals = list(russian_deals) if russian_deals is not None else []
        if compare and not deals and db_path is not None:
            deals = load_russian_ranked_deals(db_path)

        trigger = TRIGGER_MANUAL_COMPARE if compare else TRIGGER_MANUAL_CHECK
        # Manual dedupe uses chat + minute bucket to allow re-runs but avoid spam.
        from datetime import datetime, timezone

        bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        if deals:
            primary_key = f"{deals[0].store}:{deals[0].price}:{deals[0].cluster_name}"
        else:
            primary_key = "standalone"
        dedupe = f"{trigger}|{chat_id}|{primary_key}|{bucket}"

        signals: list[BuySignal] = []
        if compare and deals:
            # Optional signal context for formatting; cooldown bypassed via manual trigger.
            actionable, all_signals, _top1 = select_buy_signals(
                deals, state={}, manual=True
            )
            signals = actionable[:1] or all_signals[:1]

        job = build_job(
            trigger_type=trigger,
            signals=signals,
            russian_deals=deals if compare else deals[:5],
            dedupe_key=dedupe,
            source_pipeline_run_id=None,
            app_version=get_version(),
            instance_id=instance_id or config.get_instance_id(),
            requested_chat_id=chat_id,
        )
        enq = enqueue_job(job, root=jobs_dir)
        if enq.get("ok") or enq.get("duplicate"):
            out["ok"] = True
            out["thailand_job_id"] = enq.get("job_id") or job["job_id"]
            out["thailand_scan_status"] = "queued"
            out["thailand_scan_triggered"] = True
        else:
            out["thailand_scan_status"] = "enqueue_failed"
            out["error"] = enq.get("error")
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("enqueue_manual_thailand_job failed: %s", type(exc).__name__)
        out["error"] = type(exc).__name__
        out["thailand_scan_status"] = "enqueue_failed"
        return out


def enqueue_model_thailand_job(
    deal: RankedDeal,
    *,
    chat_id: str | int,
    jobs_dir: Path | str | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Manual Russian-TOP model search. Does not touch BUY state or cooldown."""
    target = build_target_model(deal)
    code = target.get("canonical_model_code") or deal.cluster_name
    job = build_job(
        trigger_type=TRIGGER_MANUAL_MODEL,
        signals=[],
        russian_deals=[deal],
        dedupe_key=f"{TRIGGER_MANUAL_MODEL}|{code}",
        app_version=get_version(),
        instance_id=instance_id or config.get_instance_id(),
        requested_chat_id=chat_id,
        target_model=target,
    )
    enq = enqueue_job(job, root=jobs_dir)
    return {
        "ok": bool(enq.get("ok") or enq.get("duplicate")),
        "thailand_job_id": enq.get("job_id") or job["job_id"],
        "thailand_scan_status": "queued" if enq.get("ok") or enq.get("duplicate") else "enqueue_failed",
        "trigger_type": TRIGGER_MANUAL_MODEL,
        "target_model": target,
        "error": enq.get("error"),
    }


def execute_thailand_job(
    job: dict[str, Any],
    *,
    sender: MessageSender | None = None,
    deliver: bool = True,
    state_path: Path | str = DEFAULT_STATE_PATH,
    snapshot_dir: Path | str | None = None,
    scan_fn=None,
) -> dict[str, Any]:
    """
    Worker-side: Thailand scan + comparison Telegram (no Russian BUY resend).
    """
    from thailand.scanner import run_thailand_scan

    scan = scan_fn or run_thailand_scan
    out: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "status": "failed",
        "messages": [],
        "messages_sent": 0,
        "messages_failed": 0,
        "snapshot_path": None,
        "thailand_scan_status": "failed",
    }
    trigger = str(job.get("trigger_type") or TRIGGER_BUY)
    signals = [signal_from_dict(s) for s in (job.get("signals") or []) if isinstance(s, dict)]
    deals = [
        russian_context_to_deal(c)
        for c in (job.get("russian_context") or [])
        if isinstance(c, dict)
    ]
    primary = signals[0] if signals else None

    try:
        target_model = job.get("target_model") if isinstance(job.get("target_model"), dict) else None
        scan_result = scan(
            trigger_type=trigger,
            russian_deals=deals,
            trigger_signals=[primary] if primary else None,
            snapshot_dir=snapshot_dir,
            write_snapshot=True,
            target_model=target_model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "thailand job scan failed: %s: %s", type(exc).__name__, exc
        )
        scan_result = {"status": "failed", "error": type(exc).__name__}

    status = str(scan_result.get("status") or "failed")
    out["thailand_scan_status"] = status
    out["status"] = status
    out["snapshot_path"] = scan_result.get("snapshot_path")
    out["stores"] = scan_result.get("stores")

    messages: list[str] = []
    target_search = scan_result.get("target_search")
    if target_model and isinstance(target_search, dict):
        messages.append(format_target_model_message(target_model, target_search))
    else:
        messages.append(
            format_thailand_comparison_message(
                store_results=scan_result.get("_store_results") or [],
                fx=scan_result.get("_fx"),
                match=scan_result.get("_best_match"),
                comparison=scan_result.get("_comparison"),
                russian_signal=primary,
                match_over_cap_note=scan_result.get("_match_over_cap_note")
                or (scan_result.get("matching") or {}).get("over_cap_note"),
            )
        )
    top = scan_result.get("_top") or scan_result.get("thailand_top") or []
    unverified_n = len(scan_result.get("unverified_candidates") or [])
    alt = format_thailand_alternatives_message(
        top,
        unverified_count=unverified_n,
        price_cap=scan_result.get("price_cap"),
        fx_usable=scan_result.get("fx_usable_for_verdict"),
    )
    if alt and status != "failed":
        messages.append(alt)
    out["messages"] = [m for m in messages if m][:2]

    if deliver and sender is not None:
        for text in out["messages"]:
            try:
                res = sender.send_message(text)
                if getattr(res, "ok", False):
                    out["messages_sent"] += 1
                else:
                    out["messages_failed"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("thailand job telegram failed: %s", type(exc).__name__)
                out["messages_failed"] += 1

    # Update buy state only for automatic BUY jobs (preserve manual cooldown isolation).
    if trigger == TRIGGER_BUY and primary is not None:
        job_status = (
            "success"
            if status == "ok"
            else ("partial" if status == "partial" else "failed")
        )

        def _mut(st: dict[str, Any]) -> dict[str, Any]:
            return record_thailand_job_completed(
                st,
                model_key=primary.model_key,
                job_id=str(job.get("job_id") or ""),
                job_status=job_status,
                scan_status=status,
                snapshot_path=out.get("snapshot_path"),
            )

        try:
            with_state(_mut, state_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("buy state completion save failed: %s", type(exc).__name__)

    return out


def run_buy_thailand_flow(
    *,
    russian_deals: Sequence[RankedDeal] | None = None,
    db_path: Path | str | None = None,
    sender: MessageSender | None = None,
    deliver: bool = True,
    manual: bool = False,
    force_thailand: bool = False,
    state_path: Path | str = DEFAULT_STATE_PATH,
    snapshot_dir: Path | str | None = None,
    jobs_dir: Path | str | None = None,
    source_pipeline_run_id: int | None = None,
) -> dict[str, Any]:
    """
    Compatibility wrapper.

    Automatic path: evaluate + enqueue only (async).
    Manual/force path: enqueue manual job only (async) — does not scan inline.
    """
    if manual or force_thailand:
        # Prefer compare semantics when deals available.
        chat = None
        if sender is not None and hasattr(sender, "_chat_id"):
            chat = getattr(sender, "_chat_id")
        result = enqueue_manual_thailand_job(
            compare=True,
            chat_id=chat or "0",
            db_path=db_path,
            russian_deals=russian_deals,
            jobs_dir=jobs_dir,
        )
        return {
            "buy_signals": 0,
            "buy_actionable": 0,
            "thailand_scan_triggered": bool(result.get("thailand_scan_triggered")),
            "thailand_scan_status": result.get("thailand_scan_status"),
            "thailand_job_id": result.get("thailand_job_id"),
            "messages": [],
            "messages_sent": 0,
            "messages_failed": 0,
            "snapshot_path": None,
            "ok": result.get("ok"),
            "error": result.get("error"),
        }

    return evaluate_buy_opportunity_flow(
        russian_deals=russian_deals,
        db_path=db_path,
        sender=sender,
        deliver=deliver,
        state_path=state_path,
        jobs_dir=jobs_dir,
        source_pipeline_run_id=source_pipeline_run_id,
    )
