from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import config
from admin_notify import format_ops_message
from alerts import EVENT_CROSS_STORE, EVENT_PRICE_DROP, evaluate_cross_store
from comparison import Offer, ProductMatch, match_products
from control_bot import _is_admin, process_update
from deal_ranking import rank_clusters, score_offer
from models import Product
from notification_groups import group_alert_events_for_delivery
from product_identity import ProductIdentity
from run_pipeline import main as pipeline_main
from scripts.backup_db import backup_sqlite, prune_backups
from storage import (
    create_pipeline_run,
    get_latest_store_runs,
    init_db,
    insert_store_run,
    open_db,
    save_products,
)
from stores.base import StoreAdapter
from stores.registry import enabled_slugs, get_adapter
from version import get_version


def _p(store: str, eid: str, sku: str, price: int, pid: int | None = None) -> dict:
    return {
        "id": pid if pid is not None else abs(hash(f"{store}:{eid}")) % 10_000_000,
        "store": store,
        "external_id": eid,
        "name": f"Laptop {sku}",
        "sku": sku,
        "price": price,
        "available": True,
        "url": f"https://example/{store}/{eid}",
    }


def _identity(**kwargs: object) -> ProductIdentity:
    base = dict(
        store="regard",
        external_id="1",
        sku="SKU",
        name="Laptop",
        price=200_000,
        available=True,
        url="https://x",
    )
    base.update(kwargs)
    return ProductIdentity(**base)  # type: ignore[arg-type]


class VersionTests(unittest.TestCase):
    def test_version_file(self) -> None:
        self.assertEqual(get_version(), "0.2.0")

    def test_cli_version(self) -> None:
        code = pipeline_main(["--version"])
        self.assertEqual(code, 0)

    def test_pipeline_run_stores_app_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                rid = create_pipeline_run(
                    conn,
                    started_at=datetime.now(timezone.utc).isoformat(),
                    status="running",
                    app_version=get_version(),
                    instance_id="test",
                )
                row = conn.execute(
                    "SELECT app_version, instance_id FROM pipeline_runs WHERE id=?",
                    (rid,),
                ).fetchone()
                self.assertEqual(row["app_version"], "0.2.0")
                self.assertEqual(row["instance_id"], "test")


class StoreRegistryTests(unittest.TestCase):
    def test_enabled_core_stores(self) -> None:
        slugs = enabled_slugs()
        self.assertIn("regard", slugs)
        self.assertIn("andpro", slugs)
        self.assertNotIn("dns", slugs)
        self.assertNotIn("citilink", slugs)

    def test_dns_citilink_disabled(self) -> None:
        self.assertFalse(get_adapter("dns").enabled)
        self.assertFalse(get_adapter("citilink").enabled)

    def test_sanity_rejects_empty_catalog(self) -> None:
        class EmptyStore(StoreAdapter):
            slug = "empty"
            display_name = "Empty"
            enabled = True
            min_expected_products = 3

            def collect(self):
                return []

        outcome = EmptyStore().collect_safe()
        self.assertFalse(outcome.ok)
        self.assertIn("sanity", outcome.error or "")


