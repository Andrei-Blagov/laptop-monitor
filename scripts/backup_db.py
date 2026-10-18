from __future__ import annotations

"""Full-state scheduled backup; legacy SQLite functions remain diagnostic APIs."""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from storage import DEFAULT_DB_PATH

DEFAULT_BACKUP_DIR = Path("data") / "backups"
RETENTION_DAYS = 30


def _iso_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_sqlite(
    db_path: Path | str = DEFAULT_DB_PATH,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
) -> Path:
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"DB not found: {src}")
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"laptop_monitor_{_iso_stamp()}.db"

    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # Integrity check on backup copy.
    check = sqlite3.connect(dest)
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            check.close()
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Backup integrity_check failed: {row}")
    finally:
        check.close()
    return dest


def prune_backups(
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    *,
    retention_days: int = RETENTION_DAYS,
) -> int:
    root = Path(backup_dir)
    if not root.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    removed = 0
    for path in root.glob("laptop_monitor_*.db"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def main(argv: list[str] | None = None) -> int:
    """Scheduled backups contain the entire state; old SQLite API is diagnostic only."""
    from ops.state_backup import create_backup, retain_daily
    parser = argparse.ArgumentParser(description="Private full-state backup from a read-only mount")
    parser.add_argument("--source", default="/source")
    parser.add_argument("--out-dir", default="/backup")
    args = parser.parse_args(argv)
    path = create_backup(args.source, args.out_dir)
    retained = retain_daily(args.out_dir, keep=7)
    print(f"Full backup integrity PASS: {path.name}; kept={retained['kept']}; removed={len(retained['removed'])}; unknown preserved={len(retained['skipped'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
