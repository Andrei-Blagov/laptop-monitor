from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from thailand.eligibility import sort_cheapest_rows, sort_value_rows
from thailand.formatting import format_thailand_alternatives_message, format_thailand_comparison_message
from thailand.grouping import (
    config_group_key,
    dedupe_user_facing_rows,
    select_best_and_alts,
)
from thailand.lazada import LAZADA_SEARCH_PLAN, _dedupe_offers, parse_lazada_listing
from thailand.models import FxRate, StoreScanResult, ThailandOffer
from thailand.scanner import (
    build_cheapest_eligible,
    build_thai_top,
    build_value_ranked_eligible,
)
from thailand.seller_trust import (
    TIER_A,
    TIER_C,
    annotate_marketplace_identity,
    classify_seller_trust,
    is_banana_it_seller,
    store_display_label,
)
from thailand.verification import (
    GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
    compute_verification,
)


def _fx(rub_per_thb: float = 2.5) -> FxRate:
    return FxRate(
        source="CBR",
        currency="THB",
        nominal=10,
        official_rate=rub_per_thb * 10,
        rub_per_thb=rub_per_thb,
        published_date="04.10.2026",
        fetched_at=datetime.now(timezone.utc),
    )


def _direct(**kwargs) -> ThailandOffer:
    base = dict(
        store="jib",
        external_id="jib-1",
        name="ASUS ROG Strix G16 G614PR-TS113W RTX 5070 Ti",
        url="https://www.jib.co.th/1",
        price_thb=84_990,
        available=True,
        availability_status="in_stock",
        availability_confirmed=True,
        gpu="RTX 5070 Ti",
        gpu_source=GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
        cpu="AMD Ryzen 9 8940HX",
        ram_gb=32,
        ssd_gb=1024,
        screen_size_inch=16.0,
        sku="G614PR-TS113W",
        manufacturer_part_number="G614PR-TS113W",
        channel="direct",
        collected_at=datetime.now(timezone.utc),
    )
    base.update(kwargs)
    return compute_verification(ThailandOffer(**base))


def _lazada_raw(**kwargs):
    base = {
        "itemId": "123456789",
        "skuId": "sku-1",
        "name": (
            "ASUS ROG Strix G16 G614PR-TS113W RTX 5070 Ti "
            "Ryzen 9 32GB 1TB 16 inch"
        ),
        "price": 84990,
        "url": "https://www.lazada.co.th/products/asus-i123456789-ssku-1.html",
        "seller_name": "ASUS Official Store",
        "official_store": True,
        "mall": True,
        "available": True,
        "availability_status": "in_stock",
        "gpu": "RTX 5070 Ti",
        "cpu": "AMD Ryzen 9 8940HX",
        "ram_gb": 32,
        "ssd_gb": 1024,
        "screen_size_inch": 16.0,
        "mpn": "G614PR-TS113W",
        "price_verified_for_variant": True,
    }
    base.update(kwargs)
    return base


