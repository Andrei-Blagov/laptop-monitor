from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from alerts import run_monitor
from collection import CollectionResult, collect_products, persist_collection
from compare import build_comparison
from deliver import deliver_alert_events
from identity_sync import sync_product_identifiers
from pipeline_lock import (
    DEFAULT_LOCK_PATH,
    PipelineLockError,
    pipeline_lock,
)
from storage import (
    DEFAULT_DB_PATH,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_PARTIAL,
    PIPELINE_STATUS_RUNNING,
    PIPELINE_STATUS_SUCCESS,
    create_pipeline_run,
    init_db,
    list_pipeline_runs,
    open_db,
    update_pipeline_run,
)
from telegram_sender import MessageSender


EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2
EXIT_LOCKED = 3


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error_text(exc: BaseException) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    # Defense in depth: never echo token-like substrings.
    lowered = text.lower()
    if "bot" in lowered and "token" in lowered:
        return f"{exc.__class__.__name__}: [redacted]"
    return text[:1000]


@dataclass
class PipelineResult:
    status: str
    exit_code: int
    regard_status: str = "skipped"
    regard_products: int | None = None
    andpro_status: str = "skipped"
    andpro_products: int | None = None
    identifiers_added: int | None = None
    matched_models: int | None = None
    alerts_created: int | None = None
    messages_sent: int | None = None
    messages_failed: int | None = None
    error_stage: str | None = None
    error_message: str | None = None
    duration_seconds: float = 0.0
    run_id: int | None = None
    skipped_alerts_delivery: bool = False


