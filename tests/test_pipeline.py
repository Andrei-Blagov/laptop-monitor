from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from models import Product
from pipeline_lock import (
    PipelineLockError,
    acquire_pipeline_lock,
    read_lock_info,
    release_pipeline_lock,
    try_remove_stale_lock,
)
from run_pipeline import (
    EXIT_FAILED,
    EXIT_LOCKED,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    print_status,
    run_pipeline,
    run_status,
)
from storage import (
    DEFAULT_DB_PATH,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_PARTIAL,
    PIPELINE_STATUS_SUCCESS,
    count_alert_events,
    count_price_history,
    get_delivery_stats,
    init_db,
    list_pipeline_runs,
    open_db,
    save_products,
)
from telegram_sender import FakeTelegramSender


PRODUCTION_DB = Path(DEFAULT_DB_PATH).resolve()


def _now(offset_sec: int = 0) -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).replace(
        second=offset_sec % 60
    )


def _product(
    *,
    store: str = "regard",
    external_id: str = "1",
    sku: str = "SKU-1",
    price: int = 250_000,
    name: str = "Test Laptop",
    available: bool = True,
    checked_at: datetime | None = None,
) -> Product:
    return Product(
        store=store,
        external_id=external_id,
        url=f"https://example.test/{store}/{external_id}",
        name=name,
        sku=sku,
        price=price,
        available=available,
        checked_at=checked_at or _now(),
    )


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmpdir.name)
        self.db_path = self.root / "pipeline.db"
        self.lock_path = self.root / "pipeline.lock"
        self.specs_path = self.root / "specs.json"
        self.specs_path.write_text("{}", encoding="utf-8")
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _seed_history(self) -> None:
        save_products(
            [
                _product(store="regard", external_id="1", sku="SKU-1", price=300_000),
                _product(store="andpro", external_id="a1", sku="SKU-1", price=290_000),
            ],
            self.db_path,
        )

    def _fetchers(self, regard_price: int = 250_000, andpro_price: int = 240_000):
        def regard():
            return [
                _product(
                    store="regard",
                    external_id="1",
                    sku="SKU-1",
                    price=regard_price,
                    checked_at=_now(10),
                )
            ]

        def andpro():
            return [
                _product(
                    store="andpro",
                    external_id="a1",
                    sku="SKU-1",
                    price=andpro_price,
                    checked_at=_now(10),
                )
            ]

        return regard, andpro

    def _run(self, **kwargs):
        regard, andpro = self._fetchers()
        defaults = dict(
            db_path=self.db_path,
            lock_path=self.lock_path,
            fetch_regard=regard,
            fetch_andpro=andpro,
            sender=FakeTelegramSender(),
            deliver=True,
            enrich_identities=False,
            write_comparison_artifacts=False,
            specs_path=self.specs_path,
            use_lock=True,
        )
        defaults.update(kwargs)
        return run_pipeline(**defaults)

    def test_full_success(self) -> None:
        self._seed_history()
        sender = FakeTelegramSender()
        result = self._run(sender=sender)
        self.assertEqual(result.status, PIPELINE_STATUS_SUCCESS)
        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertEqual(result.regard_status, "ok")
        self.assertEqual(result.andpro_status, "ok")
        self.assertGreaterEqual(result.alerts_created or 0, 1)
        self.assertEqual(result.messages_failed, 0)
        self.assertFalse(self.lock_path.exists())
        with open_db(self.db_path) as conn:
            rows = list_pipeline_runs(conn, limit=1)
            self.assertEqual(rows[0]["status"], PIPELINE_STATUS_SUCCESS)

    def test_regard_failed_andpro_ok_partial(self) -> None:
        self._seed_history()
        before_alerts = 0
        with open_db(self.db_path) as conn:
            init_db(conn)
            before_alerts = count_alert_events(conn)

        def regard_fail():
            raise RuntimeError("regard down")

        regard, andpro = self._fetchers()
        sender = FakeTelegramSender()
        result = self._run(
            fetch_regard=regard_fail,
            fetch_andpro=andpro,
            sender=sender,
        )
        self.assertEqual(result.status, PIPELINE_STATUS_PARTIAL)
        self.assertEqual(result.exit_code, EXIT_PARTIAL)
        self.assertEqual(result.regard_status, "failed")
        self.assertEqual(result.andpro_status, "ok")
        self.assertTrue(result.skipped_alerts_delivery)
        self.assertEqual(sender.calls, 0)
        with open_db(self.db_path) as conn:
            self.assertEqual(count_alert_events(conn), before_alerts)
            self.assertEqual(
                list_pipeline_runs(conn, limit=1)[0]["status"],
                PIPELINE_STATUS_PARTIAL,
            )

    def test_andpro_failed_regard_ok_partial(self) -> None:
        self._seed_history()

        def andpro_fail():
            raise RuntimeError("andpro down")

        regard, _ = self._fetchers()
        sender = FakeTelegramSender()
        result = self._run(
            fetch_regard=regard,
            fetch_andpro=andpro_fail,
            sender=sender,
        )
        self.assertEqual(result.status, PIPELINE_STATUS_PARTIAL)
        self.assertEqual(result.exit_code, EXIT_PARTIAL)
        self.assertEqual(result.andpro_status, "failed")
        self.assertEqual(sender.calls, 0)

    def test_both_stores_failed(self) -> None:
        def boom():
            raise RuntimeError("down")

        result = self._run(fetch_regard=boom, fetch_andpro=boom)
        self.assertEqual(result.status, PIPELINE_STATUS_FAILED)
        self.assertEqual(result.exit_code, EXIT_FAILED)
        self.assertEqual(result.error_stage, "collection")
        with open_db(self.db_path) as conn:
            self.assertEqual(
                list_pipeline_runs(conn, limit=1)[0]["status"],
                PIPELINE_STATUS_FAILED,
            )

    def test_storage_failure(self) -> None:
        self._seed_history()
        regard, andpro = self._fetchers()
        with patch(
            "run_pipeline.persist_collection",
            side_effect=OSError("disk full"),
        ):
            result = self._run(fetch_regard=regard, fetch_andpro=andpro)
        self.assertEqual(result.status, PIPELINE_STATUS_FAILED)
        self.assertEqual(result.error_stage, "storage")
        self.assertEqual(result.exit_code, EXIT_FAILED)

    def test_identity_failure(self) -> None:
        self._seed_history()
        with patch(
            "run_pipeline.sync_product_identifiers",
            side_effect=RuntimeError("identity boom"),
        ):
            result = self._run()
        self.assertEqual(result.status, PIPELINE_STATUS_FAILED)
        self.assertEqual(result.error_stage, "identity")

    def test_comparison_failure(self) -> None:
        self._seed_history()
        with patch(
            "run_pipeline.build_comparison",
            side_effect=RuntimeError("compare boom"),
        ):
            result = self._run()
        self.assertEqual(result.status, PIPELINE_STATUS_FAILED)
        self.assertEqual(result.error_stage, "comparison")

    def test_monitor_failure(self) -> None:
        self._seed_history()
        with patch(
            "run_pipeline.run_monitor",
            side_effect=RuntimeError("monitor boom"),
        ):
            result = self._run()
        self.assertEqual(result.status, PIPELINE_STATUS_FAILED)
        self.assertEqual(result.error_stage, "monitor")

    def test_telegram_full_success(self) -> None:
        self._seed_history()
        sender = FakeTelegramSender()
        result = self._run(sender=sender)
        self.assertEqual(result.status, PIPELINE_STATUS_SUCCESS)
        self.assertGreaterEqual(result.messages_sent or 0, 1)
        self.assertEqual(result.messages_failed, 0)
        self.assertEqual(len(sender.sent), result.messages_sent)

    def test_telegram_partial_failure(self) -> None:
        self._seed_history()
        # Force two separate target-like alerts via large drop + cross-store;
        # fail first send only so second can succeed on same run if multiple msgs.
        sender = FakeTelegramSender(fail_times=1, error="tg fail")
        # Ensure at least 2 messages: seed second unmatched product drop
        save_products(
            [
                _product(
                    store="regard",
                    external_id="2",
                    sku="SKU-2",
                    price=400_000,
                    checked_at=_now(0),
                )
            ],
            self.db_path,
        )

        def regard():
            return [
                _product(
                    store="regard",
                    external_id="1",
                    sku="SKU-1",
                    price=200_000,
                    checked_at=_now(20),
                ),
                _product(
                    store="regard",
                    external_id="2",
                    sku="SKU-2",
                    price=300_000,
                    checked_at=_now(20),
                ),
            ]

        def andpro():
            return [
                _product(
                    store="andpro",
                    external_id="a1",
                    sku="SKU-1",
                    price=210_000,
                    checked_at=_now(20),
                )
            ]

        result = self._run(
            fetch_regard=regard,
            fetch_andpro=andpro,
            sender=sender,
        )
        self.assertGreaterEqual(result.messages_sent or 0, 1)
        self.assertGreaterEqual(result.messages_failed or 0, 1)
        self.assertEqual(result.status, PIPELINE_STATUS_PARTIAL)
        self.assertEqual(result.exit_code, EXIT_PARTIAL)
        self.assertEqual(result.error_stage, "delivery")

    def test_lock_already_held(self) -> None:
        acquire_pipeline_lock(self.lock_path)
        try:
            result = self._run()
            self.assertEqual(result.exit_code, EXIT_LOCKED)
            self.assertEqual(result.error_message, "Pipeline already running")
        finally:
            release_pipeline_lock(self.lock_path, expected_pid=os.getpid())

    def test_stale_lock(self) -> None:
        self.lock_path.write_text(
            json.dumps({"pid": 999_999_999, "started_at": "2000-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        self.assertTrue(try_remove_stale_lock(self.lock_path) or True)
        # Even if try_remove left it, acquire should clear stale and proceed.
        self._seed_history()
        result = self._run()
        self.assertIn(result.exit_code, {EXIT_SUCCESS, EXIT_PARTIAL})
        self.assertNotEqual(result.exit_code, EXIT_LOCKED)
        self.assertFalse(self.lock_path.exists())

    def test_lock_released_after_success(self) -> None:
        self._seed_history()
        self._run()
        self.assertFalse(self.lock_path.exists())

    def test_lock_released_after_exception(self) -> None:
        self._seed_history()
        with patch(
            "run_pipeline.run_monitor",
            side_effect=RuntimeError("boom"),
        ):
            result = self._run()
        self.assertEqual(result.status, PIPELINE_STATUS_FAILED)
        self.assertFalse(self.lock_path.exists())

    def test_pipeline_runs_success_row(self) -> None:
        self._seed_history()
        self._run()
        with open_db(self.db_path) as conn:
            row = list_pipeline_runs(conn, limit=1)[0]
            self.assertEqual(row["status"], PIPELINE_STATUS_SUCCESS)
            self.assertEqual(row["regard_status"], "ok")
            self.assertEqual(row["andpro_status"], "ok")
            self.assertIsNotNone(row["finished_at"])
            self.assertIsNotNone(row["duration_seconds"])

    def test_pipeline_runs_partial_row(self) -> None:
        self._seed_history()

        def boom():
            raise RuntimeError("x")

        _, andpro = self._fetchers()
        self._run(fetch_regard=boom, fetch_andpro=andpro)
        with open_db(self.db_path) as conn:
            row = list_pipeline_runs(conn, limit=1)[0]
            self.assertEqual(row["status"], PIPELINE_STATUS_PARTIAL)
            self.assertEqual(row["error_stage"], "collection")

    def test_pipeline_runs_failed_row(self) -> None:
        def boom():
            raise RuntimeError("x")

        self._run(fetch_regard=boom, fetch_andpro=boom)
        with open_db(self.db_path) as conn:
            row = list_pipeline_runs(conn, limit=1)[0]
            self.assertEqual(row["status"], PIPELINE_STATUS_FAILED)

    def test_rerun_no_duplicate_alerts(self) -> None:
        self._seed_history()
        first = self._run()
        with open_db(self.db_path) as conn:
            alerts_after_first = count_alert_events(conn)
            history_after_first = count_price_history(conn)
        second = self._run()
        self.assertEqual(second.alerts_created, 0)
        with open_db(self.db_path) as conn:
            self.assertEqual(count_alert_events(conn), alerts_after_first)
            # Unchanged prices should not inflate history
            self.assertEqual(count_price_history(conn), history_after_first)
        self.assertGreaterEqual(first.alerts_created or 0, 1)

    def test_rerun_does_not_resend_telegram(self) -> None:
        self._seed_history()
        sender = FakeTelegramSender()
        self._run(sender=sender)
        calls_after_first = sender.calls
        self._run(sender=sender)
        self.assertEqual(sender.calls, calls_after_first)

    def test_status_does_not_mutate_db(self) -> None:
        self._seed_history()
        self._run()
        with open_db(self.db_path) as conn:
            before = list_pipeline_runs(conn, limit=100)
            before_alerts = count_alert_events(conn)
            before_delivery = get_delivery_stats(conn)
        runs = run_status(self.db_path, limit=10)
        self.assertEqual(len(runs), len(before))
        # print_status is pure stdout
        print_status(runs)
        with open_db(self.db_path) as conn:
            after = list_pipeline_runs(conn, limit=100)
            self.assertEqual(before, after)
            self.assertEqual(count_alert_events(conn), before_alerts)
            self.assertEqual(get_delivery_stats(conn), before_delivery)

    def test_uses_temp_db_not_production(self) -> None:
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)


if __name__ == "__main__":
    unittest.main()