class MatchingMultiStoreTests(unittest.TestCase):
    def test_three_stores_same_sku(self) -> None:
        products = [
            _p("regard", "1", "G614PR", 250_000, 1),
            _p("andpro", "2", "G614PR", 240_000, 2),
            _p("dns", "3", "G614PR", 235_000, 3),
        ]
        result = match_products(products, [])
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(len(result.matches[0].offers), 3)
        self.assertEqual(result.matches[0].cheapest_available.store, "dns")

    def test_config_conflict_not_merged(self) -> None:
        products = [
            _p("regard", "1", "R-SKU", 250_000, 1),
            _p("andpro", "2", "LINK", 240_000, 2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "LINK",
                "normalized_value": "LINK",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "LINK",
                "normalized_value": "LINK",
            },
        ]
        specs = {
            ("regard", "1"): _identity(
                store="regard", external_id="1", sku="R-SKU", ram_gb=16
            ),
            ("andpro", "2"): _identity(
                store="andpro", external_id="2", sku="LINK", ram_gb=32
            ),
        }
        result = match_products(products, identifiers, specs_by_key=specs)
        self.assertEqual(len(result.matches), 0)
        self.assertGreaterEqual(result.stats["conflicts"], 1)

    def test_cross_store_requires_fresh(self) -> None:
        match = ProductMatch(
            normalized_sku="G614",
            display_sku="G614",
            name="Laptop",
            offers=[
                Offer(
                    store="regard",
                    external_id="1",
                    name="L",
                    sku="G614",
                    price=200_000,
                    available=True,
                    url="u1",
                    product_id=None,
                ),
                Offer(
                    store="andpro",
                    external_id="2",
                    name="L",
                    sku="G614",
                    price=230_000,
                    available=True,
                    url="u2",
                    product_id=None,
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                one = evaluate_cross_store(conn, [match], fresh_stores={"regard"})
                self.assertEqual(one, [])
                two = evaluate_cross_store(
                    conn, [match], fresh_stores={"regard", "andpro"}
                )
                self.assertEqual(len(two), 1)


class RankingTests(unittest.TestCase):
    def test_5080_scores_higher_than_5070ti_same_price(self) -> None:
        s5080, _ = score_offer(
            price=280_000, gpu="RTX 5080", ram_gb=32, ssd_gb=1000, screen_inch=18
        )
        s5070, _ = score_offer(
            price=280_000, gpu="RTX 5070 Ti", ram_gb=32, ssd_gb=1000, screen_inch=18
        )
        self.assertGreater(s5080, s5070)

    def test_reasons_include_gpu(self) -> None:
        score, reasons = score_offer(price=220_000, gpu="RTX 5070 Ti", ram_gb=32)
        self.assertGreater(score, 0)
        self.assertTrue(any("5070" in r for r in reasons))

    def test_deterministic(self) -> None:
        a = score_offer(
            price=250_000, gpu="RTX 5070 Ti", ram_gb=32, ssd_gb=1024, screen_inch=17
        )
        b = score_offer(
            price=250_000, gpu="RTX 5070 Ti", ram_gb=32, ssd_gb=1024, screen_inch=17
        )
        self.assertEqual(a, b)

    def test_rank_clusters_order(self) -> None:
        matches = [
            ProductMatch(
                normalized_sku="A",
                display_sku="A",
                name="Cheap 5070",
                offers=[
                    Offer(
                        store="regard",
                        external_id="1",
                        name="Cheap 5070",
                        sku="A",
                        price=210_000,
                        available=True,
                        url="u",
                    )
                ],
            ),
            ProductMatch(
                normalized_sku="B",
                display_sku="B",
                name="Expensive junk",
                offers=[
                    Offer(
                        store="regard",
                        external_id="2",
                        name="Expensive junk",
                        sku="B",
                        price=400_000,
                        available=True,
                        url="u",
                    )
                ],
            ),
        ]
        ranked = rank_clusters(matches)
        self.assertGreaterEqual(ranked[0].score, ranked[-1].score)


class TelegramSortTests(unittest.TestCase):
    def test_groups_sorted_by_priority(self) -> None:
        events = [
            {
                "id": 1,
                "event_type": EVENT_PRICE_DROP,
                "product_id": 1,
                "metadata": {"new_price": 400_000, "gpu": "RTX 5070 Ti"},
            },
            {
                "id": 2,
                "event_type": EVENT_CROSS_STORE,
                "product_id": 2,
                "metadata": {
                    "new_price": 210_000,
                    "gpu": "RTX 5080",
                    "saving": 30_000,
                    "ram_gb": 32,
                },
            },
        ]
        groups = group_alert_events_for_delivery(events)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0].events[0]["id"], 2)


class StoreRunsTests(unittest.TestCase):
    def test_store_runs_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                rid = create_pipeline_run(
                    conn,
                    started_at="2026-01-01T00:00:00+00:00",
                    status="running",
                    app_version="0.2.0",
                    instance_id="t",
                )
                insert_store_run(
                    conn,
                    pipeline_run_id=rid,
                    store="regard",
                    attempted_at="2026-01-01T00:00:00+00:00",
                    finished_at="2026-01-01T00:00:01+00:00",
                    status="ok",
                    products_count=10,
                )
                insert_store_run(
                    conn,
                    pipeline_run_id=rid,
                    store="andpro",
                    attempted_at="2026-01-01T00:00:00+00:00",
                    finished_at="2026-01-01T00:00:01+00:00",
                    status="failed",
                    error_message="boom",
                )
                latest = get_latest_store_runs(conn)
                self.assertEqual(latest["regard"]["status"], "ok")
                self.assertEqual(latest["andpro"]["status"], "failed")


class BackupTests(unittest.TestCase):
    def test_backup_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            out = Path(tmp) / "backups"
            save_products(
                [
                    Product(
                        store="regard",
                        external_id="1",
                        url="https://x",
                        name="n",
                        sku="S",
                        price=100_000,
                        available=True,
                        checked_at=datetime.now(timezone.utc),
                    )
                ],
                db,
            )
            path = backup_sqlite(db, out)
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)
            self.assertEqual(prune_backups(out, retention_days=30), 0)


class ControlAuthUnit(unittest.TestCase):
    def test_admin_allowlist_from_env(self) -> None:
        old = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
        os.environ["TELEGRAM_ADMIN_CHAT_ID"] = "111,222"
        try:
            ids = config.get_telegram_admin_chat_ids()
            self.assertEqual(ids, {"111", "222"})
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_ADMIN_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_ADMIN_CHAT_ID"] = old

    def test_unauthorized_rejected(self) -> None:
        old = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
        os.environ["TELEGRAM_ADMIN_CHAT_ID"] = "999"
        try:
            self.assertFalse(_is_admin(111))
            self.assertTrue(_is_admin(999))
            client = MagicMock()
            client.post.return_value = MagicMock()
            process_update(
                client,
                "fake-token",
                {
                    "callback_query": {
                        "id": "1",
                        "data": "ctrl:run",
                        "message": {"chat": {"id": 111}},
                    }
                },
            )
            self.assertTrue(client.post.called)
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_ADMIN_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_ADMIN_CHAT_ID"] = old


class OpsNotifyFormat(unittest.TestCase):
    def test_format_ops_message(self) -> None:
        class R:
            status = "partial"
            regard_status = "ok"
            andpro_status = "failed"
            error_stage = "collection"
            error_message = "andpro down"
            duration_seconds = 1.2

        text = format_ops_message(R())
        self.assertIn("0.2.0", text)
        self.assertIn("PARTIAL", text)
        self.assertIn("Regard", text)


if __name__ == "__main__":
    unittest.main()
