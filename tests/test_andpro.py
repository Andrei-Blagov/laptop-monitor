from __future__ import annotations

import unittest
from pathlib import Path

from parsers.andpro import parse_catalog_html


FIXTURE = Path(__file__).parent / "fixtures" / "andpro_catalog_sample.html"


class AndproParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = FIXTURE.read_text(encoding="utf-8")
        cls.products = parse_catalog_html(cls.html)
        cls.by_id = {p.external_id: p for p in cls.products}

    def test_parses_target_laptops_only(self) -> None:
        # 5070 Ti + 5080 available + 5080 sold; excludes desktop GPU and 4050.
        self.assertEqual(len(self.products), 3)
        self.assertNotIn("999001", self.by_id)
        self.assertNotIn("111222", self.by_id)

    def test_rtx_5070_ti_fields(self) -> None:
        product = self.by_id["258741"]
        self.assertEqual(product.store, "andpro")
        self.assertEqual(product.external_id, "258741")
        self.assertIn("MSI Stealth A16", product.name)
        self.assertEqual(product.sku, "9S7-15FL35-239")
        self.assertEqual(product.price, 296654)
        self.assertTrue(product.available)
        self.assertTrue(
            product.url.endswith(
                "/igrovoy_noutbuk_msi_stealth_a16_ai_a3hwhg_239xru_16_2560x1600_wqxga_9s7_15fl35_239/"
            )
        )
        self.assertTrue(product.url.startswith("https://andpro.ru/"))

    def test_rtx_5080_fields(self) -> None:
        product = self.by_id["258747"]
        self.assertEqual(product.external_id, "258747")
        self.assertIn("Colorful", product.name)
        self.assertEqual(product.sku, "A10207000032")
        self.assertEqual(product.price, 328648)
        self.assertTrue(product.available)
        self.assertTrue(product.url.startswith("https://andpro.ru/catalog/notebooks_accessories/notebooks/"))

    def test_sold_item_available_false(self) -> None:
        product = self.by_id["257500"]
        self.assertFalse(product.available)
        self.assertEqual(product.price, 430172)
        self.assertEqual(product.sku, "9S7-17S372-063")

    def test_sku_is_not_warehouse_code(self) -> None:
        product = self.by_id["258741"]
        self.assertNotEqual(product.sku, "169215")
        self.assertEqual(product.sku, "9S7-15FL35-239")

    def test_filters_out_desktop_gpu_and_wrong_mobile_gpu(self) -> None:
        names = " ".join(p.name for p in self.products)
        self.assertNotIn("Desktop", names)
        self.assertNotIn("4050", names)


if __name__ == "__main__":
    unittest.main()
