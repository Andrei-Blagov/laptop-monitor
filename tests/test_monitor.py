from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
    get_current_deals,
    run_monitor,
)
from identity_sync import save_specs_cache
from models import Product
from product_identity import ProductIdentity
from storage import (
    DEFAULT_DB_PATH,
    get_alert_events,
    init_db,
    open_db,
    save_products,
    upsert_product_identifier,
)
from target_gpu import get_target_gpu


PRODUCTION_DB = (Path("data") / "laptop_monitor.db").resolve()


def _now(offset_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


def _product(
    *,
    store: str = "regard",
    external_id: str = "1",
    sku: str = "SKU-1",
    name: str = "Test Laptop RTX 5070 Ti",
    price: int | None = 250_000,
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


def _spec(
    *,
    store: str,
    external_id: str,
    sku: str,
    gpu: str,
    name: str = "Spec Laptop",
) -> dict:
    identity = ProductIdentity(
        store=store,
        external_id=external_id,
        sku=sku,
        name=name,
        price=100,
        available=True,
        url=f"https://example.test/{store}/{external_id}",
        brand="TEST",
        gpu=gpu,
    )
    return identity.to_dict()


class MonitorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmpdir.name) / "monitor_test.db"
        self.specs_path = Path(self._tmpdir.name) / "specs.json"
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)

    def tearDown(self) -> None:
        # Confirm production untouched by path isolation.
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)
        self._tmpdir.cleanup()

    def _write_specs(self, *specs: dict) -> None:
        cache = {
            f"{s['store']}:{s['external_id']}": s for s in specs
        }
        save_specs_cache(cache, self.specs_path)

    def _run(self):
        return run_monitor(self.db_path, specs_path=self.specs_path)

    def _product_id(self, store: str, external_id: str) -> int:
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM products WHERE store=? AND external_id=?",
                (store, external_id),
            ).fetchone()
            assert row is not None
            return int(row["id"])


class TargetGpuTests(unittest.TestCase):
    def test_from_normalized_gpu(self) -> None:
        self.assertEqual(
            get_target_gpu({"gpu": "RTX 5070 TI LAPTOP"}),
            "RTX 5070 Ti",
        )
        self.assertEqual(
            get_target_gpu({"gpu": "RTX 5080 LAPTOP"}),
            "RTX 5080",
        )

    def test_does_not_match_random_5070(self) -> None:
        self.assertIsNone(get_target_gpu({"name": "Model 5070 series"}))
        self.assertIsNone(get_target_gpu({"name": "RTX 4070 Laptop"}))

    def test_5080_ti_not_5080(self) -> None:
        # normalize_gpu maps 5080 TI oddly; ensure explicit Ti path for 5070 only.
        self.assertEqual(
            get_target_gpu("GeForce RTX 5070 Ti Laptop GPU"),
            "RTX 5070 Ti",
        )


