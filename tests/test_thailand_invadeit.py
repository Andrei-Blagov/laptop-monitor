from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.eligibility import offer_user_facing_eligible
from thailand.fx import FxRate
from thailand.invadeit import (
    enrich_offer_from_specifications,
    parse_invadeit_item,
    product_url,
)
from thailand.verification import is_verified_for_ranking


def _item(**overrides) -> dict:
    base = {
        "id": 81984,
        "name": "ROG Strix G16 G614PR-TS113W Eclipse Gray",
        "productNumber": "ASUS-G614PR-TS113W",
        "urlName": "rog-strix-g16-g614pr",
        "price": 99990,
        "salePrice": None,
        "beforePrice": None,
        "onSale": False,
        "inStock": True,
        "keySpecs": (
            '16" 2.5K 300Hz / AMD Ryzen 9 8940HX\n'
            "16GB DDR5 / 1TB SSD\n"
            "GeForce RTX 5070 Ti 12GB GDDR7"
        ),
        "manufacturerName": "ASUS",
        "manufacturerUrlName": "asus",
        "categoryName": "Notebooks / Laptops",
        "categoryUrlName": "notebooks-laptops",
    }
    base.update(overrides)
    return base


def _fx() -> FxRate:
    return FxRate(
        source="test",
        currency="THB",
        nominal=1,
        official_rate=1.0,
        rub_per_thb=1.0,
        published_date=None,
        fetched_at=datetime.now(timezone.utc),
    )


class InvadeItParseTests(unittest.TestCase):
    def test_structured_payload_parse(self) -> None:
        offer = parse_invadeit_item(_item())
        assert offer is not None
        self.assertEqual(offer.store, "invadeit")
        self.assertIn("/product/notebooks-laptops/asus/rog-strix-g16-g614pr-p081984/", product_url(_item()))

    def test_rtx_5070_ti(self) -> None:
        offer = parse_invadeit_item(_item())
        assert offer is not None
        self.assertEqual(offer.gpu, "RTX 5070 Ti")

    def test_rtx_5080(self) -> None:
        offer = parse_invadeit_item(
            _item(keySpecs='16" / Ryzen 9\n32GB DDR5 / 1TB SSD\nGeForce RTX 5080 16GB GDDR7', price=129990)
        )
        assert offer is not None
        self.assertEqual(offer.gpu, "RTX 5080")

    def test_target_filtering_excludes_5070_and_desktop(self) -> None:
        plain = parse_invadeit_item(_item(keySpecs="GeForce RTX 5070 8GB GDDR7\n16GB DDR5"))
        desktop = parse_invadeit_item(
            _item(categoryName="Video Cards", keySpecs="GeForce RTX 5070 Ti | 16GB GDDR7")
        )
        self.assertIsNone(plain)
        self.assertIsNone(desktop)

    def test_specs(self) -> None:
        offer = parse_invadeit_item(_item())
        assert offer is not None
        self.assertEqual(offer.ram_gb, 16)
        self.assertEqual(offer.ssd_gb, 1024)
        self.assertIn("Ryzen 9", offer.cpu or "")

    def test_price_and_public_sale(self) -> None:
        offer = parse_invadeit_item(_item(price=99990, salePrice=89990, onSale=True, beforePrice=99990))
        assert offer is not None
        self.assertEqual(offer.price_thb, 89990)
        self.assertEqual(offer.regular_price_thb, 99990)

    def test_availability(self) -> None:
        instock = parse_invadeit_item(_item(inStock=True))
        out = parse_invadeit_item(_item(inStock=False))
        unknown = parse_invadeit_item(_item(inStock=None, amountInStock=None))
        assert instock and out and unknown
        self.assertEqual(instock.availability_status, "in_stock")
        self.assertTrue(is_verified_for_ranking(instock))
        self.assertEqual(out.availability_status, "out_of_stock")
        self.assertFalse(is_verified_for_ranking(out))
        self.assertEqual(unknown.availability_status, "unknown")
        self.assertFalse(is_verified_for_ranking(unknown))

    def test_sku_and_mpn(self) -> None:
        offer = parse_invadeit_item(_item())
        assert offer is not None
        self.assertEqual(offer.sku, "ASUS-G614PR-TS113W")
        self.assertTrue(offer.manufacturer_part_number)

    def test_product_detail_enrichment(self) -> None:
        offer = parse_invadeit_item(
            _item(keySpecs="GeForce RTX 5070 Ti 12GB GDDR7", name="ROG Strix G16")
        )
        assert offer is not None
        self.assertIsNone(offer.ram_gb)
        enrich_offer_from_specifications(
            offer,
            [{"name": "Memory", "value": "32GB DDR5"}, {"name": "Storage", "value": "1TB SSD"}],
        )
        self.assertEqual(offer.ram_gb, 32)
        self.assertEqual(offer.ssd_gb, 1024)
        self.assertTrue(offer.metadata.get("detail_enriched"))

    def test_cap_filtering(self) -> None:
        inside = parse_invadeit_item(_item(price=330000))
        outside = parse_invadeit_item(_item(price=330001))
        assert inside and outside
        ok_in, _, _ = offer_user_facing_eligible(inside, fx=_fx())
        ok_out, reason, _ = offer_user_facing_eligible(outside, fx=_fx())
        self.assertTrue(ok_in)
        self.assertFalse(ok_out)
        self.assertEqual(reason, "price_above_cap_rub")


if __name__ == "__main__":
    unittest.main()