def run_pipeline(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    lock_path: Path | str = DEFAULT_LOCK_PATH,
    fetch_regard: Callable | None = None,
    fetch_andpro: Callable | None = None,
    sender: MessageSender | None = None,
    deliver: bool = True,
    write_comparison_artifacts: bool = False,
    enrich_identities: bool = False,
    specs_path: Path | str | None = None,
    last_run_path: Path | str | None = None,
    use_lock: bool = True,
) -> PipelineResult:
    """
    Полный production pipeline.

    enrich_identities=False по умолчанию в тестах/injectable path —
    для CLI выставляется True (как identity_sync.py).
    """
    started = time.perf_counter()
    started_at = _iso_now()
    result = PipelineResult(
        status=PIPELINE_STATUS_RUNNING,
        exit_code=EXIT_FAILED,
    )

    def finish(status: str, exit_code: int) -> PipelineResult:
        result.status = status
        result.exit_code = exit_code
        result.duration_seconds = round(time.perf_counter() - started, 3)
        return result

    lock_cm = pipeline_lock(lock_path) if use_lock else None
    try:
        if lock_cm is not None:
            lock_cm.__enter__()
    except PipelineLockError:
        result.status = PIPELINE_STATUS_FAILED
        result.error_stage = "lock"
        result.error_message = "Pipeline already running"
        result.duration_seconds = round(time.perf_counter() - started, 3)
        result.exit_code = EXIT_LOCKED
        return result

    run_id: int | None = None
    try:
        with open_db(db_path) as conn:
            init_db(conn)
            run_id = create_pipeline_run(
                conn, started_at=started_at, status=PIPELINE_STATUS_RUNNING
            )
            result.run_id = run_id

        # --- COLLECTION ---
        try:
            collection = collect_products(
                fetch_regard=fetch_regard,
                fetch_andpro=fetch_andpro,
            )
        except Exception as exc:
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_FAILED,
                error_stage="collection",
                error_message=_safe_error_text(exc),
                started=started,
            )
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

        result.regard_status = "ok" if collection.regard.ok else "failed"
        result.andpro_status = "ok" if collection.andpro.ok else "failed"
        result.regard_products = (
            collection.regard.count if collection.regard.ok else None
        )
        result.andpro_products = (
            collection.andpro.count if collection.andpro.ok else None
        )

        if collection.none_ok:
            msg = "Both Regard and ANDPRO collection failed"
            errors = []
            if collection.regard.error:
                errors.append(f"regard: {collection.regard.error}")
            if collection.andpro.error:
                errors.append(f"andpro: {collection.andpro.error}")
            if errors:
                msg = msg + " | " + "; ".join(errors)
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_FAILED,
                error_stage="collection",
                error_message=msg,
                started=started,
            )
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

        # Persist only successful stores (never empty failed store snapshot).
        try:
            persist_collection(
                collection,
                db_path,
                last_run_path=last_run_path,
            )
        except Exception as exc:
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_FAILED,
                error_stage="storage",
                error_message=_safe_error_text(exc),
                started=started,
            )
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

        if not collection.both_ok:
            failed = "Regard" if not collection.regard.ok else "ANDPRO"
            err = collection.regard.error or collection.andpro.error or "store failed"
            result.skipped_alerts_delivery = True
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_PARTIAL,
                error_stage="collection",
                error_message=(
                    f"{failed} collection failed; alerts/delivery skipped. {err}"
                ),
                started=started,
            )
            return finish(PIPELINE_STATUS_PARTIAL, EXIT_PARTIAL)

        # --- IDENTITY ---
        try:
            id_kwargs: dict[str, Any] = {"enrich": enrich_identities}
            if specs_path is not None:
                id_kwargs["specs_path"] = specs_path
            id_stats = sync_product_identifiers(db_path, **id_kwargs)
            result.identifiers_added = int(id_stats.get("inserted", 0))
        except Exception as exc:
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_FAILED,
                error_stage="identity",
                error_message=_safe_error_text(exc),
                started=started,
            )
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

        # --- COMPARISON ---
        try:
            comparison = build_comparison(
                db_path,
                write_artifacts=write_comparison_artifacts,
            )
            result.matched_models = int(comparison.stats.get("matched_models", 0))
        except Exception as exc:
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_FAILED,
                error_stage="comparison",
                error_message=_safe_error_text(exc),
                started=started,
            )
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

        # --- MONITOR / ALERTS ---
        try:
            mon_kwargs: dict[str, Any] = {}
            if specs_path is not None:
                mon_kwargs["specs_path"] = specs_path
            monitor = run_monitor(db_path, **mon_kwargs)
            result.alerts_created = int(monitor.total_new)
        except Exception as exc:
            _record_run(
                db_path,
                run_id,
                result,
                status=PIPELINE_STATUS_FAILED,
                error_stage="monitor",
                error_message=_safe_error_text(exc),
                started=started,
            )
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

        # --- TELEGRAM ---
        if deliver:
            try:
                delivery = deliver_alert_events(
                    db_path,
                    sender=sender,
                    dry_run=False,
                )
                stats = delivery["stats"]
                result.messages_sent = int(stats.get("sent", 0))
                result.messages_failed = int(stats.get("failed", 0))
            except Exception as exc:
                _record_run(
                    db_path,
                    run_id,
                    result,
                    status=PIPELINE_STATUS_FAILED,
                    error_stage="delivery",
                    error_message=_safe_error_text(exc),
                    started=started,
                )
                return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)

            if (result.messages_failed or 0) > 0:
                status = PIPELINE_STATUS_PARTIAL
                exit_code = EXIT_PARTIAL
                result.error_stage = "delivery"
                result.error_message = (
                    f"Telegram partial: sent={result.messages_sent} "
                    f"failed={result.messages_failed}"
                )
            else:
                status = PIPELINE_STATUS_SUCCESS
                exit_code = EXIT_SUCCESS
        else:
            result.messages_sent = 0
            result.messages_failed = 0
            status = PIPELINE_STATUS_SUCCESS
            exit_code = EXIT_SUCCESS

        _record_run(
            db_path,
            run_id,
            result,
            status=status,
            error_stage=result.error_stage,
            error_message=result.error_message,
            started=started,
        )
        return finish(status, exit_code)

    except Exception as exc:
        _record_run(
            db_path,
            run_id,
            result,
            status=PIPELINE_STATUS_FAILED,
            error_stage="pipeline",
            error_message=_safe_error_text(exc),
            started=started,
        )
        return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED)
    finally:
        if lock_cm is not None:
            try:
                lock_cm.__exit__(None, None, None)
            except Exception:
                pass


def _record_run(
    db_path: Path | str,
    run_id: int | None,
    result: PipelineResult,
    *,
    status: str,
    error_stage: str | None,
    error_message: str | None,
    started: float,
) -> None:
    if run_id is None:
        return
    duration = round(time.perf_counter() - started, 3)
    result.error_stage = error_stage
    result.error_message = error_message
    result.duration_seconds = duration
    try:
        with open_db(db_path) as conn:
            init_db(conn)
            update_pipeline_run(
                conn,
                run_id,
                finished_at=_iso_now(),
                status=status,
                regard_status=result.regard_status,
                regard_products=result.regard_products,
                andpro_status=result.andpro_status,
                andpro_products=result.andpro_products,
                identifiers_added=result.identifiers_added,
                matched_models=result.matched_models,
                alerts_created=result.alerts_created,
                messages_sent=result.messages_sent,
                messages_failed=result.messages_failed,
                error_stage=error_stage,
                error_message=error_message,
                duration_seconds=duration,
            )
    except Exception:
        # Не маскируем исходную ошибку pipeline из-за сбоя записи history.
        traceback.print_exc(file=sys.stderr)