class PriceDropTests(MonitorTestBase):
    def test_first_price_no_drop(self) -> None:
        save_products([_product(price=250_000)], self.db_path)
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5070 TI LAPTOP")
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)

    def test_two_percent_no_drop(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products(
            [_product(price=245_000, checked_at=_now(10))],
            self.db_path,
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)

    def test_six_percent_drop(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products(
            [_product(price=235_000, checked_at=_now(10))],
            self.db_path,
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 1)
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 0)
        event = result.created[0]
        self.assertEqual(event["event_type"], EVENT_PRICE_DROP)
        self.assertEqual(event["old_price"], 250_000)
        self.assertEqual(event["new_price"], 235_000)
        self.assertTrue(event["metadata"].get("is_historical_low"))
        self.assertEqual(event["metadata"].get("previous_historical_low"), 250_000)

    def test_price_increase_no_drop(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products(
            [_product(price=260_000, checked_at=_now(10))],
            self.db_path,
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)

    def test_price_none_no_exception(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products(
            [_product(price=None, checked_at=_now(10))],
            self.db_path,
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)


class HistoricalLowTests(MonitorTestBase):
    def test_first_price_not_historical_low(self) -> None:
        save_products([_product(price=250_000)], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 0)

    def test_new_historical_minimum(self) -> None:
        # 5000 ₽ ниже предыдущего min (245000) — проходит absolute threshold.
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=245_000, checked_at=_now(10))], self.db_path)
        save_products([_product(price=240_000, checked_at=_now(20))], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 1)
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)

    def test_price_above_old_minimum_not_low(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=240_000, checked_at=_now(10))], self.db_path)
        save_products([_product(price=245_000, checked_at=_now(20))], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 0)

    def test_tiny_drop_not_historical_low(self) -> None:
        # -264 ₽ / ~0.12% — ниже обоих порогов.
        save_products([_product(price=227_798, checked_at=_now(0))], self.db_path)
        save_products([_product(price=227_534, checked_at=_now(10))], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 0)
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)

    def test_three_percent_historical_low(self) -> None:
        # 3% ниже min, но <5% от previous → только HISTORICAL_LOW.
        save_products([_product(price=200_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=194_000, checked_at=_now(10))], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 1)
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 0)

    def test_absolute_4000_even_if_under_2_percent(self) -> None:
        # 4000 ₽ при ~1.6% (<2%) — проходит absolute threshold.
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=246_000, checked_at=_now(10))], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 1)

    def test_price_drop_suppresses_separate_historical_low(self) -> None:
        # 10.7% drop + new hist low → только PRICE_DROP с флагом.
        save_products([_product(price=293_821, checked_at=_now(0))], self.db_path)
        save_products([_product(price=262_512, checked_at=_now(10))], self.db_path)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 1)
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 0)
        meta = result.created[0]["metadata"]
        self.assertTrue(meta["is_historical_low"])
        self.assertEqual(meta["previous_historical_low"], 293_821)

    def test_price_drop_without_new_historical_low_flag(self) -> None:
        # Drop 6% but not below all-time min (went up then down partially).
        save_products([_product(price=200_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=250_000, checked_at=_now(10))], self.db_path)
        save_products([_product(price=230_000, checked_at=_now(20))], self.db_path)
        # 250 -> 230 = 8% PRICE_DROP; hist min before current = 200k; 230 > 200 → not hist low
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_PRICE_DROP, 0), 1)
        self.assertEqual(result.counts.get(EVENT_HISTORICAL_LOW, 0), 0)
        self.assertFalse(result.created[0]["metadata"].get("is_historical_low"))


class TargetPriceTests(MonitorTestBase):
    def test_rtx_5070_ti_at_or_below_threshold(self) -> None:
        save_products(
            [
                _product(
                    price=222_190,
                    name="Gigabyte A16 RTX 5070 Ti",
                    available=True,
                )
            ],
            self.db_path,
        )
        self._write_specs(
            _spec(
                store="regard",
                external_id="1",
                sku="SKU-1",
                gpu="RTX 5070 TI LAPTOP",
                name="Gigabyte A16",
            )
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_TARGET_PRICE, 0), 1)
        self.assertEqual(result.created[0]["metadata"]["gpu"], "RTX 5070 Ti")
        self.assertEqual(
            result.created[0]["metadata"]["threshold"],
            config.TARGET_PRICES["RTX 5070 Ti"],
        )

    def test_above_threshold_no_target(self) -> None:
        save_products([_product(price=250_000)], self.db_path)
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5070 TI LAPTOP")
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_TARGET_PRICE, 0), 0)

    def test_rtx_5080_target(self) -> None:
        save_products(
            [
                _product(
                    price=295_000,
                    name="Laptop RTX 5080",
                )
            ],
            self.db_path,
        )
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5080 LAPTOP")
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_TARGET_PRICE, 0), 1)
        self.assertEqual(result.created[0]["metadata"]["gpu"], "RTX 5080")

    def test_unavailable_below_threshold_no_target(self) -> None:
        save_products(
            [_product(price=200_000, available=False)],
            self.db_path,
        )
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5070 TI LAPTOP")
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_TARGET_PRICE, 0), 0)

    def test_reenter_below_threshold_creates_new_event(self) -> None:
        save_products([_product(price=220_000, checked_at=_now(0))], self.db_path)
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5070 TI LAPTOP")
        )
        first = self._run()
        self.assertEqual(first.counts.get(EVENT_TARGET_PRICE, 0), 1)

        # Rise above threshold.
        save_products([_product(price=240_000, checked_at=_now(10))], self.db_path)
        mid = self._run()
        self.assertEqual(mid.counts.get(EVENT_TARGET_PRICE, 0), 0)

        # Drop below again.
        save_products([_product(price=210_000, checked_at=_now(20))], self.db_path)
        second = self._run()
        self.assertEqual(second.counts.get(EVENT_TARGET_PRICE, 0), 1)

        with open_db(self.db_path) as conn:
            events = get_alert_events(conn, event_type=EVENT_TARGET_PRICE)
        self.assertEqual(len(events), 2)