class SortingCorrectiveTests(unittest.TestCase):
    def test_210k_score70_beats_250k_score90(self) -> None:
        cheap = _direct(external_id="c", price_thb=84_000)  # 210k RUB
        dear = _direct(
            external_id="d",
            price_thb=100_000,
            name="ASUS ROG Strix G18 RTX 5080",
            gpu="RTX 5080",
            sku="G814-5080",
            manufacturer_part_number="G814-5080",
        )  # 250k RUB, higher value score
        top = build_thai_top([dear, cheap], fx=_fx(2.5))
        self.assertEqual(top[0]["external_id"], "c")
        self.assertLess(top[0]["international_score"], top[1]["international_score"])
        self.assertLess(top[0]["price_rub"], top[1]["price_rub"])

    def test_same_price_higher_score_first(self) -> None:
        a = _direct(
            external_id="5070",
            price_thb=90_000,
            gpu="RTX 5070 Ti",
            sku="A-5070",
            manufacturer_part_number="A-5070",
        )
        b = _direct(
            external_id="5080",
            price_thb=90_000,
            name="ASUS ROG Strix G18 RTX 5080",
            gpu="RTX 5080",
            sku="B-5080",
            manufacturer_part_number="B-5080",
        )
        top = build_thai_top([a, b], fx=_fx(2.5))
        self.assertEqual(top[0]["price_rub"], top[1]["price_rub"])
        self.assertEqual(top[0]["external_id"], "5080")
        self.assertGreater(
            top[0]["international_score"], top[1]["international_score"]
        )

    def test_over_cap_excluded(self) -> None:
        # 2.5 RUB/THB: 132 000 THB = 330 000 RUB (in), 132 001 THB is over.
        ok = _direct(external_id="ok", price_thb=132_000)
        bad = _direct(
            external_id="bad",
            price_thb=132_001,
            sku="OVER",
            manufacturer_part_number="OVER",
        )
        top = build_thai_top([ok, bad], fx=_fx(2.5))
        ids = {r["external_id"] for r in top}
        self.assertIn("ok", ids)
        self.assertNotIn("bad", ids)

    def test_cheapest_list_strict_rub_asc(self) -> None:
        offers = [
            _direct(
                external_id="c",
                price_thb=100_000,
                sku="MODEL-C-100",
                manufacturer_part_number="MODEL-C-100",
                name="ASUS Model C RTX 5070 Ti",
            ),
            _direct(
                external_id="a",
                price_thb=70_000,
                sku="MODEL-A-100",
                manufacturer_part_number="MODEL-A-100",
                name="ASUS Model A RTX 5070 Ti",
            ),
            _direct(
                external_id="b",
                price_thb=85_000,
                sku="MODEL-B-100",
                manufacturer_part_number="MODEL-B-100",
                name="ASUS Model B RTX 5070 Ti",
            ),
        ]
        rows = build_cheapest_eligible(offers, fx=_fx(2.5))
        rubs = [r["price_rub"] for r in rows]
        self.assertEqual(rubs, sorted(rubs))
        self.assertEqual([r["external_id"] for r in rows], ["a", "b", "c"])

    def test_value_list_still_score_desc(self) -> None:
        cheap = _direct(
            external_id="c",
            price_thb=70_000,
            gpu="RTX 5070 Ti",
            sku="C",
            manufacturer_part_number="C",
        )
        dear = _direct(
            external_id="d",
            price_thb=100_000,
            name="ASUS ROG Strix G18 RTX 5080",
            gpu="RTX 5080",
            sku="D",
            manufacturer_part_number="D",
        )
        value = build_value_ranked_eligible([cheap, dear], fx=_fx(2.5))
        self.assertEqual(value[0]["external_id"], "d")
        self.assertGreater(
            value[0]["international_score"], value[1]["international_score"]
        )
        # sort helpers stay distinct
        rows = [
            {"price_rub": 210_000, "international_score": 70, "international_confidence": 50},
            {"price_rub": 250_000, "international_score": 90, "international_confidence": 80},
        ]
        self.assertEqual(sort_cheapest_rows(rows)[0]["price_rub"], 210_000)
        self.assertEqual(sort_value_rows(rows)[0]["international_score"], 90)

    def test_no_duplicate_same_model_in_telegram(self) -> None:
        jib = _direct(external_id="jib-g614", price_thb=84_990)
        laz = parse_lazada_listing(
            _lazada_raw(
                itemId="999",
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
                price=86_990,
            )
        )
        assert laz is not None
        laz.sku = "G614PR-TS113W"
        laz.manufacturer_part_number = "G614PR-TS113W"
        top = build_thai_top([jib, laz], fx=_fx(2.5))
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["store"], "jib")
        text = format_thailand_alternatives_message(top, price_cap={"over_cap_count": 0})
        # One numbered position only
        self.assertEqual(text.count("\n1. "), 1)
        self.assertNotIn("\n2. ", text)


