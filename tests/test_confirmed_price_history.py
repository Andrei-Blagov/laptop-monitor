"""Real snapshots/SQLite: no historical prices cross a configuration boundary."""
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests import test_f1_r1_variant_consistency as fixture
from confirmed_history import configuration_key, save_confirmed_prices, load_model_history
from market_snapshot import export_russian_market_snapshot, load_russian_market_snapshot
from storage import init_db, open_db, get_all_products_readonly
from model_price_history import build_model_history_report
from identity_sync import load_specs_cache
from alerts import run_monitor, EVENT_PRICE_DROP


class ConfirmedPriceHistoryTests(unittest.TestCase):
    setUp = fixture.ReusedListingTests.setUp
    tearDown = fixture.ReusedListingTests.tearDown
    write_variant = fixture.ReusedListingTests.write_variant
    publish = fixture.ReusedListingTests.publish
    selected = fixture.ReusedListingTests.selected

    def point(self, price, days=0, *, sku='A2XWHG-814XRU', ram=32, captured=True, cpu='Intel Core Ultra 9 275HX'):
        stamp = self.now - timedelta(days=days)
        self.write_variant(sku, 'MSI Raider '+sku, ram, price, stamp, captured=captured, cpu=cpu)
        export_russian_market_snapshot(self.db, specs_path=self.specs,
            snapshot_root=self.snapshot_root, instance_id='history-test', now=stamp,
            pipeline_lock_held=True)
        document = load_russian_market_snapshot(self.snapshot_root)
        records = save_confirmed_prices(self.db, document, now=stamp)
        # Second publication includes this observation, without changing its date.
        export_russian_market_snapshot(self.db, specs_path=self.specs,
            snapshot_root=self.snapshot_root, instance_id='history-test', now=stamp,
            pipeline_lock_held=True)
        return records

    def report(self):
        pid = get_all_products_readonly(self.db)[0]['id']
        with patch('russian_deals.load_specs_cache', side_effect=lambda: load_specs_cache(self.specs)):
            return build_model_history_report(self.db, pid, now=self.now, confirmed_only=True)

    def test_legacy_preserved_and_recording_idempotent(self):
        with open_db(self.db) as conn:
            before = [tuple(r) for r in conn.execute('SELECT * FROM price_history')]
        self.point(250000)
        doc = load_russian_market_snapshot(self.snapshot_root)
        save_confirmed_prices(self.db, doc, now=self.now)
        with open_db(self.db) as conn:
            legacy = [tuple(r) for r in conn.execute('SELECT * FROM price_history WHERE configuration_key IS NULL')]
            count = conn.execute('SELECT COUNT(*) FROM price_history WHERE configuration_key IS NOT NULL').fetchone()[0]
        self.assertEqual(legacy, before)
        self.assertEqual(count, 1)

    def test_days_minimum_and_current_quote_use_same_configuration(self):
        self.point(285000, 40)
        self.point(270000, 10)
        self.point(250000)
        report = self.report()
        self.assertEqual((report.best_30d_ago, report.best_7d_ago, report.all_time_min),
                         (285000, 270000, 250000))
        self.assertEqual(report.best_now, 250000)
        deal = self.selected()[0]
        self.assertEqual(deal.offer['price_history']['price_7d'], 270000)
        self.assertEqual(deal.offer['price_history']['price_30d'], 285000)
        from control_bot import build_top_parts
        text = '\n'.join(build_top_parts(self.db, deals=[deal]))
        self.assertIn('SKU: A2XWHG-814XRU', text)
        self.assertIn('−20 000 ₽', text)

    def test_reused_id_and_same_price_new_sku_ram_starts_separate_history(self):
        self.point(190000, 40)
        self.point(250000, 10)
        self.point(250000, sku='VARIANT-NEW', ram=64)
        report = self.report()
        self.assertEqual(report.all_time_min, 250000)
        self.assertIsNone(report.best_7d_ago)
        self.assertIsNone(report.best_30d_ago)
        with open_db(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT configuration_key) FROM price_history').fetchone()[0], 2)

    def test_same_sku_changed_ram_does_not_inherit_minimum(self):
        self.point(200000, 10)
        self.point(250000, ram=64)
        self.assertEqual(self.report().all_time_min, 250000)
        self.assertIsNone(self.report().best_7d_ago)

    def test_equivalent_identifiers_specs_have_same_key(self):
        specs = dict(gpu='RTX 5070 Ti', cpu='Intel Core Ultra 9 275HX', ram_gb=32,
                     ssd_gb=1024, screen_inch=16, screen_resolution='2560x1600')
        a = configuration_key(' A2XWHG-814XRU ', specs)
        b = configuration_key('a2xwhg-814xru', dict(specs, cpu='Core Ultra 9 275HX', ssd_gb=1000))
        self.assertEqual(a, b)
        self.assertNotEqual(a, configuration_key('A2XWHG-814XRU', dict(specs, ram_gb=64)))
        self.assertIsNone(configuration_key(None, specs))

    def test_missing_evidence_not_recorded_or_alerted(self):
        records = self.point(250000, captured=False)
        self.assertEqual(records, {})
        with open_db(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM price_history WHERE configuration_key IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(run_monitor(self.db, specs_path=self.specs, confirmed_offers=records).total_new, 0)
        self.assertFalse(self.report().found)

    def test_drop_alert_once_and_no_drop_from_other_configuration(self):
        self.point(280000, 10)
        records = self.point(250000)
        first = run_monitor(self.db, specs_path=self.specs, confirmed_offers=records)
        self.assertEqual(first.counts[EVENT_PRICE_DROP], 1)
        self.assertEqual(run_monitor(self.db, specs_path=self.specs, confirmed_offers=records).total_new, 0)
        records = self.point(220000, sku='VARIANT-NEW', ram=64)
        self.assertEqual(run_monitor(self.db, specs_path=self.specs, confirmed_offers=records).counts[EVENT_PRICE_DROP], 0)

    def test_additive_migration_preserves_old_schema_rows(self):
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            conn.execute("ALTER TABLE price_history DROP COLUMN configuration_key")
            old = conn.execute("SELECT * FROM price_history ORDER BY id").fetchall()
        with open_db(self.db) as conn:
            init_db(conn)
            new = [tuple(r) for r in conn.execute("SELECT id,product_id,price,available,checked_at FROM price_history ORDER BY id")]
            self.assertEqual(new, old)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM price_history WHERE configuration_key IS NOT NULL").fetchone()[0], 0)

    def test_unknown_cpu_keeps_configuration_history(self):
        self.point(270000, 10, cpu=None)
        self.point(250000, cpu=None)
        self.assertEqual(self.report().best_7d_ago, 270000)
        self.assertEqual(self.report().all_time_min, 250000)

    def test_alert_rejects_specs_changed_after_snapshot_at_same_price(self):
        self.point(280000, 10)
        records = self.point(250000)
        self.write_variant('A2XWHG-814XRU', 'MSI Raider changed configuration',
                           64, 250000, self.now)
        self.assertEqual(run_monitor(self.db, specs_path=self.specs, confirmed_offers=records).total_new, 0)

    def test_chart_source_only_contains_selected_configuration(self):
        self.point(180000, 40)
        self.point(250000, sku='VARIANT-NEW', ram=64)
        pid = get_all_products_readonly(self.db)[0]['id']
        with patch('russian_deals.load_specs_cache', side_effect=lambda: load_specs_cache(self.specs)):
            match, history = load_model_history(self.db, pid)
        self.assertEqual(match.display_sku, 'VARIANT-NEW')
        self.assertEqual([r['price'] for rows in history.values() for r in rows], [250000])
