"""Real SQLite/specs/snapshot regression for a reused Russian listing ID."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from control_bot import build_top_parts, handle_personal_top, handle_recommendation_pick, unique_russian_top
from identity_sync import load_specs_cache, save_specs_cache
from market_snapshot import export_russian_market_snapshot, load_russian_market_snapshot
from models import Product
from product_identity import ProductIdentity
from storage import create_pipeline_run, insert_store_run, open_db, save_products
from thai_catalog import russian_offer_views


class ReusedListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "market.db"
        self.specs = self.root / "specs.json"
        self.snapshot_root = self.root / "russian_market"
        self.now = datetime.now(timezone.utc)
        self.old_at = self.now - timedelta(minutes=40)
        self.new_at = self.now - timedelta(minutes=10)
        self.url = "https://example.test/regard/reuse-id"
        self.write_variant("A2XWHG-814XRU", "MSI Raider 16 HX AI A2XWHG-814XRU", 32, 250000, self.old_at)
        self.publish()

    def tearDown(self):
        self.temp.cleanup()

    def write_variant(self, sku, name, ram, price, checked_at, *, captured=True, cpu="Intel Core Ultra 9 275HX"):
        product = Product(store="regard", external_id="reuse-id", url=self.url, name=name,
                          sku=sku, price=price, available=True, checked_at=checked_at)
        save_products([product], self.db)
        stamp = checked_at.isoformat()
        fields = ("cpu", "gpu", "ram_gb", "ssd_gb", "screen_size_inch", "screen_resolution")
        identity = ProductIdentity(
            store="regard", external_id="reuse-id", sku=sku, name=name, price=price,
            available=True, url=self.url, cpu=cpu, gpu="RTX 5070 Ti", gpu_vram_gb=12,
            ram_gb=ram, ssd_gb=1024, screen_size_inch=16.0, screen_resolution="2560x1600",
            source_identifiers={"field_captured_at": {field: stamp for field in fields} if captured else {}},
        )
        save_specs_cache({"regard:reuse-id": identity.to_dict()}, self.specs)
        with open_db(self.db) as conn:
            run_id = create_pipeline_run(conn, started_at=stamp, status="success")
            insert_store_run(conn, pipeline_run_id=run_id, store="regard", attempted_at=stamp,
                             finished_at=stamp, status="ok", products_count=1, error_message=None)

    def publish(self):
        export_russian_market_snapshot(self.db, specs_path=self.specs,
                                       snapshot_root=self.snapshot_root, instance_id="f1-r1",
                                       now=self.now, pipeline_lock_held=True)

    def selected(self):
        original = load_specs_cache
        with patch("russian_deals.load_specs_cache", side_effect=lambda: original(self.specs)):
            return unique_russian_top(self.db, limit=10)

    def views(self):
        with patch("russian_deals.load_specs_cache", side_effect=lambda: load_specs_cache(self.specs)), patch(
            "control_bot.DEFAULT_DB_PATH", self.db
        ), patch("control_bot._reply_or_edit") as favorites, patch("control_bot.send_message") as buy:
            top = "\n".join(build_top_parts(self.db))
            handle_personal_top(Mock(), "token", 42)
            handle_recommendation_pick(Mock(), "token", 42, "best", db_path=self.db)
        return top, favorites.call_args.args[3], buy.call_args.args[3]

    def daily(self):
        raw = load_russian_market_snapshot(self.snapshot_root)
        return russian_offer_views(raw, now=self.now)[0]

    def test_superseded_variant_same_price_url_is_rejected_everywhere(self):
        self.assertEqual(len(self.daily()), 1)
        self.write_variant("VARIANT-NEW", "MSI Raider New variant 64GB", 64, 250000, self.new_at)
        self.assertEqual(self.selected(), [])
        top, favorites, buy = self.views()
        for text in (top, favorites, buy):
            self.assertNotIn(self.url, text)
        self.assertNotIn("New variant 64GB", top)
        self.assertNotIn("64 ГБ", top)
        self.publish()
        refreshed = self.selected()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual((refreshed[0].offer["sku"], refreshed[0].ram_gb), ("VARIANT-NEW", 64))

    def test_same_sku_and_confirmed_configuration_stays_coherent(self):
        self.write_variant("A2XWHG-814XRU", "MSI Raider updated title", 32, 250000, self.new_at)
        chosen = self.selected()
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].cluster_name, "MSI Raider 16 HX AI A2XWHG-814XRU")
        self.assertEqual(chosen[0].ram_gb, 32)
        self.assertEqual(chosen[0].offer["price_checked_at"], self.old_at.isoformat())
        top, favorites, buy = self.views()
        for text in (top, favorites, buy):
            self.assertIn(self.url, text)
        self.assertEqual(len(self.daily()), 1)

    def test_normalized_equivalent_identifier_is_accepted(self):
        self.write_variant(" a2xwhg-814xru ", "MSI Raider updated title", 32, 250000,
                           self.new_at, cpu="Core Ultra 9 275HX")
        self.assertEqual(len(self.selected()), 1)

    def test_price_change_waits_for_new_snapshot(self):
        self.write_variant("A2XWHG-814XRU", "MSI Raider same variant", 32, 245000, self.new_at)
        self.assertEqual(self.selected(), [])
        self.publish()
        chosen = self.selected()
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].price, 245000)
        self.assertEqual(chosen[0].offer["price_checked_at"], self.new_at.isoformat())

    def test_missing_or_conflicting_evidence_excludes_offer(self):
        self.write_variant("A2XWHG-814XRU", "MSI Raider same variant", 32, 250000,
                           self.new_at, captured=False)
        self.publish()
        self.assertEqual(self.daily(), [])
        self.assertEqual(self.selected(), [])
        self.write_variant("A2XWHG-814XRU", "MSI Raider changed RAM", 64, 250000, self.new_at)
        self.assertEqual(self.selected(), [])

    def test_unknown_cpu_remains_eligible_with_label(self):
        self.write_variant("A2XWHG-814XRU", "MSI Raider unknown CPU", 32, 250000,
                           self.new_at, cpu=None)
        self.publish()
        self.assertEqual(len(self.daily()), 1)
        chosen = self.selected()
        self.assertEqual(len(chosen), 1)
        self.assertIn("CPU не подтверждён", "\n".join(build_top_parts(self.db, deals=chosen)))

    def test_price_cap_still_applies_to_confirmed_variant(self):
        self.write_variant("A2XWHG-814XRU", "MSI Raider price boundary", 32, 290000, self.new_at)
        self.publish()
        self.assertEqual(len(self.daily()), 1)
        self.assertEqual(len(self.selected()), 1)
        self.write_variant("A2XWHG-814XRU", "MSI Raider above cap", 32, 290001, self.new_at)
        self.publish()
        self.assertEqual(self.daily(), [])
        self.assertEqual(self.selected(), [])


if __name__ == "__main__":
    unittest.main()