class BananaViaLazadaTests(unittest.TestCase):
    def test_banana_it_recognized(self) -> None:
        self.assertTrue(is_banana_it_seller("BaNANA IT"))
        self.assertTrue(is_banana_it_seller("Banana IT"))
        self.assertTrue(is_banana_it_seller("BNN"))

    def test_tier_a_channel_brand_not_direct(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
            )
        )
        assert o is not None
        annotate_marketplace_identity(o)
        self.assertEqual(classify_seller_trust(o), TIER_A)
        self.assertEqual(o.channel, "lazada")
        self.assertEqual(o.retailer_brand, "BaNANA")
        self.assertTrue(o.marketplace)
        self.assertNotEqual(store_display_label(o), "BaNANA")
        self.assertEqual(store_display_label(o), "Lazada · BaNANA IT")

    def test_appears_in_top_if_eligible(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
                price=80_000,
            )
        )
        assert o is not None
        top = build_thai_top([o], fx=_fx(2.5))
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["store_label"], "Lazada · BaNANA IT")
        self.assertEqual(top[0]["retailer_brand"], "BaNANA")
        self.assertEqual(top[0]["channel"], "lazada")

    def test_excluded_if_over_cap(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
                price=132_001,
            )
        )
        assert o is not None
        self.assertEqual(build_thai_top([o], fx=_fx(2.5)), [])

    def test_variant_unverified_excluded(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
                price_verified_for_variant=False,
            )
        )
        assert o is not None
        self.assertEqual(build_thai_top([o], fx=_fx(2.5)), [])

    def test_status_line_banana_via_lazada(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(seller_name="BaNANA IT", official_store=False, mall=True)
        )
        text = format_thailand_comparison_message(
            store_results=[
                StoreScanResult(store="jib", ok=True, offers=[]),
                StoreScanResult(store="advice", ok=False, error="timeout", offers=[]),
                StoreScanResult(store="banana", ok=False, error="Cloudflare", offers=[]),
                StoreScanResult(
                    store="lazada", ok=True, offers=[o] if o else []
                ),
            ],
            fx=_fx(2.5),
            match=None,
            comparison=None,
        )
        self.assertIn("BaNANA — временно недоступен", text)
        self.assertNotIn("Cloudflare", text)
        self.assertNotIn("HTTP_403", text)
        self.assertIn("Lazada ✅", text)
        self.assertIn("BaNANA IT через Lazada ✅", text)


class TargetedLazadaTests(unittest.TestCase):
    def test_targeted_seller_queries_in_plan(self) -> None:
        queries = [q for q, _ in LAZADA_SEARCH_PLAN]
        joined = " | ".join(queries)
        self.assertIn("BaNANA IT", joined)
        self.assertIn("JIB", joined)
        self.assertIn("ASUS Official", joined)
        self.assertTrue(any(h for _, h in LAZADA_SEARCH_PLAN))

    def test_duplicate_listing_deduped(self) -> None:
        a = parse_lazada_listing(_lazada_raw(itemId="111", skuId="s1", price=84990))
        b = parse_lazada_listing(
            _lazada_raw(
                itemId="111",
                skuId="s1",
                price=83990,
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
            )
        )
        assert a and b
        a.metadata["discovery_query"] = "RTX 5070 Ti notebook"
        b.metadata["discovery_query"] = "BaNANA IT RTX 5070 Ti notebook"
        deduped = _dedupe_offers([a, b])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].price_thb, 83990)

    def test_query_seller_hint_does_not_override_actual_seller(self) -> None:
        # Even if discovered via BaNANA IT query, listing seller wins.
        o = parse_lazada_listing(
            _lazada_raw(seller_name="Random Shop XYZ", official_store=False, mall=False)
        )
        assert o is not None
        o.metadata["discovery_query"] = "BaNANA IT RTX 5070 Ti notebook"
        annotate_marketplace_identity(o)
        self.assertEqual(o.seller_name, "Random Shop XYZ")
        self.assertIsNone(o.retailer_brand)
        self.assertNotEqual(store_display_label(o), "Lazada · BaNANA IT")

    def test_query_gpu_does_not_override_actual_gpu(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(
                name="ASUS ROG Strix G16 G614PR RTX 5070 Ti 32GB 1TB",
                gpu="RTX 5070 Ti",
            ),
            catalog_gpu="RTX 5080",
        )
        assert o is not None
        self.assertEqual(o.gpu, "RTX 5070 Ti")

    def test_official_seller_evidence_retained(self) -> None:
        o = parse_lazada_listing(_lazada_raw())
        assert o is not None
        self.assertTrue(o.official_store)
        self.assertTrue(o.mall)
        self.assertEqual(o.seller_trust_tier, TIER_A)

    def test_unknown_seller_still_tier_c(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(
                seller_name="tiny unknown booth",
                official_store=False,
                mall=False,
                seller_rating=None,
                seller_reviews_count=None,
                units_sold=None,
            )
        )
        assert o is not None
        self.assertEqual(classify_seller_trust(o), TIER_C)