class CrossStoreTests(MonitorTestBase):
    def _seed_pair(
        self,
        *,
        regard_price: int,
        andpro_price: int,
        regard_available: bool = True,
        andpro_available: bool = True,
        sku: str = "SHARED-SKU",
    ) -> None:
        save_products(
            [
                _product(
                    store="regard",
                    external_id="10",
                    sku=sku,
                    price=regard_price,
                    available=regard_available,
                    name="Model Regard",
                ),
                _product(
                    store="andpro",
                    external_id="20",
                    sku=sku,
                    price=andpro_price,
                    available=andpro_available,
                    name="Model ANDPRO",
                ),
            ],
            self.db_path,
        )
        # Exact SKU match — no extra identifiers needed.
        with open_db(self.db_path) as conn:
            init_db(conn)
            for store, ext in (("regard", "10"), ("andpro", "20")):
                row = conn.execute(
                    "SELECT id, sku FROM products WHERE store=? AND external_id=?",
                    (store, ext),
                ).fetchone()
                upsert_product_identifier(
                    conn,
                    product_id=int(row["id"]),
                    identifier_type="sku",
                    value=row["sku"],
                    normalized_value=row["sku"].upper(),
                    source="test",
                )

    def test_difference_above_threshold(self) -> None:
        self._seed_pair(regard_price=270_190, andpro_price=283_605)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_CROSS_STORE, 0), 1)
        meta = result.created[0]["metadata"]
        self.assertEqual(meta["difference"], 13_415)
        self.assertEqual(meta["cheapest_store"], "regard")

    def test_unchanged_cross_store_not_duplicated(self) -> None:
        self._seed_pair(regard_price=270_190, andpro_price=283_605)
        first = self._run()
        self.assertEqual(first.counts.get(EVENT_CROSS_STORE, 0), 1)
        second = self._run()
        self.assertEqual(second.counts.get(EVENT_CROSS_STORE, 0), 0)

    def test_changed_cross_store_prices_create_new(self) -> None:
        self._seed_pair(regard_price=270_190, andpro_price=283_605)
        first = self._run()
        self.assertEqual(first.counts.get(EVENT_CROSS_STORE, 0), 1)
        # Meaningful price change → new dedupe key.
        self._seed_pair(regard_price=260_000, andpro_price=283_605)
        second = self._run()
        self.assertEqual(second.counts.get(EVENT_CROSS_STORE, 0), 1)

    def test_difference_below_threshold(self) -> None:
        self._seed_pair(regard_price=270_000, andpro_price=275_000)
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_CROSS_STORE, 0), 0)

    def test_unavailable_cheap_not_used(self) -> None:
        # Cheap regard unavailable; only andpro available → no cross-store pair.
        self._seed_pair(
            regard_price=200_000,
            andpro_price=283_605,
            regard_available=False,
            andpro_available=True,
        )
        result = self._run()
        self.assertEqual(result.counts.get(EVENT_CROSS_STORE, 0), 0)

        deals = get_current_deals(self.db_path, specs_path=self.specs_path)
        self.assertEqual(len(deals.cross_store), 0)


