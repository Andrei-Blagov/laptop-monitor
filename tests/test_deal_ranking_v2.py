from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from comparison import Offer, ProductMatch
from deal_ranking import (
    RankedDeal,
    canonical_gpu,
    classify_cpu,
    compute_confidence,
    format_top_deals_message,
    historical_mins_from_rows,
    rank_clusters,
    score_cross_store_saving,
    score_gpu,
    score_historical_opportunity,
    score_offer,
    score_price_value,
    score_ram,
    score_screen,
    score_ssd,
)
from models import Product
from storage import (
    count_price_history,
    count_products,
    create_pipeline_run,
    get_product,
    init_db,
    insert_store_run,
    open_db,
    save_products,
)


def _now() -> datetime:
    return datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)


def _offer(**kwargs: object) -> Offer:
    base = dict(
        store="kns",
        external_id="1",
        name="Laptop",
        sku="SKU",
        price=220_000,
        available=True,
        url="https://example/x",
        product_id=1,
    )
    base.update(kwargs)
    return Offer(**base)  # type: ignore[arg-type]


def _match(name: str, offers: list[Offer], sku: str = "S") -> ProductMatch:
    return ProductMatch(
        normalized_sku=sku,
        display_sku=sku,
        name=name,
        offers=offers,
    )


