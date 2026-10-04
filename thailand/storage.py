from __future__ import annotations

"""Thailand scan snapshots (JSON files, not Russian SQLite)."""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

DEFAULT_SCAN_DIR = Path("data") / "thailand_scans"


def atomic_write_json(path: Path | str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=p.name + ".", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def snapshot_filename(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    return f"thailand_scan_{stamp}.json"


def write_scan_snapshot(
    payload: dict[str, Any],
    *,
    directory: Path | str = DEFAULT_SCAN_DIR,
    when: datetime | None = None,
) -> Path:
    directory = Path(directory)
    path = directory / snapshot_filename(when)
    # Never persist secrets
    safe = dict(payload)
    for key in list(safe.keys()):
        lk = key.lower()
        if any(s in lk for s in ("token", "secret", "password", "cookie", "authorization")):
            safe.pop(key, None)
    atomic_write_json(path, safe)
    try:
        prune_snapshots(directory, keep=int(config.THAILAND_SNAPSHOT_RETENTION))
    except Exception as exc:  # noqa: BLE001
        logger.warning("thailand snapshot prune failed: %s", type(exc).__name__)
    return path


def prune_snapshots(directory: Path | str, *, keep: int = 20) -> int:
    """Delete oldest snapshots beyond keep. Never raises on missing dir."""
    directory = Path(directory)
    if not directory.exists():
        return 0
    files = sorted(
        directory.glob("thailand_scan_*.json"),
        key=lambda p: p.name,
    )
    if len(files) <= keep:
        return 0
    removed = 0
    for old in files[: len(files) - keep]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            # Do not delete current on cleanup error — skip bad file.
            continue
    return removed
