from __future__ import annotations

"""Dedicated Thailand job worker CLI: python -m thailand.worker --once|--drain"""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import config
from buy_opportunity import DEFAULT_STATE_PATH
from buy_thailand_flow import execute_thailand_job
from thailand.job_models import utc_now_iso
from thailand.job_queue import (
    archive_job,
    claim_next_job,
    ensure_queue_dirs,
    pending_count,
    read_worker_state,
    recover_stale_processing,
    write_worker_state,
)

logger = logging.getLogger(__name__)


def _build_sender(job: dict[str, Any]):
    from telegram_sender import TelegramSender

    token, default_chat = config.get_telegram_credentials()
    chat = job.get("requested_chat_id") or default_chat
    if not token or not chat:
        return None
    return TelegramSender(token, str(chat))


def process_one(
    *,
    jobs_dir: Path | str | None = None,
    state_path: Path | str = DEFAULT_STATE_PATH,
    snapshot_dir: Path | str | None = None,
    deliver: bool = True,
    scan_fn=None,
) -> dict[str, Any] | None:
    ensure_queue_dirs(jobs_dir)
    recover_stale_processing(root=jobs_dir)
    job = claim_next_job(root=jobs_dir)
    if job is None:
        return None

    job_id = str(job.get("job_id") or "")
    started = time.perf_counter()
    started_at = utc_now_iso()
    logger.info(
        "thailand worker start job_id=%s trigger=%s",
        job_id,
        job.get("trigger_type"),
    )
    try:
        write_worker_state(
            {
                "last_job_id": job_id,
                "last_started_at": started_at,
                "last_status": "processing",
                "pending_count": pending_count(jobs_dir),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker state write failed: %s", type(exc).__name__)

    sender = None
    if deliver:
        try:
            sender = _build_sender(job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("thailand worker sender init failed: %s", type(exc).__name__)

    try:
        result = execute_thailand_job(
            job,
            sender=sender,
            deliver=deliver and sender is not None,
            state_path=state_path,
            snapshot_dir=snapshot_dir,
            scan_fn=scan_fn,
        )
        status = str(result.get("status") or "failed")
        finished_at = utc_now_iso()
        duration = round(time.perf_counter() - started, 3)
        archived = dict(job)
        archived.update(
            {
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "snapshot_path": result.get("snapshot_path"),
                "messages_sent": result.get("messages_sent"),
                "messages_failed": result.get("messages_failed"),
                "stores": result.get("stores"),
                "thailand_scan_status": result.get("thailand_scan_status"),
                "duration_seconds": duration,
            }
        )
        fatal = status == "failed" and not result.get("stores")
        path = archive_job(archived, root=jobs_dir, failed=fatal)
        try:
            write_worker_state(
                {
                    "last_job_id": job_id,
                    "last_started_at": started_at,
                    "last_finished_at": finished_at,
                    "last_status": status,
                    "last_duration_seconds": duration,
                    "last_archive_path": str(path),
                    "pending_count": pending_count(jobs_dir),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker state write failed: %s", type(exc).__name__)
        logger.info(
            "thailand worker finish job_id=%s status=%s duration=%.3fs snapshot=%s "
            "messages_sent=%s",
            job_id,
            status,
            duration,
            result.get("snapshot_path"),
            result.get("messages_sent"),
        )
        return archived
    except Exception as exc:  # noqa: BLE001
        finished_at = utc_now_iso()
        duration = round(time.perf_counter() - started, 3)
        failed = dict(job)
        failed.update(
            {
                "status": "failed",
                "started_at": started_at,
                "finished_at": finished_at,
                "error_class": type(exc).__name__,
                "error_summary": type(exc).__name__,
                "attempted_at": started_at,
                "duration_seconds": duration,
            }
        )
        archive_job(failed, root=jobs_dir, failed=True)
        try:
            write_worker_state(
                {
                    "last_job_id": job_id,
                    "last_started_at": started_at,
                    "last_finished_at": finished_at,
                    "last_status": "failed",
                    "last_duration_seconds": duration,
                    "pending_count": pending_count(jobs_dir),
                }
            )
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "thailand worker fatal job_id=%s error=%s", job_id, type(exc).__name__
        )
        return failed


def drain(
    *,
    jobs_dir: Path | str | None = None,
    state_path: Path | str = DEFAULT_STATE_PATH,
    snapshot_dir: Path | str | None = None,
    deliver: bool = True,
    scan_fn=None,
    max_jobs: int | None = None,
) -> int:
    """Process pending jobs sequentially until empty. Returns processed count."""
    ensure_queue_dirs(jobs_dir)
    recover_stale_processing(root=jobs_dir)
    n = 0
    while True:
        if max_jobs is not None and n >= max_jobs:
            break
        result = process_one(
            jobs_dir=jobs_dir,
            state_path=state_path,
            snapshot_dir=snapshot_dir,
            deliver=deliver,
            scan_fn=scan_fn,
        )
        if result is None:
            break
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thailand async job worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Process one pending job")
    mode.add_argument("--drain", action="store_true", help="Drain pending queue")
    parser.add_argument("--jobs-dir", default=None)
    parser.add_argument("--no-deliver", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    jobs_dir = args.jobs_dir
    deliver = not args.no_deliver
    if args.once:
        result = process_one(jobs_dir=jobs_dir, deliver=deliver)
        return 0 if result is not None or pending_count(jobs_dir) == 0 else 0
    processed = drain(jobs_dir=jobs_dir, deliver=deliver)
    logger.info("thailand worker drain processed=%s", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