class CrossChannelGroupingTests(unittest.TestCase):
    def test_direct_and_marketplace_same_mpn_grouped(self) -> None:
        jib = _direct(price_thb=84_990)
        laz = parse_lazada_listing(
            _lazada_raw(
                seller_name="BaNANA IT",
                official_store=False,
                mall=True,
                price=86_990,
            )
        )
        assert laz is not None
        laz.sku = "G614PR-TS113W"
        laz.manufacturer_part_number = "G614PR-TS113W"
        rows = [
            {
                "store": jib.store,
                "external_id": jib.external_id,
                "name": jib.name,
                "gpu": jib.gpu,
                "cpu": jib.cpu,
                "ram_gb": jib.ram_gb,
                "ssd_gb": jib.ssd_gb,
                "screen_size_inch": jib.screen_size_inch,
                "sku": jib.sku,
                "manufacturer_part_number": jib.manufacturer_part_number,
                "price_thb": jib.price_thb,
                "price_rub": 212_475,
                "marketplace": False,
                "channel": "direct",
                "seller_name": None,
                "seller_trust_tier": TIER_A,
                "international_score": 80,
                "international_confidence": 90,
                "url": jib.url,
            },
            {
                "store": laz.store,
                "external_id": laz.external_id,
                "name": laz.name,
                "gpu": laz.gpu,
                "cpu": laz.cpu,
                "ram_gb": laz.ram_gb,
                "ssd_gb": laz.ssd_gb,
                "screen_size_inch": laz.screen_size_inch,
                "sku": laz.sku,
                "manufacturer_part_number": laz.manufacturer_part_number,
                "price_thb": laz.price_thb,
                "price_rub": 217_475,
                "marketplace": True,
                "channel": "lazada",
                "retailer_brand": "BaNANA",
                "seller_name": "BaNANA IT",
                "seller_trust_tier": TIER_A,
                "mall": True,
                "international_score": 78,
                "international_confidence": 85,
                "url": laz.url,
                "store_label": "Lazada · BaNANA IT",
            },
        ]
        self.assertEqual(config_group_key(rows[0]), config_group_key(rows[1]))
        deduped, removed = dedupe_user_facing_rows(rows)
        self.assertEqual(removed, 1)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["store"], "jib")

    def test_cheapest_wins_meaningful_price_diff(self) -> None:
        # Marketplace meaningfully cheaper (>2%) → marketplace wins, label preserved
        best = select_best_and_alts(
            [
                {
                    "store": "jib",
                    "external_id": "d1",
                    "price_rub": 220_000,
                    "price_thb": 88_000,
                    "marketplace": False,
                    "channel": "direct",
                    "seller_name": None,
                    "seller_trust_tier": TIER_A,
                    "international_score": 80,
                    "international_confidence": 90,
                    "manufacturer_part_number": "G614PR-TS113W",
                    "url": "https://jib/1",
                },
                {
                    "store": "lazada",
                    "external_id": "m1",
                    "price_rub": 200_000,
                    "price_thb": 80_000,
                    "marketplace": True,
                    "channel": "lazada",
                    "retailer_brand": "BaNANA",
                    "seller_name": "BaNANA IT",
                    "seller_trust_tier": TIER_A,
                    "mall": True,
                    "store_label": "Lazada · BaNANA IT",
                    "international_score": 82,
                    "international_confidence": 88,
                    "manufacturer_part_number": "G614PR-TS113W",
                    "url": "https://lazada/1",
                },
            ]
        )
        self.assertEqual(best["store"], "lazada")
        self.assertEqual(best["store_label"], "Lazada · BaNANA IT")
        self.assertEqual(best["seller_name"], "BaNANA IT")

    def test_direct_wins_near_price_tie(self) -> None:
        best = select_best_and_alts(
            [
                {
                    "store": "lazada",
                    "external_id": "m1",
                    "price_rub": 212_000,
                    "price_thb": 84_800,
                    "marketplace": True,
                    "channel": "lazada",
                    "seller_name": "BaNANA IT",
                    "retailer_brand": "BaNANA",
                    "seller_trust_tier": TIER_A,
                    "mall": True,
                    "store_label": "Lazada · BaNANA IT",
                    "international_score": 80,
                    "international_confidence": 85,
                    "manufacturer_part_number": "G614PR-TS113W",
                    "url": "https://lazada/1",
                },
                {
                    "store": "jib",
                    "external_id": "d1",
                    "price_rub": 212_475,
                    "price_thb": 84_990,
                    "marketplace": False,
                    "channel": "direct",
                    "seller_name": None,
                    "seller_trust_tier": TIER_A,
                    "international_score": 80,
                    "international_confidence": 90,
                    "manufacturer_part_number": "G614PR-TS113W",
                    "url": "https://jib/1",
                },
            ]
        )
        self.assertEqual(best["store"], "jib")
        self.assertFalse(best.get("marketplace"))

    def test_marketplace_seller_label_preserved(self) -> None:
        o = parse_lazada_listing(
            _lazada_raw(seller_name="BaNANA IT", official_store=False, mall=True)
        )
        assert o is not None
        top = build_thai_top([o], fx=_fx(2.5))
        self.assertEqual(top[0]["store_label"], "Lazada · BaNANA IT")
        text = format_thailand_alternatives_message(top)
        self.assertIn("Lazada · BaNANA IT", text)
        self.assertNotIn("\nBaNANA\n", text)

    def test_alternative_channels_max2(self) -> None:
        rows = [
            {
                "store": "jib",
                "external_id": "d1",
                "price_rub": 210_000,
                "price_thb": 84_000,
                "marketplace": False,
                "channel": "direct",
                "seller_name": None,
                "seller_trust_tier": TIER_A,
                "international_score": 80,
                "international_confidence": 90,
                "manufacturer_part_number": "G614PR-TS113W",
                "url": "https://jib/1",
            },
            {
                "store": "lazada",
                "external_id": "m1",
                "price_rub": 211_000,
                "price_thb": 84_400,
                "marketplace": True,
                "channel": "lazada",
                "seller_name": "BaNANA IT",
                "retailer_brand": "BaNANA",
                "seller_trust_tier": TIER_A,
                "store_label": "Lazada · BaNANA IT",
                "international_score": 79,
                "international_confidence": 85,
                "manufacturer_part_number": "G614PR-TS113W",
                "url": "https://lazada/1",
            },
            {
                "store": "lazada",
                "external_id": "m2",
                "price_rub": 212_000,
                "price_thb": 84_800,
                "marketplace": True,
                "channel": "lazada",
                "seller_name": "JIB Computer Group",
                "retailer_brand": "JIB",
                "seller_trust_tier": TIER_A,
                "store_label": "Lazada · JIB Computer Group",
                "international_score": 78,
                "international_confidence": 85,
                "manufacturer_part_number": "G614PR-TS113W",
                "url": "https://lazada/2",
            },
            {
                "store": "lazada",
                "external_id": "m3",
                "price_rub": 213_000,
                "price_thb": 85_200,
                "marketplace": True,
                "channel": "lazada",
                "seller_name": "ASUS Official Store",
                "seller_trust_tier": TIER_A,
                "official_store": True,
                "store_label": "Lazada · ASUS Official Store",
                "international_score": 77,
                "international_confidence": 85,
                "manufacturer_part_number": "G614PR-TS113W",
                "url": "https://lazada/3",
            },
        ]
        best = select_best_and_alts(rows)
        self.assertEqual(best["store"], "jib")
        self.assertEqual(len(best["alt_channels"]), 2)


if __name__ == "__main__":
    unittest.main()