class DedupeAndIdempotencyTests(MonitorTestBase):
    def test_repeat_monitor_zero_new_events(self) -> None:
        save_products(
            [
                _product(price=220_000, name="Cheap 5070 Ti"),
                _product(
                    store="andpro",
                    external_id="2",
                    sku="SKU-1",
                    price=240_000,
                    name="Same SKU ANDPRO",
                ),
            ],
            self.db_path,
        )
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5070 TI LAPTOP"),
            _spec(store="andpro", external_id="2", sku="SKU-1", gpu="RTX 5070 TI LAPTOP"),
        )
        first = self._run()
        self.assertGreater(first.total_new, 0)
        second = self._run()
        self.assertEqual(second.total_new, 0)

    def test_dedupe_same_event(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=235_000, checked_at=_now(10))], self.db_path)
        first = self._run()
        self.assertEqual(first.counts.get(EVENT_PRICE_DROP, 0), 1)
        second = self._run()
        self.assertEqual(second.counts.get(EVENT_PRICE_DROP, 0), 0)
        with open_db(self.db_path) as conn:
            self.assertEqual(
                len(get_alert_events(conn, event_type=EVENT_PRICE_DROP)),
                1,
            )

    def test_price_change_creates_new_event(self) -> None:
        save_products([_product(price=250_000, checked_at=_now(0))], self.db_path)
        save_products([_product(price=235_000, checked_at=_now(10))], self.db_path)
        first = self._run()
        self.assertEqual(first.counts.get(EVENT_PRICE_DROP, 0), 1)

        save_products([_product(price=220_000, checked_at=_now(20))], self.db_path)
        second = self._run()
        self.assertEqual(second.counts.get(EVENT_PRICE_DROP, 0), 1)

        with open_db(self.db_path) as conn:
            drops = get_alert_events(conn, event_type=EVENT_PRICE_DROP)
        self.assertEqual(len(drops), 2)

    def test_production_db_path_not_used(self) -> None:
        self.assertNotEqual(self.db_path.resolve(), PRODUCTION_DB)
        def _production_snapshot():
            if not PRODUCTION_DB.exists():
                return None
            st = PRODUCTION_DB.stat()
            return (st.st_size, st.st_mtime_ns)

        before = _production_snapshot()
        save_products([_product(price=200_000)], self.db_path)
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SKU-1", gpu="RTX 5070 TI LAPTOP")
        )
        self._run()
        self.assertEqual(before, _production_snapshot())


class DealsTests(MonitorTestBase):
    def test_deals_independent_of_alerts(self) -> None:
        save_products(
            [
                _product(
                    store="regard",
                    external_id="1",
                    sku="SHARED",
                    price=220_000,
                    name="Cheap",
                ),
                _product(
                    store="andpro",
                    external_id="2",
                    sku="SHARED",
                    price=240_000,
                    name="Expensive",
                ),
            ],
            self.db_path,
        )
        self._write_specs(
            _spec(store="regard", external_id="1", sku="SHARED", gpu="RTX 5070 TI LAPTOP"),
            _spec(store="andpro", external_id="2", sku="SHARED", gpu="RTX 5070 TI LAPTOP"),
        )
        # Create alerts once.
        self._run()
        self._run()  # second: 0 new

        deals = get_current_deals(self.db_path, specs_path=self.specs_path)
        self.assertGreaterEqual(len(deals.target_price), 1)
        self.assertGreaterEqual(len(deals.cross_store), 1)
        best = deals.cheapest_by_gpu["RTX 5070 Ti"]
        self.assertIsNotNone(best)
        self.assertEqual(best["price"], 220_000)


if __name__ == "__main__":
    unittest.main()
