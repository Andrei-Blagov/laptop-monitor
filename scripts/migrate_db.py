from __future__ import annotations

"""Explicit schema-only DB migration (no collection / alerts / Telegram)."""

import argparse
import sqlite3
import sys
from pathlib import Path

import config
from storage import DEFAULT_DB_PATH, init_db, open_db
from version import APP_NAME, get_version

REQUIRED_TABLES = (
    "products",
    "price_history",
    "alert_events",
    "pipeline_runs",
    "store_runs",
)


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "missing"


def migrate_db(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, object]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(path) as conn:
        init_db(conn)
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        check = integrity_check(conn)
    return {
        "db_path": str(path.resolve()),
        "app_version": get_version(),
        "instance": config.get_instance_id(),
        "migration_ok": not missing and check.lower() == "ok",
        "missing_tables": missing,
        "integrity_check": check,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply laptop-monitor schema migrations only"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args(argv)
    result = migrate_db(args.db)
    print(f"{APP_NAME} {result['app_version']}")
    print(f"Instance: {result['instance']}")
    print(f"DB: {result['db_path']}")
    print(f"Migration: {'OK' if result['migration_ok'] else 'FAILED'}")
    if result["missing_tables"]:
        print(f"Missing tables: {', '.join(result['missing_tables'])}")
    print(f"integrity_check: {result['integrity_check']}")
    return 0 if result["migration_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
