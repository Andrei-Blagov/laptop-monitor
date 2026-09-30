from __future__ import annotations

import json
import logging
import os
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

import config
from control_bot import (
    format_pipeline_result_message,
    format_store_summary_lines,
    build_top_text,
)
from deal_ranking import rank_clusters
from comparison import Offer, ProductMatch
from models import Product
from run_pipeline import PipelineResult
from scripts.backup_db import backup_sqlite, main as backup_main
from scripts.build_deploy_bundle import (
    build_deploy_bundle,
    collect_bundle_paths,
)
from scripts.migrate_db import migrate_db, main as migrate_main
from store_freshness import get_fresh_store_slugs
from storage import (
    create_pipeline_run,
    init_db,
    insert_store_run,
    open_db,
    require_schema_ready,
    save_products,
    schema_is_ready,
)
from telegram_safe import redact_secrets, safe_exc_message
from version import get_version


ROOT = Path(__file__).resolve().parent.parent


class VersionSingleSourceTests(unittest.TestCase):
    def test_version_file_matches_get_version(self) -> None:
        self.assertEqual(
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            get_version(),
        )

    def test_deploy_files_have_no_hardcoded_semver(self) -> None:
        """Deploy configs must not embed a literal X.Y.Z version."""
        forbidden = []
        # Current release version must not appear as a hardcoded tag.
        ver = get_version()
        paths = [
            ROOT / "deploy" / "Dockerfile",
            ROOT / "deploy" / "docker-compose.yml",
            ROOT / "deploy" / "compose.sh",
            ROOT / "deploy" / "systemd" / "laptop-monitor.service",
            ROOT / "deploy" / "systemd" / "laptop-monitor-control.service",
            ROOT / "deploy" / "systemd" / "laptop-monitor-backup.service",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            # Allow comments that mention versioning conceptually, but not
            # image:tag or LABEL = "0.2.0" literals.
            if f'"{ver}"' in text or f":{ver}" in text or f"={ver}" in text:
                # compose.sh reading VERSION file is OK — it won't contain literal
                if path.name == "compose.sh":
                    continue
                forbidden.append(str(path.relative_to(ROOT)))
        self.assertEqual(forbidden, [], f"Hardcoded version in: {forbidden}")

    def test_dockerfile_uses_arg(self) -> None:
        text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG APP_VERSION", text)
        self.assertIn("${APP_VERSION}", text)
        self.assertNotIn('"0.2.0"', text)

    def test_compose_uses_env_version(self) -> None:
        text = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("LAPTOP_MONITOR_VERSION", text)
        self.assertNotIn(":0.2.0", text)


class MigrateDbTests(unittest.TestCase):
    def test_migrate_creates_schema_idempotent_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            # Minimal "old" DB with products + alert
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    sku TEXT,
                    name TEXT,
                    url TEXT,
                    price INTEGER,
                    available INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    last_checked_at TEXT,
                    UNIQUE(store, external_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO products (store, external_id, name, url, price, available) "
                "VALUES ('regard','1','n','u',100000,1)"
            )
            conn.commit()
            conn.close()

            r1 = migrate_db(db)
            self.assertTrue(r1["migration_ok"])
            self.assertEqual(r1["integrity_check"], "ok")
            r2 = migrate_db(db)
            self.assertTrue(r2["migration_ok"])

            with open_db(db) as conn:
                init_db(conn)
                n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                self.assertEqual(n, 1)
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("store_runs", tables)
                self.assertIn("pipeline_runs", tables)

    def test_migrate_cli_no_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with patch("scripts.migrate_db.config.get_telegram_credentials") as creds:
                code = migrate_main(["--db", str(db)])
                self.assertEqual(code, 0)
                creds.assert_not_called()

    def test_require_schema_ready_fails_before_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.db"
            ok, missing = schema_is_ready(db)
            self.assertFalse(ok)
            with self.assertRaises(RuntimeError) as ctx:
                require_schema_ready(db)
            self.assertIn("migration", str(ctx.exception).lower())


class FreshStoreTopTests(unittest.TestCase):
    def _run(
        self,
        store: str,
        status: str,
        minutes_ago: float,
        *,
        products: int | None = 10,
    ) -> dict:
        finished = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return {
            "store": store,
            "status": status,
            "finished_at": finished.isoformat(),
            "attempted_at": finished.isoformat(),
            "products_count": products,
        }

    def test_regard_fresh_andpro_stale_ttl(self) -> None:
        latest = {
            "regard": self._run("regard", "ok", 30),
            "andpro": self._run("andpro", "ok", 400),
        }
        fresh = get_fresh_store_slugs(latest, max_age_minutes=180)
        self.assertEqual(fresh, {"regard"})

    def test_latest_failed_makes_stale(self) -> None:
        latest = {
            "andpro": self._run("andpro", "failed", 5, products=None),
        }
        fresh = get_fresh_store_slugs(latest, max_age_minutes=180)
        self.assertEqual(fresh, set())

    def test_three_stores_one_stale_excluded_from_top(self) -> None:
        matches = [
            ProductMatch(
                normalized_sku="X",
                display_sku="X",
                name="Model X",
                offers=[
                    Offer(
                        store="regard",
                        external_id="1",
                        name="Model X",
                        sku="X",
                        price=250_000,
                        available=True,
                        url="r",
                    ),
                    Offer(
                        store="andpro",
                        external_id="2",
                        name="Model X",
                        sku="X",
                        price=200_000,  # cheaper but will be stale
                        available=True,
                        url="a",
                    ),
                    Offer(
                        store="dns",
                        external_id="3",
                        name="Model X",
                        sku="X",
                        price=240_000,
                        available=True,
                        url="d",
                    ),
                ],
            )
        ]
        deals = rank_clusters(matches, fresh_stores={"regard", "dns"})
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].store, "dns")
        self.assertEqual(deals[0].price, 240_000)
        self.assertEqual(deals[0].saving_vs_next, 10_000)
        self.assertEqual(deals[0].next_store, "regard")

    def test_build_top_empty_when_no_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            save_products(
                [
                    Product(
                        store="regard",
                        external_id="1",
                        url="u",
                        name="n",
                        sku="S",
                        price=100_000,
                        available=True,
                        checked_at=datetime.now(timezone.utc),
                    )
                ],
                db,
            )
            with open_db(db) as conn:
                init_db(conn)
                rid = create_pipeline_run(
                    conn,
                    started_at=datetime.now(timezone.utc).isoformat(),
                    status="failed",
                )
                old = datetime.now(timezone.utc) - timedelta(minutes=5)
                insert_store_run(
                    conn,
                    pipeline_run_id=rid,
                    store="regard",
                    attempted_at=old.isoformat(),
                    finished_at=old.isoformat(),
                    status="failed",
                    error_message="boom",
                )
            text = build_top_text(db)
            self.assertIn("Нет свежих данных", text)