def print_pipeline_summary(result: PipelineResult) -> None:
    label = {
        PIPELINE_STATUS_SUCCESS: "SUCCESS",
        PIPELINE_STATUS_PARTIAL: "PARTIAL",
        PIPELINE_STATUS_FAILED: "FAILED",
        PIPELINE_STATUS_RUNNING: "RUNNING",
    }.get(result.status, result.status.upper())
    print(f"Pipeline run: {label}")
    regard_n = (
        str(result.regard_products)
        if result.regard_status == "ok"
        else result.regard_status
    )
    andpro_n = (
        str(result.andpro_products)
        if result.andpro_status == "ok"
        else result.andpro_status
    )
    print(f"Regard: {regard_n} products" if result.regard_status == "ok" else f"Regard: {result.regard_status}")
    print(f"ANDPRO: {andpro_n} products" if result.andpro_status == "ok" else f"ANDPRO: {result.andpro_status}")
    if result.identifiers_added is not None:
        print(f"Identifiers: +{result.identifiers_added}")
    if result.matched_models is not None:
        print(f"Matched models: {result.matched_models}")
    if result.alerts_created is not None:
        print(f"New alerts: {result.alerts_created}")
    elif result.skipped_alerts_delivery:
        print("New alerts: skipped (incomplete collection)")
    if result.messages_sent is not None or result.messages_failed is not None:
        print(
            "Telegram messages sent: "
            f"{result.messages_sent or 0}"
            + (
                f" (failed: {result.messages_failed})"
                if result.messages_failed
                else ""
            )
        )
    elif result.skipped_alerts_delivery:
        print("Telegram: skipped (incomplete collection)")
    print(f"Duration: {result.duration_seconds}s")
    if result.error_stage:
        print(f"Error stage: {result.error_stage}")
    if result.error_message:
        print(f"Error: {result.error_message}")


def run_status(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    limit: int = 10,
) -> list[dict]:
    with open_db(db_path) as conn:
        init_db(conn)
        return list_pipeline_runs(conn, limit=limit)


def print_status(runs: list[dict]) -> None:
    if not runs:
        print("No pipeline runs yet.")
        return
    print("Recent pipeline runs:")
    for row in runs:
        rid = row.get("id")
        started = row.get("started_at")
        status = row.get("status")
        regard = row.get("regard_status")
        rp = row.get("regard_products")
        andpro = row.get("andpro_status")
        ap = row.get("andpro_products")
        alerts = row.get("alerts_created")
        sent = row.get("messages_sent")
        failed = row.get("messages_failed")
        dur = row.get("duration_seconds")
        err_stage = row.get("error_stage")
        regard_s = f"{regard}/{rp}" if regard else "-"
        andpro_s = f"{andpro}/{ap}" if andpro else "-"
        tg = f"{sent or 0}/{failed or 0}"
        line = (
            f"  #{rid} {started} [{status}] "
            f"Regard={regard_s} ANDPRO={andpro_s} "
            f"alerts={alerts if alerts is not None else '-'} "
            f"tg_sent/failed={tg} "
            f"dur={dur if dur is not None else '-'}s"
        )
        if err_stage:
            line += f" error_stage={err_stage}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Единый production pipeline laptop-monitor"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Показать последние pipeline runs (без запуска)",
    )
    args = parser.parse_args(argv)

    if args.status:
        runs = run_status(DEFAULT_DB_PATH)
        print_status(runs)
        return EXIT_SUCCESS

    result = run_pipeline(
        DEFAULT_DB_PATH,
        enrich_identities=True,
        write_comparison_artifacts=True,
        last_run_path=Path("data") / "last_run.json",
        deliver=True,
    )
    if result.exit_code == EXIT_LOCKED:
        print("Pipeline already running")
        return EXIT_LOCKED
    print_pipeline_summary(result)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
