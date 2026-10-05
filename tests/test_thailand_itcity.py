from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from thailand.eligibility import offer_user_facing_eligible
from thailand.errors import html_block_code
from thailand.fx import FxRate
from thailand.itcity import parse_itcity_hit, read_search_config
from thailand.verification import is_verified_for_ranking


def _hit(**overrides) -> dict:
    base = {
        "objectID": "PRD1",
        "productId": "PRD1",
        "productName": "ASUS ROG Strix G16 G614PR-TS113W",
        "shortDescription": "RTX 5070 Ti Laptop GPU 16GB DDR5 1TB SSD 16 inch",
        "price": 99990,
        "specialPrice": None,
        "isSpecialPrice": False,
        "stockAvailable": True,
        "sku": ["ASUS-G614PR"],
        "brand": {"label": "ASUS"},
        "categories": [{"lvl0": "NOTEBOOK"}],
        "categoryName": ["NOTEBOOK"],
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


class ItCityParseTests(unittest.TestCase):
    def test_discovered_payload_parse(self) -> None:
        offers = parse_itcity_hit(_hit())
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].store, "itcity")

    def test_rtx_5070_ti(self) -> None:
        self.assertEqual(parse_itcity_hit(_hit())[0].gpu, "RTX 5070 Ti")

    def test_rtx_5080(self) -> None:
        offer = parse_itcity_hit(
            _hit(shortDescription="GeForce RTX 5080 Laptop GPU 32GB DDR5 2TB SSD")
        )[0]
        self.assertEqual(offer.gpu, "RTX 5080")

    def test_false_positives_excluded(self) -> None:
        desktop = parse_itcity_hit(
            _hit(
                productName="การ์ดจอ ASUS RTX 5070 Ti",
                categories=[{"lvl0": "VGA"}],
                categoryName=["การ์ดจอ"],
            )
        )
        comset = parse_itcity_hit(
            _hit(productName="เซทคอมประกอบ RTX 5070 Ti Comset", categoryName=["เซทคอมประกอบ"])
        )
        plain = parse_itcity_hit(_hit(shortDescription="RTX 5070 Laptop GPU 16GB DDR5"))
        self.assertEqual(desktop, [])
        self.assertEqual(comset, [])
        self.assertEqual(plain, [])

    def test_price_public_sale_not_member(self) -> None:
        sale = parse_itcity_hit(
            _hit(price=99990, isSpecialPrice=True, specialPrice={"value": 89990, "remark": "Monthly"})
        )[0]
        member = parse_itcity_hit(
            _hit(price=99990, isSpecialPrice=True, specialPrice={"value": 70000, "remark": "member only"})
        )[0]
        self.assertEqual(sale.price_thb, 89990)
        self.assertEqual(sale.regular_price_thb, 99990)
        self.assertEqual(member.price_thb, 99990)

    def test_availability(self) -> None:
        instock = parse_itcity_hit(_hit(stockAvailable=True))[0]
        out = parse_itcity_hit(_hit(stockAvailable=False))[0]
        self.assertTrue(is_verified_for_ranking(instock))
        self.assertFalse(is_verified_for_ranking(out))

    def test_specs_and_sku(self) -> None:
        offer = parse_itcity_hit(_hit())[0]
        self.assertEqual(offer.ram_gb, 16)
        self.assertEqual(offer.sku, "ASUS-G614PR")

    def test_product_url(self) -> None:
        offer = parse_itcity_hit(_hit())[0]
        self.assertEqual(offer.url, "https://www.itcity.in.th/product/PRD1")

    def test_cap_filtering(self) -> None:
        inside = parse_itcity_hit(_hit(price=300000))[0]
        outside = parse_itcity_hit(_hit(price=300001))[0]
        self.assertTrue(offer_user_facing_eligible(inside, fx=_fx())[0])
        ok, reason, _ = offer_user_facing_eligible(outside, fx=_fx())
        self.assertFalse(ok)
        self.assertEqual(reason, "price_above_300000_rub")

    def test_blocked_or_challenge_safe(self) -> None:
        self.assertEqual(html_block_code("<html>Just a moment...</html>"), "CHALLENGE")
        self.assertIsNone(read_search_config("<html>Just a moment...</html>"))
        from thailand.itcity import collect

        page = MagicMock()
        page.status_code = 200
        page.text = "<html><title>Just a moment...</title></html>"
        client = MagicMock()
        client.get.return_value = page
        client.__enter__.return_value = client
        with patch("thailand.itcity.httpx.Client", return_value=client):
            result = collect(client=client)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CHALLENGE")
        self.assertEqual(result.offers, [])


if __name__ == "__main__":
    unittest.main()
