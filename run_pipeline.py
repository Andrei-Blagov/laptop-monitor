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
from collection import (
    collect_products,
    persist_collection,
    write_collection_diagnostic,
)
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
    insert_store_run,
    open_db,
    read_pipeline_runs_readonly,
    update_pipeline_run,
)
from admin_notify import maybe_notify_ops_failure
from comparison import match_products
from deal_ranking import rank_clusters
from integrations.n8n import (
    build_pipeline_completed_payload,
    post_pipeline_webhook,
)
from store_freshness import get_fresh_store_slugs
from stores.registry import all_adapters
from storage import get_all_identifiers, get_all_products, get_latest_store_runs_readonly
from telegram_sender import MessageSender
from version import APP_NAME, get_version
import config


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
    store_statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    app_version: str | None = None
    instance_id: str | None = None

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

    def finish(status: str, exit_code: int, *, notify_n8n: bool = False) -> PipelineResult:
        result.status = status
        result.exit_code = exit_code
        result.duration_seconds = round(time.perf_counter() - started, 3)
        if notify_n8n:
            _maybe_notify_n8n(db_path, result, started_at=started_at)
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
                conn,
                started_at=started_at,
                status=PIPELINE_STATUS_RUNNING,
                app_version=get_version(),
                instance_id=config.get_instance_id(),
            )
            result.run_id = run_id

        # --- COLLECTION (independent per store) ---
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
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)

        result.regard_status = "ok" if collection.regard.ok else "failed"
        result.andpro_status = "ok" if collection.andpro.ok else "failed"
        result.regard_products = (
            collection.regard.count if collection.regard.ok else None
        )
        result.andpro_products = (
            collection.andpro.count if collection.andpro.ok else None
        )
        result.store_statuses = {
            slug: {
                "status": "ok" if store_res.ok else "failed",
                "products_count": store_res.count if store_res.ok else None,
                "error": store_res.error,
            }
            for slug, store_res in collection.stores.items()
        }
        result.app_version = get_version()
        result.instance_id = config.get_instance_id()
        fresh_stores = set(collection.successful_stores)
        collection_partial = bool(collection.failed_stores) and bool(
            collection.successful_stores
        )

        # Persist store_runs + successful store snapshots only.
        now_iso = _iso_now()
        try:
            with open_db(db_path) as conn:
                init_db(conn)
                for slug, store_res in collection.stores.items():
                    insert_store_run(
                        conn,
                        pipeline_run_id=run_id,
                        store=slug,
                        attempted_at=started_at,
                        finished_at=now_iso,
                        status="ok" if store_res.ok else "failed",
                        products_count=(
                            store_res.count if store_res.ok else None
                        ),
                        error_message=store_res.error,
                    )
            if collection.any_ok:
                persist_collection(
                    collection,
                    db_path,
                    last_run_path=last_run_path,
                    only_successful=True,
                )
            elif last_run_path is not None:
                write_collection_diagnostic(collection, last_run_path)
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
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)

        if collection.none_ok:
            msg = "All stores failed collection"
            errors = [
                f"{s}: {collection.stores[s].error}"
                for s in collection.failed_stores
                if collection.stores[s].error
            ]
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
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)

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
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)

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
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)

        # --- MONITOR / ALERTS ---
        try:
            mon_kwargs: dict[str, Any] = {
                "fresh_stores": fresh_stores,
                "product_store_filter": fresh_stores,
            }
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
            return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)

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
                # Alerts already created; keep them for retry → PARTIAL.
                _record_run(
                    db_path,
                    run_id,
                    result,
                    status=PIPELINE_STATUS_PARTIAL,
                    error_stage="delivery",
                    error_message=_safe_error_text(exc),
                    started=started,
                )
                return finish(PIPELINE_STATUS_PARTIAL, EXIT_PARTIAL, notify_n8n=True)

            if (result.messages_failed or 0) > 0:
                status = PIPELINE_STATUS_PARTIAL
                exit_code = EXIT_PARTIAL
                result.error_stage = "delivery"
                result.error_message = (
                    f"Telegram partial: sent={result.messages_sent} "
                    f"failed={result.messages_failed}"
                )
            elif collection_partial:
                status = PIPELINE_STATUS_PARTIAL
                exit_code = EXIT_PARTIAL
                result.error_stage = result.error_stage or "collection"
                result.error_message = result.error_message or (
                    "Partial collection: "
                    + ",".join(sorted(collection.failed_stores))
                    + " failed; local alerts for successful stores applied"
                )
            else:
                status = PIPELINE_STATUS_SUCCESS
                exit_code = EXIT_SUCCESS
        else:
            result.messages_sent = 0
            result.messages_failed = 0
            if collection_partial:
                status = PIPELINE_STATUS_PARTIAL
                exit_code = EXIT_PARTIAL
                result.error_stage = "collection"
                result.error_message = (
                    "Partial collection: "
                    + ",".join(sorted(collection.failed_stores))
                    + " failed"
                )
            else:
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
        return finish(status, exit_code, notify_n8n=True)

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
        return finish(PIPELINE_STATUS_FAILED, EXIT_FAILED, notify_n8n=True)
    finally:
        if lock_cm is not None:
            try:
                lock_cm.__exit__(None, None, None)
            except Exception:
                pass


