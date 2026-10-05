from __future__ import annotations

"""SQLite online backup + integrity check + retention."""

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
    parser = argparse.ArgumentParser(description="Backup laptop-monitor SQLite DB")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--out-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    args = parser.parse_args(argv)
    path = backup_sqlite(args.db, args.out_dir)
    removed = prune_backups(args.out_dir, retention_days=args.retention_days)
    print(f"Backup OK: {path}")
    print(f"Pruned: {removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
