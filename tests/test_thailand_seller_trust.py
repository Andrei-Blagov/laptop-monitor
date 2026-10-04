from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.eligibility import apply_near_price_trust_preference
from thailand.lazada import parse_lazada_listing
from thailand.matching import group_thai_offers
from thailand.models import ThailandOffer
from thailand.seller_trust import (
    TIER_A,
    TIER_B,
    TIER_C,
    classify_seller_trust,
    marketplace_confidence,
    store_display_label,
)
from thailand.verification import (
    GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
    compute_verification,
)


def _direct(**kwargs) -> ThailandOffer:
    base = dict(
        store="banana",
        external_id="d1",
        name="ASUS ROG Strix G16 G614PR-TS113W RTX 5070 Ti",
        url="https://www.bnn.in.th/th/p/d1",
        price_thb=84990,
        available=True,
        availability_status="in_stock",
        availability_confirmed=True,
        gpu="RTX 5070 Ti",
        gpu_source=GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
        sku="G614PR-TS113W",
        manufacturer_part_number="G614PR-TS113W",
        collected_at=datetime.now(timezone.utc),
        marketplace=False,
    )
    base.update(kwargs)
    return compute_verification(ThailandOffer(**base))


class SellerTrustTests(unittest.TestCase):
    def test_official_store_tier_a(self) -> None:
        o = parse_lazada_listing(
            {
                "itemId": "1",
                "name": "ASUS ROG Strix G16 RTX 5070 Ti 32GB 1TB",
                "price": 85000,
                "gpu": "RTX 5070 Ti",
                "seller_name": "Shop",
                "official_store": True,
                "available": True,
                "availability_status": "in_stock",
            }
        )
        self.assertEqual(classify_seller_trust(o), TIER_A)

    def test_known_retailer_tier_a(self) -> None:
        o = parse_lazada_listing(
            {
                "itemId": "2",
                "name": "ASUS ROG Strix G16 RTX 5070 Ti 32GB 1TB",
                "price": 85000,
                "gpu": "RTX 5070 Ti",
                "seller_name": "JIB Computer Group",
                "official_store": False,
                "mall": False,
                "available": True,
                "availability_status": "in_stock",
            }
        )
        self.assertEqual(classify_seller_trust(o), TIER_A)

    def test_strong_established_tier_b(self) -> None:
        o = parse_lazada_listing(
            {
                "itemId": "3",
                "name": "MSI Vector RTX 5070 Ti 32GB 1TB",
                "price": 82000,
                "gpu": "RTX 5070 Ti",
                "seller_name": "Tech World TH",
                "official_store": False,
                "mall": False,
                "seller_rating": 4.8,
                "seller_reviews_count": 200,
                "units_sold": 80,
                "available": True,
                "availability_status": "in_stock",
            }
        )
        self.assertEqual(classify_seller_trust(o), TIER_B)

    def test_unknown_tier_c(self) -> None:
        o = parse_lazada_listing(
            {
                "itemId": "4",
                "name": "MSI Vector RTX 5070 Ti 32GB 1TB",
                "price": 80000,
                "gpu": "RTX 5070 Ti",
                "seller_name": "tiny unknown",
                "official_store": False,
                "mall": False,
                "available": True,
                "availability_status": "in_stock",
            }
        )
        self.assertEqual(classify_seller_trust(o), TIER_C)

    def test_official_wins_near_price_tie(self) -> None:
        rows = [
            {
                "name": "unknown",
                "price_thb": 84000,
                "price_rub": 210000,
                "international_score": 70,
                "marketplace": True,
                "seller_trust_tier": TIER_C,
                "official_store": False,
                "mall": False,
            },
            {
                "name": "official",
                "price_thb": 85000,
                "price_rub": 212500,
                "international_score": 70,
                "marketplace": True,
                "seller_trust_tier": TIER_A,
                "official_store": True,
                "mall": True,
            },
        ]
        # Value-sort would keep unknown first by cheaper price; trust preference reorders.
        out = apply_near_price_trust_preference(rows, near_thb=2000)
        self.assertEqual(out[0]["name"], "official")

    def test_marketplace_confidence_not_hardware(self) -> None:
        o = parse_lazada_listing(
            {
                "itemId": "5",
                "name": "ASUS ROG RTX 5080 32GB 1TB",
                "price": 120000,
                "gpu": "RTX 5080",
                "seller_name": "ASUS Official Store",
                "official_store": True,
                "available": True,
                "availability_status": "in_stock",
            }
        )
        conf = marketplace_confidence(o)
        self.assertGreaterEqual(conf, 90)
        self.assertEqual(store_display_label(o), "Lazada · ASUS Official Store")

    def test_cross_channel_duplicate_prefers_direct(self) -> None:
        direct = _direct(price_thb=84990)
        laz = parse_lazada_listing(
            {
                "itemId": "9",
                "name": "ASUS ROG Strix G16 G614PR-TS113W RTX 5070 Ti 32GB",
                "price": 88990,
                "gpu": "RTX 5070 Ti",
                "mpn": "G614PR-TS113W",
                "skuId": "G614PR-TS113W",
                "seller_name": "BaNANA IT",
                "available": True,
                "availability_status": "in_stock",
            }
        )
        assert laz is not None
        laz.sku = "G614PR-TS113W"
        laz.manufacturer_part_number = "G614PR-TS113W"
        groups = group_thai_offers([direct, laz])
        self.assertEqual(len(groups), 1)
        self.assertIsNotNone(groups[0].best)
        self.assertFalse(groups[0].best.marketplace)
        self.assertEqual(groups[0].best.store, "banana")


if __name__ == "__main__":
    unittest.main()