class DealRankingV2Tests(unittest.TestCase):
    # --- GPU ---
    def test_01_5080_gt_5070ti_equal_rest(self) -> None:
        a = score_offer(
            price=280_000,
            gpu="RTX 5080",
            cpu="Ryzen 9 8940HX",
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=18,
        )
        b = score_offer(
            price=280_000,
            gpu="RTX 5070 Ti",
            cpu="Ryzen 9 8940HX",
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=18,
        )
        self.assertGreater(a.score, b.score)
        self.assertEqual(score_gpu("RTX 5080")[0], config.SCORE_GPU_5080)
        self.assertEqual(score_gpu("RTX 5070 Ti")[0], config.SCORE_GPU_5070TI)

    def test_02_unknown_gpu_zero(self) -> None:
        pts, reason = score_gpu(None)
        self.assertEqual(pts, 0)
        self.assertIsNone(reason)
        self.assertEqual(score_gpu("RTX 5070")[0], 0)  # not Ti
        self.assertEqual(score_gpu("RTX 4090")[0], 0)

    # --- PRICE ---
    def test_03_price_below_at_above_target(self) -> None:
        below, _ = score_price_value(200_000, "RTX 5070 Ti")
        at, _ = score_price_value(230_000, "RTX 5070 Ti")
        above, _ = score_price_value(250_000, "RTX 5070 Ti")
        far, _ = score_price_value(290_000, "RTX 5070 Ti")
        self.assertGreater(below, at)
        self.assertGreater(at, above)
        self.assertGreater(above, far)
        self.assertLessEqual(below, config.SCORE_PRICE_MAX)
        self.assertGreaterEqual(far, 0)

    def test_04_eligibility_300k_unchanged(self) -> None:
        self.assertTrue(config.is_price_in_tracking_scope(300_000))
        self.assertFalse(config.is_price_in_tracking_scope(300_001))
        match = _match(
            "Over",
            [_offer(price=350_000, available=True)],
        )
        self.assertEqual(rank_clusters([match]), [])

    # --- CPU ---
    def test_05_hx_stronger_than_non_hx(self) -> None:
        hx, _ = classify_cpu("AMD Ryzen 9 8940HX")
        h, _ = classify_cpu("INTEL CORE 7 240H")
        mid, _ = classify_cpu("Ryzen 5 7535HS")
        self.assertGreater(hx, h)
        self.assertGreaterEqual(h, mid)

    def test_06_unknown_cpu_safe(self) -> None:
        pts, reason = classify_cpu(None)
        self.assertEqual(pts, 0)
        self.assertIsNone(reason)
        result = score_offer(price=220_000, gpu="RTX 5070 Ti", cpu=None)
        self.assertEqual(result.breakdown["cpu"], 0)

    # --- RAM ---
    def test_07_32_gt_16(self) -> None:
        self.assertGreater(score_ram(32)[0], score_ram(16)[0])

    def test_08_64_not_huge_skew(self) -> None:
        r64 = score_ram(64)[0]
        r32 = score_ram(32)[0]
        self.assertGreaterEqual(r64, r32)
        self.assertLessEqual(r64 - r32, 2.0)
        # 64GB alone cannot outweigh GPU gap 5080 vs 5070Ti (4 pts)
        gap = config.SCORE_GPU_5080 - config.SCORE_GPU_5070TI
        self.assertLess(r64 - r32, gap)

    # --- SSD ---
    def test_09_1tb_gt_512(self) -> None:
        self.assertGreater(score_ssd(1024)[0], score_ssd(512)[0])

    def test_10_2tb_slightly_gt_1tb(self) -> None:
        self.assertGreater(score_ssd(2048)[0], score_ssd(1024)[0])
        self.assertLessEqual(score_ssd(2048)[0] - score_ssd(1024)[0], 2.0)

    # --- SCREEN ---
    def test_11_screen_size_order(self) -> None:
        s18 = score_screen(18)[0]
        s17 = score_screen(17)[0]
        s16 = score_screen(16)[0]
        s156 = score_screen(15.6)[0]
        self.assertGreater(s18, s17)
        self.assertGreater(s17, s16)
        self.assertGreater(s16, s156)

    # --- HISTORY ---
    def test_12_at_min_max(self) -> None:
        pts, _ = score_historical_opportunity(200_000, 200_000)
        self.assertEqual(pts, config.SCORE_HISTORY_WITHIN_1PCT)

    def test_13_plus_3pct(self) -> None:
        pts, _ = score_historical_opportunity(206_000, 200_000)
        self.assertEqual(pts, config.SCORE_HISTORY_WITHIN_3PCT)

    def test_14_plus_5pct(self) -> None:
        pts, _ = score_historical_opportunity(210_000, 200_000)
        self.assertEqual(pts, config.SCORE_HISTORY_WITHIN_5PCT)

    def test_15_plus_10pct(self) -> None:
        pts, _ = score_historical_opportunity(220_000, 200_000)
        self.assertEqual(pts, config.SCORE_HISTORY_WITHIN_10PCT)

    def test_16_no_history_safe(self) -> None:
        pts, reason = score_historical_opportunity(220_000, None)
        self.assertEqual(pts, 0)
        self.assertIsNone(reason)
        result = score_offer(price=220_000, gpu="RTX 5070 Ti", historical_min=None)
        self.assertEqual(result.breakdown["history"], 0)

    # --- SAVING ---
    def test_17_saving_tiers(self) -> None:
        self.assertEqual(score_cross_store_saving(4_999)[0], 0)
        self.assertEqual(score_cross_store_saving(5_000)[0], 1)
        self.assertEqual(score_cross_store_saving(10_000)[0], 2)
        self.assertEqual(score_cross_store_saving(20_000)[0], 3)
        self.assertEqual(score_cross_store_saving(30_000)[0], 4)
        self.assertEqual(score_cross_store_saving(100_000)[0], config.SCORE_SAVING_MAX)

    # --- TOTAL / BREAKDOWN ---
    def test_18_total_0_100(self) -> None:
        result = score_offer(
            price=200_000,
            gpu="RTX 5080",
            cpu="Ryzen 9 8940HX",
            ram_gb=64,
            ssd_gb=2048,
            screen_inch=18,
            screen_resolution="2560x1600",
            historical_min=200_000,
            saving_vs_next=40_000,
        )
        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 100)

    def test_19_breakdown_sum_equals_total(self) -> None:
        result = score_offer(
            price=220_000,
            gpu="RTX 5070 Ti",
            cpu="Intel Core Ultra 7 255HX",
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=17,
            historical_min=210_000,
            saving_vs_next=12_000,
        )
        self.assertAlmostEqual(sum(result.breakdown.values()), result.score, places=2)

    # --- CONFIDENCE ---
    def test_20_full_specs_high_confidence(self) -> None:
        conf = compute_confidence(
            gpu="RTX 5070 Ti",
            cpu="HX",
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=16,
            historical_min=200_000,
        )
        self.assertEqual(conf, 100)

    def test_21_missing_cpu_history_lower(self) -> None:
        full = compute_confidence(
            gpu="RTX 5070 Ti",
            cpu="HX",
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=16,
            historical_min=200_000,
        )
        partial = compute_confidence(
            gpu="RTX 5070 Ti",
            cpu=None,
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=16,
            historical_min=None,
        )
        self.assertLess(partial, full)

    # --- RANKING SCENARIOS ---
    def test_22_expensive_5080_vs_value_5070ti(self) -> None:
        m5080 = _match(
            "ASUS 5080",
            [_offer(store="kns", external_id="a", price=299_000, product_id=1)],
            sku="A",
        )
        m5070 = _match(
            "Gigabyte 5070Ti",
            [_offer(store="kns", external_id="b", price=205_000, product_id=2)],
            sku="B",
        )
        specs = {
            ("kns", "a"): type(
                "S",
                (),
                {
                    "gpu": "RTX 5080",
                    "cpu": "Ryzen 9 8940HX",
                    "ram_gb": 32,
                    "ssd_gb": 1000,
                    "screen_size_inch": 16,
                    "screen_resolution": None,
                },
            )(),
            ("kns", "b"): type(
                "S",
                (),
                {
                    "gpu": "RTX 5070 Ti",
                    "cpu": "Ryzen 9 8940HX",
                    "ram_gb": 32,
                    "ssd_gb": 1000,
                    "screen_size_inch": 16,
                    "screen_resolution": None,
                },
            )(),
        }
        ranked = rank_clusters(
            [m5080, m5070],
            specs_by_key=specs,
            historical_mins={1: 290_000, 2: 204_000},
        )
        self.assertEqual(len(ranked), 2)
        # Value 5070 Ti near target/min should beat near-cap 5080.
        self.assertEqual(ranked[0].cluster_name, "Gigabyte 5070Ti")

    def test_23_cheaper_not_always_win(self) -> None:
        cheap_weak = _match(
            "Weak cheap",
            [_offer(store="kns", external_id="c", price=180_000, product_id=3)],
            sku="C",
        )
        strong = _match(
            "Strong",
            [_offer(store="kns", external_id="d", price=225_000, product_id=4)],
            sku="D",
        )
        specs = {
            ("kns", "c"): type(
                "S",
                (),
                {
                    "gpu": None,
                    "cpu": None,
                    "ram_gb": 16,
                    "ssd_gb": 512,
                    "screen_size_inch": 15.6,
                    "screen_resolution": None,
                },
            )(),
            ("kns", "d"): type(
                "S",
                (),
                {
                    "gpu": "RTX 5070 Ti",
                    "cpu": "Ryzen 9 8940HX",
                    "ram_gb": 32,
                    "ssd_gb": 1000,
                    "screen_size_inch": 17,
                    "screen_resolution": "2560x1600",
                },
            )(),
        }
        ranked = rank_clusters([cheap_weak, strong], specs_by_key=specs)
        self.assertEqual(ranked[0].cluster_name, "Strong")

    def test_24_hardware_alone_not_always_win(self) -> None:
        # Same as test_22 intent: hardware (5080) alone doesn't auto-win vs value.
        self.test_22_expensive_5080_vs_value_5070ti()

    def test_25_over_300k_never_ranked(self) -> None:
        match = _match("X", [_offer(price=301_000)])
        self.assertEqual(rank_clusters([match]), [])

    def test_26_stale_store_excluded(self) -> None:
        match = _match(
            "M",
            [
                _offer(store="kns", external_id="1", price=210_000, product_id=1),
                _offer(store="regard", external_id="2", price=200_000, product_id=2),
            ],
        )
        ranked = rank_clusters([match], fresh_stores={"kns"})
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].store, "kns")
        self.assertEqual(ranked[0].price, 210_000)

    def test_27_unavailable_excluded(self) -> None:
        match = _match(
            "M",
            [
                _offer(price=200_000, available=False),
                _offer(
                    store="regard",
                    external_id="2",
                    price=220_000,
                    available=True,
                    product_id=2,
                ),
            ],
        )
        ranked = rank_clusters([match])
        self.assertEqual(ranked[0].store, "regard")

    # --- HISTORY BULK ---
    def test_28_bulk_history_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            save_products(
                [
                    Product(
                        store="kns",
                        external_id="1",
                        url="u",
                        name="N",
                        sku="S",
                        price=220_000,
                        available=True,
                        checked_at=_now(),
                    )
                ],
                db,
            )
            with open_db(db) as conn:
                init_db(conn)
                before_p = count_products(conn)
                before_h = count_price_history(conn)
                row = get_product(conn, "kns", "1")
                assert row is not None
                pid = int(row["id"])
                conn.execute(
                    "INSERT INTO price_history (product_id, price, available, checked_at) "
                    "VALUES (?, ?, 1, ?)",
                    (pid, 210_000, (_now() - timedelta(days=3)).isoformat()),
                )
                conn.commit()
                after_insert_h = count_price_history(conn)
            mins = historical_mins_from_rows(
                {pid: [{"price": 210_000, "checked_at": _now().isoformat()}]}
            )
            self.assertEqual(mins[pid], 210_000)
            with open_db(db) as conn:
                self.assertEqual(count_products(conn), before_p)
                self.assertEqual(count_price_history(conn), after_insert_h)
                self.assertGreater(after_insert_h, before_h)

    # --- FORMAT ---
    def test_29_telegram_includes_score_confidence(self) -> None:
        deal = RankedDeal(
            score=84.0,
            reasons=["RTX 5070 Ti", "32GB RAM"],
            cluster_name="Gigabyte A16 Pro",
            gpu="RTX 5070 Ti",
            store="kns",
            price=220_485,
            url="https://example/x",
            ram_gb=32,
            ssd_gb=1000,
            screen_inch=16,
            confidence=92,
            score_breakdown={"gpu": 21},
        )
        text = format_top_deals_message([deal])
        self.assertIn("Оценка: 84/100", text)
        self.assertIn("Данные: 92%", text)
        self.assertIn("Почему:", text)

    def test_30_message_under_limit(self) -> None:
        deals = [
            RankedDeal(
                score=80 - i,
                reasons=["RTX 5070 Ti", "цена ниже target", "32GB RAM", "1TB SSD"],
                cluster_name=f"Model {i} Long Name Example",
                gpu="RTX 5070 Ti",
                store="kns",
                price=220_000 + i * 1000,
                url=f"https://example.test/item/{i}",
                ram_gb=32,
                ssd_gb=1000,
                screen_inch=16,
                confidence=83,
                saving_vs_next=7000,
                next_store="andpro",
            )
            for i in range(10)
        ]
        text = format_top_deals_message(deals, limit=10)
        self.assertLessEqual(len(text), config.TELEGRAM_TOP_SOFT_LIMIT)

    # --- N8N / ordering ---
    def test_31_top_deals_ordering_stable(self) -> None:
        matches = [
            _match(
                "Low",
                [_offer(store="kns", external_id="l", price=290_000, product_id=1)],
                sku="L",
            ),
            _match(
                "High",
                [_offer(store="kns", external_id="h", price=210_000, product_id=2)],
                sku="H",
            ),
        ]
        specs = {
            ("kns", "l"): type(
                "S",
                (),
                {
                    "gpu": "RTX 5070 Ti",
                    "cpu": None,
                    "ram_gb": 16,
                    "ssd_gb": 512,
                    "screen_size_inch": 15.6,
                    "screen_resolution": None,
                },
            )(),
            ("kns", "h"): type(
                "S",
                (),
                {
                    "gpu": "RTX 5070 Ti",
                    "cpu": "Ryzen 9 8940HX",
                    "ram_gb": 32,
                    "ssd_gb": 1000,
                    "screen_size_inch": 17,
                    "screen_resolution": None,
                },
            )(),
        }
        ranked = rank_clusters(matches, specs_by_key=specs)
        names = [d.cluster_name for d in ranked]
        self.assertEqual(names[0], "High")
        payload = [
            {"name": d.cluster_name, "price": d.price, "score": d.score}
            for d in ranked
        ]
        self.assertEqual(payload[0]["name"], ranked[0].cluster_name)

    def test_33_cluster_specs_from_sibling_offer(self) -> None:
        """Cheapest store may lack cache; sibling identity still supplies GPU."""
        match = _match(
            "Gigabyte A16",
            [
                _offer(
                    store="kns",
                    external_id="cheap",
                    price=256_000,
                    product_id=1,
                    name="Gigabyte A16 no gpu token",
                ),
                _offer(
                    store="regard",
                    external_id="rich",
                    price=270_000,
                    product_id=2,
                    name="Gigabyte A16 regard",
                ),
            ],
            sku="G",
        )
        specs = {
            ("regard", "rich"): type(
                "S",
                (),
                {
                    "gpu": "RTX 5080 LAPTOP",
                    "cpu": "INTEL CORE 7 240H",
                    "ram_gb": 32,
                    "ssd_gb": 1024,
                    "screen_size_inch": 16.0,
                    "screen_resolution": "2560x1600",
                },
            )(),
        }
        ranked = rank_clusters([match], specs_by_key=specs, fresh_stores={"kns", "regard"})
        self.assertEqual(ranked[0].store, "kns")
        self.assertIsNotNone(canonical_gpu(ranked[0].gpu))
        self.assertGreaterEqual(ranked[0].score, 50)

    def test_32_top_deals_prices_le_300k(self) -> None:
        matches = [
            _match("A", [_offer(price=299_999, product_id=1)], sku="A"),
            _match(
                "B",
                [_offer(store="regard", external_id="b", price=300_001, product_id=2)],
                sku="B",
            ),
        ]
        ranked = rank_clusters(matches)
        self.assertTrue(all((d.price or 0) <= 300_000 for d in ranked))
        self.assertTrue(all(d.cluster_name != "B" for d in ranked))


if __name__ == "__main__":
    unittest.main()
