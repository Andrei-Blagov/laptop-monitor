from __future__ import annotations

"""Atomic Thailand job queue: pending / processing / archive / failed."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config
from thailand.job_models import strip_secrets, utc_now_iso, validate_job

logger = logging.getLogger(__name__)


def jobs_root(base: Path | str | None = None) -> Path:
    if base is not None:
        return Path(base)
    return Path(config.THAILAND_JOBS_DIR)


def ensure_queue_dirs(root: Path | str | None = None) -> Path:
    r = jobs_root(root)
    for name in ("pending", "processing", "archive", "failed"):
        (r / name).mkdir(parents=True, exist_ok=True)
    return r


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = strip_secrets(data)
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _iter_jobs(directory: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if data is not None:
            yield path, data


def pending_count(root: Path | str | None = None) -> int:
    r = ensure_queue_dirs(root)
    return len(list((r / "pending").glob("*.json")))


def find_active_dedupe(
    dedupe_key: str, *, root: Path | str | None = None
) -> str | None:
    """Return job_id if pending/processing already has this dedupe_key."""
    r = ensure_queue_dirs(root)
    for sub in ("pending", "processing"):
        for _path, job in _iter_jobs(r / sub):
            if str(job.get("dedupe_key") or "") == dedupe_key:
                return str(job.get("job_id") or "")
    return None


def enqueue_job(
    job: dict[str, Any],
    *,
    root: Path | str | None = None,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """
    Atomically enqueue job into pending/.

    Returns {"ok": bool, "job_id": ..., "path": ..., "error": ...}.
    """
    r = ensure_queue_dirs(root)
    ok, reason = validate_job(job)
    if not ok:
        return {"ok": False, "error": reason, "job_id": job.get("job_id")}

    job_id = str(job["job_id"])
    dedupe_key = str(job.get("dedupe_key") or "")
    if not allow_duplicate and dedupe_key:
        existing = find_active_dedupe(dedupe_key, root=r)
        if existing:
            return {
                "ok": False,
                "error": "duplicate_dedupe",
                "job_id": existing,
                "duplicate": True,
            }

    if pending_count(r) >= int(config.THAILAND_JOB_MAX_PENDING):
        logger.warning(
            "thailand queue full pending>=%s", config.THAILAND_JOB_MAX_PENDING
        )
        return {"ok": False, "error": "queue_full", "job_id": job_id}

    job = dict(job)
    job["status"] = "pending"
    job.setdefault("attempt", 0)
    job.setdefault("claimed_at", None)
    dest = r / "pending" / f"{job_id}.json"
    if dest.exists():
        return {"ok": False, "error": "job_exists", "job_id": job_id}

    _atomic_write(dest, job)
    return {"ok": True, "job_id": job_id, "path": str(dest), "status": "pending"}


def claim_next_job(*, root: Path | str | None = None) -> dict[str, Any] | None:
    """Atomically claim oldest pending job via rename to processing/."""
    r = ensure_queue_dirs(root)
    pending = r / "pending"
    processing = r / "processing"
    for path in sorted(pending.glob("*.json")):
        job = _read_json(path)
        if job is None:
            continue
        job_id = str(job.get("job_id") or path.stem)
        dest = processing / f"{job_id}.json"
        try:
            os.replace(path, dest)
        except OSError:
            continue
        claimed = _read_json(dest) or job
        claimed["status"] = "processing"
        claimed["claimed_at"] = utc_now_iso()
        claimed["attempt"] = int(claimed.get("attempt") or 0) + 1
        _atomic_write(dest, claimed)
        return claimed
    return None


def _prune_dir(directory: Path, keep: int) -> int:
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    removed = 0
    while len(files) > keep:
        old = files.pop(0)
        try:
            old.unlink()
            removed += 1
        except OSError:
            break
    return removed


def archive_job(
    job: dict[str, Any],
    *,
    root: Path | str | None = None,
    failed: bool = False,
) -> Path:
    r = ensure_queue_dirs(root)
    job_id = str(job.get("job_id") or "unknown")
    sub = "failed" if failed else "archive"
    dest = r / sub / f"{job_id}.json"
    proc = r / "processing" / f"{job_id}.json"
    _atomic_write(dest, job)
    if proc.exists():
        try:
            proc.unlink()
        except OSError:
            pass
    if failed:
        _prune_dir(r / "failed", int(config.THAILAND_JOB_FAILED_RETENTION))
    else:
        _prune_dir(r / "archive", int(config.THAILAND_JOB_ARCHIVE_RETENTION))
    return dest


def recover_stale_processing(*, root: Path | str | None = None) -> list[str]:
    """
    Move stale processing jobs back to pending (or failed if max attempts).

    Returns list of job_ids recovered/failed.
    """
    r = ensure_queue_dirs(root)
    out: list[str] = []
    now = datetime.now(timezone.utc)
    threshold = float(config.THAILAND_JOB_STALE_PROCESSING_SECONDS)
    max_attempts = int(config.THAILAND_JOB_MAX_ATTEMPTS)

    for path, job in list(_iter_jobs(r / "processing")):
        claimed_raw = job.get("claimed_at") or job.get("created_at")
        try:
            claimed = datetime.fromisoformat(str(claimed_raw).replace("Z", "+00:00"))
            if claimed.tzinfo is None:
                claimed = claimed.replace(tzinfo=timezone.utc)
        except Exception:
            claimed = now
        age = (now - claimed).total_seconds()
        if age < threshold:
            continue
        job_id = str(job.get("job_id") or path.stem)
        attempt = int(job.get("attempt") or 0)
        if attempt >= max_attempts:
            job["status"] = "failed"
            job["finished_at"] = utc_now_iso()
            job["error_class"] = "stale_processing_max_attempts"
            job["error_summary"] = "stale processing exceeded max attempts"
            archive_job(job, root=r, failed=True)
            out.append(job_id)
            continue
        # Requeue
        job["status"] = "pending"
        job["claimed_at"] = None
        job["requeued_at"] = utc_now_iso()
        job["requeue_reason"] = "stale_processing"
        pending_path = r / "pending" / f"{job_id}.json"
        try:
            os.replace(path, pending_path)
            _atomic_write(pending_path, job)
            out.append(job_id)
        except OSError as exc:
            logger.warning("stale recover failed %s: %s", job_id, type(exc).__name__)
    return out


def write_worker_state(
    data: dict[str, Any], *, path: Path | str | None = None
) -> None:
    p = Path(path or config.THAILAND_WORKER_STATE_PATH)
    payload = strip_secrets(dict(data))
    payload["updated_at"] = utc_now_iso()
    _atomic_write(p, payload)


def read_worker_state(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path or config.THAILAND_WORKER_STATE_PATH)
    if not p.exists():
        return {}
    return _read_json(p) or {}