def _maybe_notify_n8n(
    db_path: Path | str,
    result: PipelineResult,
    *,
    started_at: str | None,
) -> None:
    """Best-effort n8n webhook; never changes pipeline status."""
    try:
        top_payload: list[dict[str, Any]] = []
        try:
            latest = get_latest_store_runs_readonly(db_path)
            fresh = get_fresh_store_slugs(latest)
            if fresh:
                products = get_all_products(db_path)
                identifiers = get_all_identifiers(db_path)
                comparison = match_products(products, identifiers)
                deals = rank_clusters(
                    comparison.matches, fresh_stores=fresh, limit=10
                )
                top_payload = [
                    {
                        "name": d.cluster_name,
                        "store": d.store,
                        "price": d.price,
                        "score": d.score,
                        "url": d.url,
                    }
                    for d in deals
                ]
        except Exception:
            top_payload = []
        payload = build_pipeline_completed_payload(
            run_id=result.run_id,
            status=result.status,
            started_at=started_at,
            finished_at=_iso_now(),
            duration_seconds=result.duration_seconds,
            store_statuses=result.store_statuses,
            alerts_created=result.alerts_created,
            messages_sent=result.messages_sent,
            messages_failed=result.messages_failed,
            top_deals=top_payload,
            adapters_meta=[a.meta_dict() for a in all_adapters()],
            error_summary=result.error_message,
        )
        post_pipeline_webhook(payload)
    except Exception:
        # Absolute isolation from pipeline outcome.
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
    print(f"Version: {result.app_version or get_version()}")
    print(f"Instance: {result.instance_id or config.get_instance_id()}")
    if result.store_statuses:
        print("Stores:")
        for slug, info in sorted(result.store_statuses.items()):
            status = str(info.get("status") or "?")
            count = info.get("products_count")
            if status == "ok" and count is not None:
                print(f"  {slug}: OK ({count})")
            else:
                print(f"  {slug}: {status.upper()}")
    else:
        # Legacy fallback
        print(
            f"Regard: {result.regard_products} products"
            if result.regard_status == "ok"
            else f"Regard: {result.regard_status}"
        )
        print(
            f"ANDPRO: {result.andpro_products} products"
            if result.andpro_status == "ok"
            else f"ANDPRO: {result.andpro_status}"
        )
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
    """Read-only: never creates DB/schema or mutates data."""
    return read_pipeline_runs_readonly(db_path, limit=limit)


def print_status(runs: list[dict], db_path: Path | str = DEFAULT_DB_PATH) -> None:
    if not runs:
        print("No pipeline runs yet.")
        return
    print("Recent pipeline runs:")
    from storage import get_store_runs_for_pipeline, open_db_readonly

    for row in runs:
        rid = row.get("id")
        started = row.get("started_at")
        status = row.get("status")
        alerts = row.get("alerts_created")
        sent = row.get("messages_sent")
        failed = row.get("messages_failed")
        dur = row.get("duration_seconds")
        err_stage = row.get("error_stage")
        ver = row.get("app_version") or "-"
        tg = f"{sent or 0}/{failed or 0}"
        store_bits: list[str] = []
        if rid is not None:
            try:
                conn = open_db_readonly(db_path)
                try:
                    for sr in get_store_runs_for_pipeline(conn, int(rid)):
                        st = sr.get("status")
                        cnt = sr.get("products_count")
                        bit = f"{sr.get('store')}={st}"
                        if cnt is not None:
                            bit += f"/{cnt}"
                        store_bits.append(bit)
                finally:
                    conn.close()
            except Exception:
                store_bits = []
        if not store_bits:
            regard = row.get("regard_status")
            rp = row.get("regard_products")
            andpro = row.get("andpro_status")
            ap = row.get("andpro_products")
            store_bits = [
                f"regard={regard}/{rp}" if regard else "regard=-",
                f"andpro={andpro}/{ap}" if andpro else "andpro=-",
            ]
        line = (
            f"  #{rid} {started} [{status}] v={ver} "
            f"stores[{', '.join(store_bits)}] "
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
    parser.add_argument(
        "--version",
        action="store_true",
        help="Показать версию приложения",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {get_version()}")
        return EXIT_SUCCESS

    if args.status:
        runs = run_status(DEFAULT_DB_PATH)
        print_status(runs, DEFAULT_DB_PATH)
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
    # Ops alert only for scheduled/CLI runs — not unit tests.
    if result.status in {PIPELINE_STATUS_PARTIAL, PIPELINE_STATUS_FAILED}:
        maybe_notify_ops_failure(result)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
