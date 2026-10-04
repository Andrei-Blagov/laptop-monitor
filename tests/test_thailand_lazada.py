from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from thailand.lazada import (
    extract_list_items_from_html,
    parse_lazada_listing,
    parse_lazada_payload,
)
from thailand.seller_trust import TIER_A, TIER_B, TIER_C, classify_seller_trust
from thailand.verification import UNVERIFIED, VERIFIED


def _raw(**kwargs):
    base = {
        "itemId": "123456789",
        "skuId": "sku-1",
        "name": (
            "ASUS ROG Strix G16 G614PR-TS113W RTX 5070 Ti "
            "Ryzen 9 32GB 1TB 16 inch"
        ),
        "price": 84990,
        "listed_price": 89990,
        "url": "https://www.lazada.co.th/products/asus-i123456789-ssku-1.html",
        "seller_name": "ASUS Official Store",
        "seller_id": "1001",
        "official_store": True,
        "mall": True,
        "seller_rating": 4.9,
        "seller_reviews_count": 1200,
        "units_sold": 350,
        "available": True,
        "availability_status": "in_stock",
        "gpu": "RTX 5070 Ti",
        "cpu": "AMD Ryzen 9 8940HX",
        "ram_gb": 32,
        "ssd_gb": 1024,
        "screen_size_inch": 16.0,
        "price_verified_for_variant": True,
    }
    base.update(kwargs)
    return base