class DynamicStoreSummaryTests(unittest.TestCase):
    def test_format_pipeline_result_n_store(self) -> None:
        result = PipelineResult(
            status="partial",
            exit_code=2,
            store_statuses={
                "regard": {"status": "ok", "products_count": 24},
                "andpro": {"status": "ok", "products_count": 28},
            },
            alerts_created=2,
            messages_sent=1,
            messages_failed=0,
            duration_seconds=9.2,
        )
        text = format_pipeline_result_message(result)
        self.assertIn("Regard", text)
        self.assertIn("ANDPRO", text)
        self.assertIn("DNS", text)  # disabled listed
        self.assertIn("Citilink", text)
        self.assertNotRegex(text, r"(?m)^Regard:")  # not hardcoded single-line legacy alone only
        self.assertIn("OK (24)", text)

    def test_format_store_summary_lines_dynamic(self) -> None:
        lines = format_store_summary_lines(
            store_statuses={
                "regard": {"status": "ok", "products_count": 3},
                "andpro": {"status": "failed", "products_count": None},
            }
        )
        joined = "\n".join(lines)
        self.assertIn("Regard: OK (3)", joined)
        self.assertIn("ANDPRO: FAILED", joined)


class TelegramSafeTests(unittest.TestCase):
    def test_redact_token_in_url(self) -> None:
        token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        out = redact_secrets(url)
        self.assertNotIn(token, out)
        self.assertIn("[REDACTED]", out)

    def test_safe_exc_message_strips_token(self) -> None:
        token = "999888777:AASecretTokenValueHereXXXX"
        exc = httpx.RequestError(
            f"Request failed for https://api.telegram.org/bot{token}/getUpdates"
        )
        msg = safe_exc_message(exc)
        self.assertNotIn(token, msg)
        self.assertNotIn(f"bot{token}", msg)

    def test_logger_output_no_token(self) -> None:
        token = "111222333:AABBCCDDEEFFGGHHIIJJKKLLMM"
        exc = Exception(f"https://api.telegram.org/bot{token}/sendMessage boom")
        records: list[str] = []

        class H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        log = logging.getLogger("telegram_safe_test")
        log.handlers.clear()
        log.addHandler(H())
        log.setLevel(logging.ERROR)
        log.error("%s", safe_exc_message(exc))
        blob = "\n".join(records)
        self.assertNotIn(token, blob)


class BundleTests(unittest.TestCase):
    def test_bundle_allowlist_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            archive, digest = build_deploy_bundle(dist_dir=dist)
            self.assertTrue(archive.exists())
            self.assertEqual(len(digest), 64)
            names: list[str] = []
            with tarfile.open(archive, "r:gz") as tar:
                names = [m.name for m in tar.getmembers() if m.isfile()]
            joined = "\n".join(names)
            self.assertTrue(any(n.endswith("/VERSION") for n in names))
            self.assertTrue(any("scripts/migrate_db.py" in n for n in names))
            self.assertTrue(any("scripts/backup_db.py" in n for n in names))
            self.assertTrue(any("deploy/compose.sh" in n for n in names))
            self.assertTrue(any("ops/n8n_watchdog.py" in n for n in names))
            self.assertTrue(any("deploy/systemd/n8n-watchdog.timer" in n for n in names))
            self.assertFalse(any("/tests/" in n or n.endswith("/tests") for n in names))
            self.assertFalse(any("scripts/windows" in n for n in names))
            self.assertFalse(any(".git/" in n for n in names))
            self.assertFalse(any(".venv" in n for n in names))
            self.assertIn(get_version(), archive.name)

    def test_collect_paths_excludes_forbidden(self) -> None:
        paths = [p.relative_to(ROOT).as_posix() for p in collect_bundle_paths(ROOT)]
        self.assertTrue(any(p == "VERSION" for p in paths))
        self.assertFalse(any(p.startswith("tests/") for p in paths))
        self.assertFalse(any("windows" in p for p in paths))


class BackupModulePathTests(unittest.TestCase):
    def test_backup_main_module_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            out = Path(tmp) / "backups"
            save_products(
                [
                    Product(
                        store="regard",
                        external_id="1",
                        url="u",
                        name="n",
                        sku="S",
                        price=100_000,
                        available=True,
                        checked_at=datetime.now(timezone.utc),
                    )
                ],
                db,
            )
            code = backup_main(["--db", str(db), "--out-dir", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue(list(out.glob("laptop_monitor_*.db")))


if __name__ == "__main__":
    unittest.main()
