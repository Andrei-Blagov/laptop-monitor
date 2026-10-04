from __future__ import annotations

import unittest

from thailand.advice import parse_advice_product_dict


class AdviceParseTests(unittest.TestCase):
    def _raw(self, **kwargs):
        base = {
            "external_id": "adv-5070ti-1",
            "name": (
                "MSI Vector 16 HX AI RTX 5070 Ti "
                "Intel Core Ultra 9 275HX 32GB RAM 1TB SSD 16\""
            ),
            "sku": "VECTOR16-5070TI",
            "manufacturer_part_number": "VECTOR16HXAI",
            "price_thb": 65990,
            "available": True,
            "availability_status": "in_stock",
            "cpu": "Intel Core Ultra 9 275HX",
            "gpu": "RTX 5070 Ti",
            "ram_gb": 32,
            "ssd_gb": 1024,
            "screen_size_inch": 16.0,
            "warranty": "2 years Thailand warranty",
            "url": "https://www.advice.co.th/product/adv-5070ti-1",
        }
        base.update(kwargs)
        return base

    def test_parse_5070_ti(self) -> None:
        o = parse_advice_product_dict(self._raw())
        self.assertIsNotNone(o)
        self.assertEqual(o.gpu, "RTX 5070 Ti")
        self.assertEqual(o.store, "advice")

    def test_parse_5080(self) -> None:
        o = parse_advice_product_dict(
            self._raw(
                external_id="adv-5080",
                name="ASUS ROG Strix Scar 18 RTX 5080 64GB 2TB SSD 18\"",
                gpu="RTX 5080",
                price_thb=89990,
                ram_gb=64,
                ssd_gb=2048,
                screen_size_inch=18.0,
            )
        )
        self.assertIsNotNone(o)
        self.assertEqual(o.gpu, "RTX 5080")

    def test_price(self) -> None:
        o = parse_advice_product_dict(self._raw())
        self.assertEqual(o.price_thb, 65990)

    def test_availability(self) -> None:
        o = parse_advice_product_dict(self._raw())
        self.assertTrue(o.available)

    def test_cpu_ram_ssd(self) -> None:
        o = parse_advice_product_dict(self._raw())
        self.assertIn("Ultra", o.cpu or "")
        self.assertEqual(o.ram_gb, 32)
        self.assertEqual(o.ssd_gb, 1024)

    def test_warranty(self) -> None:
        o = parse_advice_product_dict(self._raw())
        self.assertIn("Thailand", o.warranty or "")

    def test_strong_identifiers(self) -> None:
        o = parse_advice_product_dict(self._raw())
        self.assertEqual(o.manufacturer_part_number, "VECTOR16HXAI")
        self.assertEqual(o.sku, "VECTOR16-5070TI")


if __name__ == "__main__":
    unittest.main()