class LazadaParseTests(unittest.TestCase):
    def test_parse_5070_ti(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertIsNotNone(o)
        self.assertEqual(o.gpu, "RTX 5070 Ti")
        self.assertTrue(o.marketplace)

    def test_parse_5080(self) -> None:
        o = parse_lazada_listing(
            _raw(
                itemId="2",
                name="ASUS ROG Strix G18 RTX 5080 32GB 1TB",
                gpu="RTX 5080",
                price=127990,
            )
        )
        self.assertEqual(o.gpu, "RTX 5080")

    def test_exclude_5070_non_ti(self) -> None:
        o = parse_lazada_listing(
            _raw(name="Laptop RTX 5070 16GB", gpu="RTX 5070", price=70000)
        )
        self.assertIsNone(o)

    def test_exclude_5060(self) -> None:
        self.assertIsNone(
            parse_lazada_listing(
                _raw(name="Gaming RTX 5060 Laptop", gpu="RTX 5060", price=40000)
            )
        )

    def test_exclude_5090(self) -> None:
        self.assertIsNone(
            parse_lazada_listing(
                _raw(name="RTX 5090 Laptop", gpu="RTX 5090", price=200000)
            )
        )

    def test_exclude_desktop_gpu(self) -> None:
        self.assertIsNone(
            parse_lazada_listing(
                _raw(
                    name="NVIDIA GeForce RTX 5080 Desktop Graphics Card",
                    gpu="RTX 5080",
                    price=50000,
                )
            )
        )

    def test_accessory_false_positive(self) -> None:
        self.assertIsNone(
            parse_lazada_listing(
                _raw(
                    name="Cooling pad for RTX 5080 laptop",
                    gpu="RTX 5080",
                    price=990,
                )
            )
        )

    def test_seller_name(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertEqual(o.seller_name, "ASUS Official Store")

    def test_official_mall_flags(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertTrue(o.official_store)
        self.assertTrue(o.mall)

    def test_rating_reviews_sold(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertEqual(o.seller_rating, 4.9)
        self.assertEqual(o.seller_reviews_count, 1200)
        self.assertEqual(o.units_sold, 350)

    def test_listed_and_sale_price(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertEqual(o.price_thb, 84990)
        self.assertEqual(o.regular_price_thb, 89990)

    def test_personalized_voucher_not_applied(self) -> None:
        o = parse_lazada_listing(
            _raw(price=84990, voucher_text="New user -2000", promo_notes="coins")
        )
        self.assertEqual(o.price_thb, 84990)
        self.assertIn("New user", o.voucher_text or "")

    def test_variant_exact_price_verified(self) -> None:
        o = parse_lazada_listing(_raw(price_verified_for_variant=True))
        self.assertTrue(o.price_verified_for_variant)
        self.assertEqual(o.verification_status, VERIFIED)

    def test_from_price_rejected(self) -> None:
        o = parse_lazada_listing(_raw(from_price=True, price=50000))
        self.assertFalse(o.price_verified_for_variant)
        self.assertNotEqual(o.verification_status, VERIFIED)

    def test_multi_variant_mismatch(self) -> None:
        o = parse_lazada_listing(
            _raw(
                name="ASUS ROG Strix Gaming Laptop RTX 5080 32GB",
                gpu="RTX 5080",
                selected_variant_gpu="RTX 5070 Ti",
                price_verified_for_variant=False,
            )
        )
        self.assertEqual(o.gpu, "RTX 5070 Ti")
        self.assertNotEqual(o.verification_status, VERIFIED)

    def test_availability(self) -> None:
        o = parse_lazada_listing(_raw(availability_status="out of stock", available=False))
        self.assertEqual(o.availability_status, "out_of_stock")

    def test_direct_product_url(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertIn("/products/", o.url)
        self.assertNotIn("/catalog/", o.url)

    def test_unknown_seller_tier_c(self) -> None:
        o = parse_lazada_listing(
            _raw(
                seller_name="random shop xyz",
                official_store=False,
                mall=False,
                seller_rating=None,
                seller_reviews_count=None,
                units_sold=None,
            )
        )
        self.assertEqual(classify_seller_trust(o), TIER_C)

    def test_official_tier_a(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertEqual(o.seller_trust_tier, TIER_A)

    def test_banana_it_lazada(self) -> None:
        o = parse_lazada_listing(
            _raw(seller_name="BaNANA IT", official_store=False, mall=False)
        )
        self.assertEqual(classify_seller_trust(o), TIER_A)

    def test_jib_lazada(self) -> None:
        o = parse_lazada_listing(
            _raw(seller_name="JIB Computer Group", official_store=False, mall=True)
        )
        self.assertEqual(o.seller_trust_tier, TIER_A)

    def test_html_list_items_extract(self) -> None:
        html = (
            '<script>window.pageData = {"mods":{"listItems":['
            '{"itemId":"9","name":"ASUS ROG RTX 5070 Ti 32GB 1TB",'
            '"price":"84990","sellerName":"ASUS Official Store",'
            '"isOfficial":true,"isMall":true,"productUrl":'
            '"https://www.lazada.co.th/products/x-i9.html"}'
            "]}};</script>"
        )
        items = extract_list_items_from_html(html)
        self.assertEqual(len(items), 1)
        offers = parse_lazada_payload(items)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].store, "lazada")


class LazadaTrustTopTests(unittest.TestCase):
    def test_tier_c_excluded_from_top(self) -> None:
        from thailand.scanner import build_thai_top
        from thailand.models import FxRate

        o = parse_lazada_listing(
            _raw(
                seller_name="unknown tiny shop",
                official_store=False,
                mall=False,
                seller_rating=None,
                seller_reviews_count=None,
                units_sold=None,
            )
        )
        assert o is not None
        fx = FxRate(
            source="CBR",
            currency="THB",
            nominal=10,
            official_rate=25.0,
            rub_per_thb=2.5,
            published_date="04.10.2026",
            fetched_at=datetime.now(timezone.utc),
        )
        self.assertEqual(build_thai_top([o], fx=fx), [])

    def test_marketplace_confidence_separate(self) -> None:
        o = parse_lazada_listing(_raw())
        self.assertIsNotNone(o.marketplace_confidence)
        # hardware score is computed later in scanner; confidence is seller-side
        self.assertGreaterEqual(o.marketplace_confidence, 90.0)


if __name__ == "__main__":
    unittest.main()
