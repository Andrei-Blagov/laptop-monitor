from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.jib import parse_suggestion_rec
from thailand.specs_parse import exclude_non_target_gpu


class JibParseTests(unittest.TestCase):
    def _rec(self, **kwargs):
        base = {
            "id": "0000085630",
            "title": (
                "NOTEBOOK LENOVO LEGION PRO 7 16IAX10H 83F500NWTA "
                "Intel Core Ultra 9 275HX 32GB RAM 1TB SSD RTX 5070 Ti 16\""
            ),
            "price": "124990.00",
            "salePrice": "119990.00",
            "link": "https://www.jib.co.th/img.jpg",
        }
        base.update(kwargs)
        return base

    def test_parse_5070_ti(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].gpu, "RTX 5070 Ti")

    def test_parse_5080(self) -> None:
        title = (
            "NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W "
            "Core Ultra 9 32GB 1TB SSD RTX 5080 16\""
        )
        offers = parse_suggestion_rec(
            [self._rec(id="0000085696", title=title, salePrice="84990.00")],
            catalog_gpu="RTX 5080",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].gpu, "RTX 5080")

    def test_exclude_5070_non_ti(self) -> None:
        self.assertTrue(exclude_non_target_gpu("NOTEBOOK MSI RTX 5070 8GB"))
        offers = parse_suggestion_rec(
            [self._rec(title="NOTEBOOK MSI Thin RTX 5070 16GB RAM")],
            catalog_gpu="RTX 5070 Ti",
        )
        # Title has 5070 non-Ti → excluded before catalog apply
        self.assertEqual(offers, [])

    def test_exclude_5090(self) -> None:
        self.assertTrue(exclude_non_target_gpu("NOTEBOOK ASUS RTX 5090"))
        offers = parse_suggestion_rec(
            [self._rec(title="NOTEBOOK ASUS ROG RTX 5090 64GB")],
            catalog_gpu="RTX 5080",
        )
        self.assertEqual(offers, [])

    def test_thb_price(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertEqual(offers[0].price_thb, 119990)

    def test_public_promo_price(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertEqual(offers[0].price_thb, 119990)
        self.assertEqual(offers[0].regular_price_thb, 124990)

    def test_availability(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertTrue(offers[0].available)
        self.assertEqual(offers[0].availability_status, "in_stock")

    def test_cpu(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertIsNotNone(offers[0].cpu)
        self.assertIn("Ultra", offers[0].cpu)

    def test_ram(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertEqual(offers[0].ram_gb, 32)

    def test_ssd(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertEqual(offers[0].ssd_gb, 1024)

    def test_screen(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertEqual(offers[0].screen_size_inch, 16.0)

    def test_part_number(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertTrue(offers[0].manufacturer_part_number)
        self.assertIn("83F500NWTA", offers[0].manufacturer_part_number)


if __name__ == "__main__":
    unittest.main()
