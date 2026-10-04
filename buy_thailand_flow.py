from __future__ import annotations

"""Orchestrate BUY signal + optional Thailand scan + Telegram messages (≤3)."""

import logging
from pathlib import Path
from typing import Any, Sequence

from buy_opportunity import (
    DEFAULT_STATE_PATH,
    BuySignal,
    load_state,
    record_signal,
    save_state,
    select_buy_signals,
)
from deal_ranking import RankedDeal
from russian_deals import load_russian_ranked_deals
from telegram_sender import MessageSender
from thailand.formatting import (
    format_buy_opportunity_message,
    format_thailand_alternatives_message,
    format_thailand_comparison_message,
)
from thailand.scanner import run_thailand_scan

logger = logging.getLogger(__name__)


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
) -> dict[str, Any]:
    """
    Evaluate BUY opportunities and optionally scan Thailand.

    Thailand failures never raise; Russian deals path is read-only here.
    """
    out: dict[str, Any] = {
        "buy_signals": 0,
        "buy_actionable": 0,
        "thailand_scan_triggered": False,
        "thailand_scan_status": None,
        "messages": [],
        "messages_sent": 0,
        "messages_failed": 0,
        "snapshot_path": None,
    }
    try:
        deals = list(russian_deals) if russian_deals is not None else []
        if not deals and db_path is not None:
            deals = load_russian_ranked_deals(db_path)
        state = load_state(state_path)
        actionable, all_signals, top1_key = select_buy_signals(
            deals, state=state, manual=manual or force_thailand
        )
        out["buy_signals"] = len(all_signals)
        out["buy_actionable"] = len(actionable)

        trigger_scan = bool(actionable) or force_thailand or (
            manual and force_thailand
        )
        # Manual "compare now" / "check thailand" forces scan even without BUY.
        if force_thailand:
            trigger_scan = True
            if not actionable and all_signals:
                actionable = list(all_signals[:1])
            elif not actionable and deals:
                # Synthetic path: no BUY, still compare top deal context
                actionable = []

        messages: list[str] = []
        primary: BuySignal | None = actionable[0] if actionable else (
            all_signals[0] if all_signals and force_thailand else None
        )

        if primary is not None and (actionable or force_thailand):
            messages.append(format_buy_opportunity_message(primary))

        scan_result = None
        if trigger_scan:
            out["thailand_scan_triggered"] = True
            try:
                scan_result = run_thailand_scan(
                    trigger_type="manual" if manual or force_thailand else "buy_signal",
                    russian_deals=deals,
                    trigger_signals=[primary] if primary else None,
                    snapshot_dir=snapshot_dir,
                    write_snapshot=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Thailand scan wrapper failed: %s", type(exc).__name__)
                scan_result = {"status": "failed", "error": type(exc).__name__}
            out["thailand_scan_status"] = scan_result.get("status")
            out["snapshot_path"] = scan_result.get("snapshot_path")

            store_results = scan_result.get("_store_results") or []
            fx = scan_result.get("_fx")
            best_match = scan_result.get("_best_match")
            comparison = scan_result.get("_comparison")
            messages.append(
                format_thailand_comparison_message(
                    store_results=store_results,
                    fx=fx,
                    match=best_match,
                    comparison=comparison,
                    russian_signal=primary,
                )
            )
            top = scan_result.get("_top") or scan_result.get("thailand_top") or []
            unverified_n = len(scan_result.get("unverified_candidates") or [])
            alt = format_thailand_alternatives_message(
                top, unverified_count=unverified_n
            )
            if alt and scan_result.get("status") != "failed":
                messages.append(alt)

            # Persist state for actionable signals
            if primary is not None and (actionable or force_thailand):
                record_signal(
                    state,
                    primary,
                    thailand_scanned=True,
                    top1_key=top1_key,
                )
                try:
                    save_state(state, state_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("buy state save failed: %s", type(exc).__name__)
        elif primary is not None and actionable:
            # Signal exists but cooldown blocked Thai scan — still announce Russia BUY?
            # Spec: Thailand scan cooldown; Russian BUY for new non-deduped signal.
            # If deduped entirely, actionable empty. If somehow primary without scan:
            messages.append(format_buy_opportunity_message(primary))
            record_signal(
                state, primary, thailand_scanned=False, top1_key=top1_key
            )
            try:
                save_state(state, state_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("buy state save failed: %s", type(exc).__name__)

        # Cap at 3 messages
        messages = [m for m in messages if m][:3]
        out["messages"] = messages

        if deliver and sender is not None:
            for text in messages:
                try:
                    res = sender.send_message(text)
                    if getattr(res, "ok", False):
                        out["messages_sent"] += 1
                    else:
                        out["messages_failed"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("buy/thai telegram send failed: %s", type(exc).__name__)
                    out["messages_failed"] += 1
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("buy_thailand_flow failed: %s", type(exc).__name__)
        out["error"] = type(exc).__name__
        return out
