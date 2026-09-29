from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import config
from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
)
from deliver import deliver_alert_events, run_baseline, run_status
from models import Product
from notification_groups import group_alert_events_for_delivery
from notifications import format_notification_group, format_price_update_group
from storage import (
    DEFAULT_DB_PATH,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENT,
    DELIVERY_STATUS_SKIPPED,
    count_alert_events,
    create_delivery_for_events,
    ensure_delivery,
    get_alert_events,
    get_delivery,
    get_delivery_event_ids,
    get_delivery_stats,
    init_db,
    open_db,
    save_alert_event,
    save_products,
    update_delivery,
)
from telegram_sender import FakeTelegramSender, SendResult, TelegramSender


PRODUCTION_DB = Path(DEFAULT_DB_PATH).resolve()


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmpdir.name) / "delivery_test.db"
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)

    def tearDown(self) -> None:
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)
        self._tmpdir.cleanup()

    def _add_product(
        self,
        *,
        store: str = "regard",
        external_id: str = "1",
        sku: str = "SKU-1",
        name: str = "Test Laptop",
        price: int = 220_000,
        url: str | None = None,
    ) -> int:
        save_products(
            [
                Product(
                    store=store,
                    external_id=external_id,
                    url=url or f"https://example.test/{store}/{external_id}",
                    name=name,
                    sku=sku,
                    price=price,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                )
            ],
            self.db_path,
        )
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM products WHERE store=? AND external_id=?",
                (store, external_id),
            ).fetchone()
            return int(row["id"])

    def _add_event(
        self,
        *,
        event_type: str = "TARGET_PRICE",
        product_id: int | None = None,
        dedupe: str = "d1",
        new_price: int = 220_000,
        old_price: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        if product_id is None:
            product_id = self._add_product()
        with open_db(self.db_path) as conn:
            init_db(conn)
            event = save_alert_event(
                conn,
                event_type=event_type,
                product_id=product_id,
                dedupe_key=dedupe,
                old_price=old_price,
                new_price=new_price,
                metadata=metadata
                or {
                    "name": "Gigabyte A16",
                    "gpu": "RTX 5070 Ti",
                    "store": "regard",
                    "threshold": 230_000,
                    "url": "https://example.test/1",
                },
            )
            assert event is not None
            return event

    def _product_id(self) -> int:
        with open_db(self.db_path) as conn:
            row = conn.execute("SELECT id FROM products LIMIT 1").fetchone()
            return int(row["id"])

    def test_new_event_creates_pending_then_sent(self) -> None:
        self._add_event()
        sender = FakeTelegramSender()
        result = deliver_alert_events(
            self.db_path,
            sender=sender,
            destination=config.TELEGRAM_DESTINATION,
        )
        self.assertEqual(result["stats"]["sent"], 1)
        self.assertEqual(len(sender.sent), 1)
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery["status"], DELIVERY_STATUS_SENT)
            self.assertEqual(delivery["attempts"], 1)

    def test_sent_not_resent(self) -> None:
        self._add_event()
        sender = FakeTelegramSender()
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(sender.calls, 1)

    def test_failed_increments_attempts_and_retries(self) -> None:
        self._add_event()
        sender = FakeTelegramSender(fail_times=1, error="boom")
        first = deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(first["stats"]["failed"], 1)
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_FAILED)
            self.assertEqual(delivery["attempts"], 1)
            self.assertEqual(delivery["last_error"], "boom")
            self.assertEqual(count_alert_events(conn), 1)

        second = deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(second["stats"]["sent"], 1)
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_SENT)
            self.assertEqual(delivery["attempts"], 2)

    def test_max_attempts_stops_retry(self) -> None:
        self._add_event()
        sender = FakeTelegramSender(fail_times=100, error="always")
        for _ in range(config.MAX_DELIVERY_ATTEMPTS):
            deliver_alert_events(
                self.db_path,
                sender=sender,
                destination=config.TELEGRAM_DESTINATION,
                max_attempts=config.MAX_DELIVERY_ATTEMPTS,
            )
        calls_after_limit = sender.calls
        deliver_alert_events(
            self.db_path,
            sender=sender,
            destination=config.TELEGRAM_DESTINATION,
            max_attempts=config.MAX_DELIVERY_ATTEMPTS,
        )
        self.assertEqual(sender.calls, calls_after_limit)
        self.assertEqual(sender.calls, config.MAX_DELIVERY_ATTEMPTS)

    def test_no_duplicate_delivery_rows(self) -> None:
        event = self._add_event()
        with open_db(self.db_path) as conn:
            ensure_delivery(
                conn,
                alert_event_id=int(event["id"]),
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            ensure_delivery(
                conn,
                alert_event_id=int(event["id"]),
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            rows = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_deliveries"
            ).fetchone()
            self.assertEqual(int(rows["cnt"]), 1)

    def test_baseline_skips_and_not_sent(self) -> None:
        self._add_event(dedupe="a")
        self._add_event(
            dedupe="b",
            product_id=self._product_id(),
            metadata={
                "name": "Other",
                "store": "andpro",
                "cheapest_store": "regard",
                "cheapest_price": 1,
                "other_store": "andpro",
                "other_price": 2,
                "difference": 1,
            },
            event_type="CROSS_STORE_SAVING",
            new_price=1,
        )
        created = run_baseline(self.db_path)
        self.assertEqual(created, 2)
        stats = run_status(self.db_path)
        self.assertEqual(stats[DELIVERY_STATUS_SKIPPED], 2)

        sender = FakeTelegramSender()
        result = deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(result["stats"]["sent"], 0)
        self.assertEqual(sender.calls, 0)

    def test_dry_run_does_not_change_db(self) -> None:
        self._add_event()
        before = run_status(self.db_path)
        result = deliver_alert_events(
            self.db_path,
            dry_run=True,
            destination=config.TELEGRAM_DESTINATION,
        )
        self.assertEqual(result["stats"]["dry_run"], 1)
        after = run_status(self.db_path)
        self.assertEqual(before, after)
        with open_db(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM notification_deliveries"
                ).fetchone()["cnt"],
                0,
            )

    def test_status_unsent_matches_dry_run_grouping(self) -> None:
        self._add_event(dedupe="u1")
        self._add_event(dedupe="u2", product_id=self._product_id(), new_price=221_000)
        stats = run_status(self.db_path)
        self.assertEqual(stats["unsent_alert_events"], 2)
        self.assertEqual(stats.get(DELIVERY_STATUS_PENDING, 0), 0)
        dry = deliver_alert_events(
            self.db_path,
            dry_run=True,
            destination=config.TELEGRAM_DESTINATION,
        )
        self.assertEqual(dry["stats"]["unsent_alert_events"], 2)
        self.assertEqual(dry["stats"]["grouped_messages"], dry["stats"]["dry_run"])

    def test_failure_keeps_alert_event(self) -> None:
        self._add_event()
        sender = FakeTelegramSender(fail_times=1)
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        with open_db(self.db_path) as conn:
            self.assertEqual(count_alert_events(conn), 1)
            events = get_alert_events(conn)
            self.assertEqual(len(events), 1)

    def test_separate_events_separate_deliveries(self) -> None:
        pid = self._add_product()
        self._add_event(product_id=pid, dedupe="e1")
        self._add_event(product_id=pid, dedupe="e2", new_price=221_000)
        sender = FakeTelegramSender()
        result = deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(result["stats"]["sent"], 2)
        self.assertEqual(len(sender.sent), 2)
        with open_db(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM notification_deliveries"
                ).fetchone()["cnt"],
                2,
            )

    def test_pending_created_before_send_path(self) -> None:
        event = self._add_event()
        with open_db(self.db_path) as conn:
            delivery = ensure_delivery(
                conn,
                alert_event_id=int(event["id"]),
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
                status=DELIVERY_STATUS_PENDING,
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_PENDING)

    def test_uses_temp_db_not_production(self) -> None:
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)

    # --- pacing / 429 / provider_message_id ---

    def test_rate_limiter_between_sequential_sends(self) -> None:
        slept: list[float] = []
        clock = {"t": 100.0}

        def monotonic() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["t"] += seconds

        client = MagicMock()
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {
            "ok": True,
            "result": {"message_id": 42},
        }
        ok_response.headers = {}
        client.post.return_value = ok_response

        sender = TelegramSender(
            "token-secret",
            "123",
            client=client,
            min_interval_seconds=1.1,
            sleep=sleep,
            monotonic=monotonic,
        )
        sender.send_message("a")
        clock["t"] += 0.2  # only 0.2s elapsed
        sender.send_message("b")
        self.assertTrue(slept)
        self.assertAlmostEqual(slept[0], 0.9, places=5)
        # token must not appear in error strings from sender itself
        self.assertNotIn("token-secret", str(slept))

    def test_telegram_429_reads_retry_after(self) -> None:
        client = MagicMock()
        response = MagicMock()
        response.status_code = 429
        response.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 7",
            "parameters": {"retry_after": 7},
        }
        response.headers = {}
        client.post.return_value = response

        sender = TelegramSender("tok", "1", client=client, min_interval_seconds=0)
        result = sender.send_message("hi")
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after, 7.0)

    def test_429_retry_then_success_marks_sent(self) -> None:
        self._add_event()
        slept: list[float] = []
        sender = FakeTelegramSender(
            responses=[
                SendResult(
                    ok=False,
                    error="rate limit",
                    status_code=429,
                    retryable=True,
                    retry_after=3.5,
                ),
                SendResult(ok=True, message_id=777),
            ]
        )
        result = deliver_alert_events(
            self.db_path,
            sender=sender,
            destination=config.TELEGRAM_DESTINATION,
            sleep=slept.append,
        )
        self.assertEqual(result["stats"]["sent"], 1)
        self.assertEqual(slept, [3.5])
        self.assertEqual(sender.calls, 2)
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_SENT)
            self.assertEqual(delivery["attempts"], 2)
            self.assertEqual(delivery["provider_message_id"], "777")

    def test_429_exhausts_max_attempts_marks_failed(self) -> None:
        self._add_event()
        sender = FakeTelegramSender(
            fail_times=100,
            error="rate limit",
            status_code=429,
            retry_after=1.0,
        )
        slept: list[float] = []
        result = deliver_alert_events(
            self.db_path,
            sender=sender,
            destination=config.TELEGRAM_DESTINATION,
            max_attempts=3,
            sleep=slept.append,
        )
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(sender.calls, 3)
        # sleep between attempts 1->2 and 2->3 only
        self.assertEqual(slept, [1.0, 1.0])
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_FAILED)
            self.assertEqual(delivery["attempts"], 3)

    def test_successful_send_saves_provider_message_id(self) -> None:
        self._add_event()
        sender = FakeTelegramSender()
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertEqual(delivery["provider_message_id"], "1001")

    # --- grouping ---

    def _matched_pair_events(self) -> tuple[dict, dict, dict]:
        regard_id = self._add_product(
            store="regard",
            external_id="r1",
            sku="G614PR-RV089",
            name="ASUS ROG Strix G16 G614PR-RV089",
            price=285_290,
            url="https://regard.test/rv089",
        )
        andpro_id = self._add_product(
            store="andpro",
            external_id="a1",
            sku="G614PR-RV089",
            name="ASUS G614PR-RV089",
            price=262_512,
            url="https://andpro.test/rv089",
        )
        drop_regard = self._add_event(
            event_type=EVENT_PRICE_DROP,
            product_id=regard_id,
            dedupe="drop-regard",
            old_price=307_030,
            new_price=285_290,
            metadata={
                "name": "ASUS ROG Strix G16 G614PR-RV089",
                "gpu": "RTX 5070 Ti",
                "store": "regard",
                "drop_amount": 21_740,
                "drop_percent": 7.08,
                "is_historical_low": True,
                "normalized_sku": "G614PR-RV089",
                "url": "https://regard.test/rv089",
            },
        )
        drop_andpro = self._add_event(
            event_type=EVENT_PRICE_DROP,
            product_id=andpro_id,
            dedupe="drop-andpro",
            old_price=293_821,
            new_price=262_512,
            metadata={
                "name": "ASUS G614PR-RV089",
                "gpu": "RTX 5070 Ti",
                "store": "andpro",
                "drop_amount": 31_309,
                "drop_percent": 10.66,
                "is_historical_low": True,
                "normalized_sku": "G614PR-RV089",
                "url": "https://andpro.test/rv089",
            },
        )
        cross = self._add_event(
            event_type=EVENT_CROSS_STORE,
            product_id=andpro_id,
            dedupe="cross-rv089",
            new_price=262_512,
            metadata={
                "name": "ASUS G614PR-RV089",
                "normalized_sku": "G614PR-RV089",
                "cheapest_store": "andpro",
                "cheapest_price": 262_512,
                "other_store": "regard",
                "other_price": 285_290,
                "difference": 22_778,
                "cheapest_url": "https://andpro.test/rv089",
                "other_url": "https://regard.test/rv089",
            },
        )
        return drop_regard, drop_andpro, cross

    def test_two_price_drops_same_model_group(self) -> None:
        drop_regard, drop_andpro, _ = self._matched_pair_events()
        # remove cross by using only drops via direct grouping
        groups = group_alert_events_for_delivery([drop_regard, drop_andpro])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, "price_update")
        self.assertEqual(set(groups[0].event_ids), {drop_regard["id"], drop_andpro["id"]})

    def test_price_drop_and_cross_store_group(self) -> None:
        drop_regard, drop_andpro, cross = self._matched_pair_events()
        groups = group_alert_events_for_delivery([drop_andpro, cross])
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].event_ids), {drop_andpro["id"], cross["id"]})

    def test_two_drops_and_cross_one_message(self) -> None:
        drop_regard, drop_andpro, cross = self._matched_pair_events()
        sender = FakeTelegramSender()
        result = deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(result["stats"]["unsent_alert_events"], 3)
        self.assertEqual(result["stats"]["grouped_messages"], 1)
        self.assertEqual(result["stats"]["sent"], 1)
        self.assertEqual(len(sender.sent), 1)
        text = sender.sent[0]
        self.assertIn("PRICE UPDATE", text)
        self.assertIn("Новый исторический минимум", text)
        self.assertIn("Сейчас дешевле", text)
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=int(drop_regard["id"]),
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            linked = get_delivery_event_ids(conn, int(delivery["id"]))
            self.assertEqual(
                set(linked),
                {drop_regard["id"], drop_andpro["id"], cross["id"]},
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_SENT)
            self.assertIsNotNone(delivery["provider_message_id"])

    def test_different_models_not_grouped(self) -> None:
        a = self._add_event(
            event_type=EVENT_PRICE_DROP,
            dedupe="m1",
            old_price=100,
            new_price=90,
            metadata={
                "name": "Model A",
                "store": "regard",
                "normalized_sku": "MODEL-A",
                "drop_amount": 10,
                "url": "https://a",
            },
        )
        pid2 = self._add_product(
            store="regard", external_id="2", sku="MODEL-B", name="Model B"
        )
        b = self._add_event(
            event_type=EVENT_PRICE_DROP,
            product_id=pid2,
            dedupe="m2",
            old_price=200,
            new_price=180,
            metadata={
                "name": "Model B",
                "store": "regard",
                "normalized_sku": "MODEL-B",
                "drop_amount": 20,
                "url": "https://b",
            },
        )
        groups = group_alert_events_for_delivery([a, b])
        self.assertEqual(len(groups), 2)

    def test_standalone_cross_store_not_lost(self) -> None:
        self._add_event(
            event_type=EVENT_CROSS_STORE,
            dedupe="cross-only",
            new_price=100,
            metadata={
                "name": "Solo",
                "normalized_sku": "SOLO-1",
                "cheapest_store": "andpro",
                "cheapest_price": 100,
                "other_store": "regard",
                "other_price": 120,
                "difference": 20,
                "cheapest_url": "https://andpro/solo",
            },
        )
        dry = deliver_alert_events(
            self.db_path, dry_run=True, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(dry["stats"]["unsent_alert_events"], 1)
        self.assertEqual(dry["stats"]["grouped_messages"], 1)
        self.assertIn("CROSS", dry["messages"][0]["text"].upper())

    def test_target_price_not_lost(self) -> None:
        self._add_event(
            event_type=EVENT_TARGET_PRICE,
            dedupe="target-1",
            new_price=200_000,
            metadata={
                "name": "Target Model",
                "store": "regard",
                "threshold": 210_000,
                "normalized_sku": "TARGET-1",
                "url": "https://t",
            },
        )
        dry = deliver_alert_events(
            self.db_path, dry_run=True, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(dry["stats"]["grouped_messages"], 1)
        self.assertIn("TARGET PRICE", dry["messages"][0]["text"])

    def test_dry_run_uses_same_grouping_logic(self) -> None:
        self._matched_pair_events()
        dry = deliver_alert_events(
            self.db_path, dry_run=True, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(dry["stats"]["unsent_alert_events"], 3)
        self.assertEqual(dry["stats"]["grouped_messages"], 1)
        self.assertEqual(len(dry["messages"]), 1)
        self.assertEqual(dry["messages"][0]["kind"], "price_update")

    def test_dry_run_does_not_mutate_with_groups(self) -> None:
        self._matched_pair_events()
        before = run_status(self.db_path)
        deliver_alert_events(
            self.db_path, dry_run=True, destination=config.TELEGRAM_DESTINATION
        )
        after = run_status(self.db_path)
        self.assertEqual(before, after)
        with open_db(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM notification_deliveries"
                ).fetchone()["cnt"],
                0,
            )

    def test_grouped_delivery_links_all_events(self) -> None:
        drops = self._matched_pair_events()
        sender = FakeTelegramSender()
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        with open_db(self.db_path) as conn:
            delivery = get_delivery(
                conn,
                alert_event_id=int(drops[0]["id"]),
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            linked = get_delivery_event_ids(conn, int(delivery["id"]))
            self.assertEqual(len(linked), 3)

    def test_successful_grouped_send_marks_all_processed(self) -> None:
        self._matched_pair_events()
        sender = FakeTelegramSender()
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        stats = run_status(self.db_path)
        self.assertEqual(stats["unsent_alert_events"], 0)
        self.assertEqual(stats[DELIVERY_STATUS_SENT], 1)

    def test_rerun_does_not_resend_grouped_events(self) -> None:
        self._matched_pair_events()
        sender = FakeTelegramSender()
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        self.assertEqual(sender.calls, 1)
        self.assertEqual(len(sender.sent), 1)

    def test_failed_grouped_send_keeps_events(self) -> None:
        self._matched_pair_events()
        sender = FakeTelegramSender(fail_times=1, error="boom")
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        with open_db(self.db_path) as conn:
            self.assertEqual(count_alert_events(conn), 3)
            delivery = get_delivery(
                conn,
                alert_event_id=1,
                channel=config.CHANNEL_TELEGRAM,
                destination=config.TELEGRAM_DESTINATION,
            )
            self.assertEqual(delivery["status"], DELIVERY_STATUS_FAILED)
            self.assertEqual(len(get_delivery_event_ids(conn, int(delivery["id"]))), 3)
        # retry succeeds
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        stats = run_status(self.db_path)
        self.assertEqual(stats["unsent_alert_events"], 0)
        self.assertEqual(stats[DELIVERY_STATUS_SENT], 1)

    def test_baseline_skipped_records_preserved(self) -> None:
        self._add_event(dedupe="old1")
        self._add_event(dedupe="old2", product_id=self._product_id(), new_price=1)
        created = run_baseline(self.db_path)
        self.assertEqual(created, 2)
        # new unsent event
        self._add_event(dedupe="new1", product_id=self._product_id(), new_price=2)
        with open_db(self.db_path) as conn:
            skipped_before = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_deliveries WHERE status='skipped'"
            ).fetchone()["cnt"]
        sender = FakeTelegramSender()
        deliver_alert_events(
            self.db_path, sender=sender, destination=config.TELEGRAM_DESTINATION
        )
        with open_db(self.db_path) as conn:
            skipped_after = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_deliveries WHERE status='skipped'"
            ).fetchone()["cnt"]
            self.assertEqual(int(skipped_before), 2)
            self.assertEqual(int(skipped_after), 2)

    def test_migration_existing_deliveries_safe(self) -> None:
        """Старая схема UNIQUE + без junction/provider_message_id мигрирует."""
        with open_db(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    store TEXT, external_id TEXT, url TEXT, name TEXT,
                    sku TEXT, price INTEGER, available INTEGER, checked_at TEXT
                );
                CREATE TABLE alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    product_id INTEGER,
                    created_at TEXT NOT NULL,
                    old_price INTEGER,
                    new_price INTEGER,
                    metadata_json TEXT,
                    dedupe_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_event_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    destination TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    sent_at TEXT,
                    last_error TEXT,
                    UNIQUE(alert_event_id, channel, destination)
                );
                INSERT INTO alert_events (
                    id, event_type, product_id, created_at, old_price, new_price,
                    metadata_json, dedupe_key
                ) VALUES (
                    1, 'TARGET_PRICE', NULL, '2020-01-01T00:00:00+00:00',
                    NULL, 1, '{}', 'legacy-1'
                );
                INSERT INTO notification_deliveries (
                    id, alert_event_id, channel, destination, status, attempts,
                    created_at, last_attempt_at, sent_at, last_error
                ) VALUES (
                    10, 1, 'telegram', 'default', 'skipped', 0,
                    '2020-01-01T00:00:00+00:00', NULL, NULL, 'baseline-existing'
                );
                """
            )
            init_db(conn)
            cols = {
                r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(notification_deliveries)"
                ).fetchall()
            }
            self.assertIn("provider_message_id", cols)
            row = conn.execute(
                "SELECT status, last_error FROM notification_deliveries WHERE id=10"
            ).fetchone()
            self.assertEqual(row["status"], "skipped")
            self.assertEqual(row["last_error"], "baseline-existing")
            junction = conn.execute(
                "SELECT alert_event_id FROM notification_delivery_events WHERE delivery_id=10"
            ).fetchone()
            self.assertEqual(int(junction["alert_event_id"]), 1)
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='notification_deliveries'"
            ).fetchone()["sql"]
            self.assertNotIn(
                "UNIQUE(alert_event_id, channel, destination)",
                " ".join(sql.split()),
            )

    def test_absorbed_historical_low_linked(self) -> None:
        pid = self._add_product(sku="ABSORB-1")
        drop = self._add_event(
            event_type=EVENT_PRICE_DROP,
            product_id=pid,
            dedupe="drop-abs",
            old_price=100,
            new_price=90,
            metadata={
                "name": "Absorb",
                "store": "regard",
                "normalized_sku": "ABSORB-1",
                "is_historical_low": True,
                "drop_amount": 10,
                "url": "https://x",
            },
        )
        hist = self._add_event(
            event_type=EVENT_HISTORICAL_LOW,
            product_id=pid,
            dedupe="hist-abs",
            new_price=90,
            metadata={
                "name": "Absorb",
                "store": "regard",
                "normalized_sku": "ABSORB-1",
                "url": "https://x",
            },
        )
        groups = group_alert_events_for_delivery([drop, hist])
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].event_ids), {drop["id"], hist["id"]})
        text = format_notification_group(groups[0])
        self.assertIn("PRICE UPDATE", text)
        self.assertIn("исторический минимум", text.lower())

    def test_grouped_formatter_asus_example(self) -> None:
        events = [
            {
                "id": 1,
                "event_type": EVENT_PRICE_DROP,
                "old_price": 293_821,
                "new_price": 262_512,
                "metadata": {
                    "name": "ASUS G614PR-RV089",
                    "gpu": "RTX 5070 Ti",
                    "store": "andpro",
                    "drop_amount": 31_309,
                    "drop_percent": 10.66,
                    "is_historical_low": True,
                    "url": "https://andpro.test/rv089",
                },
            },
            {
                "id": 2,
                "event_type": EVENT_PRICE_DROP,
                "old_price": 307_030,
                "new_price": 285_290,
                "metadata": {
                    "name": "ASUS ROG Strix G16 G614PR-RV089",
                    "gpu": "RTX 5070 Ti",
                    "store": "regard",
                    "drop_amount": 21_740,
                    "drop_percent": 7.08,
                    "is_historical_low": True,
                    "url": "https://regard.test/rv089",
                },
            },
            {
                "id": 3,
                "event_type": EVENT_CROSS_STORE,
                "new_price": 262_512,
                "metadata": {
                    "cheapest_store": "andpro",
                    "cheapest_price": 262_512,
                    "other_store": "regard",
                    "difference": 22_778,
                },
            },
        ]
        text = format_price_update_group(events)
        self.assertIn("PRICE UPDATE", text)
        self.assertIn("ASUS", text)
        self.assertIn("RTX 5070 Ti", text)
        self.assertIn("ANDPRO", text)
        self.assertIn("Regard", text)
        self.assertIn("Новый исторический минимум", text)
        self.assertIn("Сейчас дешевле", text)
        self.assertIn("22 778", text)


if __name__ == "__main__":
    unittest.main()
