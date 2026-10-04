from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from thailand.banana import collect, parse_banana_json_payload, parse_banana_product_dict
from stores.common import extract_specs_from_name


class BananaParseTests(unittest.TestCase):
    def _raw(self, **kwargs):
        base = {
            "external_id": "bnn-1",
            "name": (
                "MSI Vector 16 HX AI RTX 5070 Ti Laptop "
                "Core Ultra 9 275HX 32GB DDR5 1TB SSD 16\" 2560x1600"
            ),
            "sku": "83F500NWTA",
            "manufacturer_part_number": "83F500NWTA",
            "price_thb": 62990,
            "regular_price_thb": 64990,
            "available": True,
            "availability_status": "online_available",
            "gpu": "RTX 5070 Ti",
            "cpu": "Intel Core Ultra 9 275HX",
            "ram_gb": 32,
            "ssd_gb": 1024,
            "screen_size_inch": 16.0,
            "screen_resolution": "2560x1600",
            "url": "https://www.bnn.in.th/th/p/bnn-1",
        }
        base.update(kwargs)
        return base

    def test_parse_5070_ti(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertIsNotNone(o)
        self.assertEqual(o.store, "banana")
        self.assertEqual(o.gpu, "RTX 5070 Ti")

    def test_parse_5080(self) -> None:
        o = parse_banana_product_dict(
            self._raw(
                external_id="bnn-5080",
                name="ASUS ROG Strix G18 RTX 5080 32GB 1TB 18\"",
                gpu="RTX 5080",
                price_thb=84990,
            )
        )
        self.assertIsNotNone(o)
        self.assertEqual(o.gpu, "RTX 5080")

    def test_public_price(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.price_thb, 62990)

    def test_promo_price(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.regular_price_thb, 64990)

    def test_out_of_stock(self) -> None:
        o = parse_banana_product_dict(
            self._raw(available=False, availability_status="out of stock")
        )
        self.assertFalse(o.available)
        self.assertEqual(o.availability_status, "out_of_stock")

    def test_store_pickup_only(self) -> None:
        o = parse_banana_product_dict(
            self._raw(availability_status="store pickup only", available=True)
        )
        self.assertTrue(o.available)
        self.assertEqual(o.availability_status, "store_pickup_only")

    def test_online_available(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.availability_status, "online_available")

    def test_cpu_ram_ssd(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.ram_gb, 32)
        self.assertEqual(o.ssd_gb, 1024)
        self.assertIn("Ultra", o.cpu or "")

    def test_screen(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.screen_size_inch, 16.0)

    def test_resolution(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.screen_resolution, "2560x1600")

    def test_sku_mpn(self) -> None:
        o = parse_banana_product_dict(self._raw())
        self.assertEqual(o.sku, "83F500NWTA")
        self.assertEqual(o.manufacturer_part_number, "83F500NWTA")

    def test_no_gddr_ram_confusion(self) -> None:
        specs = extract_specs_from_name(
            "ASUS ROG Strix G16 RTX 5080 16GB GDDR7 32GB DDR5 1TB SSD"
        )
        self.assertEqual(specs.get("ram_gb"), 32)
        o = parse_banana_product_dict(
            self._raw(
                name="ASUS ROG Strix G16 RTX 5080 16GB GDDR7 32GB DDR5 RAM 1TB SSD",
                gpu="RTX 5080",
                ram_gb=None,  # force title parse
            )
        )
        # Explicit ram_gb None — parser may still get from structured after specs
        # Re-parse via name only path:
        from thailand.specs_parse import specs_from_text

        s = specs_from_text(
            "ASUS ROG Strix G16 RTX 5080 16GB GDDR7 32GB DDR5 RAM 1TB SSD",
            catalog_gpu="RTX 5080",
        )
        self.assertEqual(s.get("ram_gb"), 32)


class BananaCollectTests(unittest.TestCase):
    def test_http_json_success(self) -> None:
        payload = [
            {
                "external_id": "b1",
                "name": "MSI Vector 16 HX AI RTX 5070 Ti 32GB 1TB",
                "price_thb": 62990,
                "available": True,
                "availability_status": "online_available",
                "gpu": "RTX 5070 Ti",
                "cpu": "Intel Core Ultra 9 275HX",
                "ram_gb": 32,
                "ssd_gb": 1024,
                "url": "https://www.bnn.in.th/th/p/b1",
            }
        ]

        class Resp:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self):
                return payload

            text = ""

        client = MagicMock()
        client.get.return_value = Resp()
        result = collect(client=client, allow_browser=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.collection_mode, "http")
        self.assertGreaterEqual(len(result.offers), 1)
        self.assertEqual(result.offers[0].gpu, "RTX 5070 Ti")

    def test_browser_fallback_on_http_403(self) -> None:
        class Resp:
            status_code = 403
            headers = {"content-type": "text/html"}
            text = "Attention Required! Cloudflare"
            def json(self):
                raise ValueError("no")

        client = MagicMock()
        client.get.return_value = Resp()
        offer = parse_banana_product_dict(
            {
                "external_id": "b2",
                "name": "ASUS ROG Strix G16 RTX 5080 32GB 1TB",
                "price_thb": 84990,
                "available": True,
                "availability_status": "in_stock",
                "gpu": "RTX 5080",
                "url": "https://www.bnn.in.th/th/p/b2",
            }
        )
        with patch("thailand.banana._collect_browser", return_value=([offer], [])):
            result = collect(client=client, allow_browser=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.collection_mode, "browser")
        self.assertEqual(result.offers[0].gpu, "RTX 5080")

    def test_browser_challenge_failed(self) -> None:
        class Resp:
            status_code = 403
            headers = {"content-type": "text/html"}
            text = "blocked"
            def json(self):
                raise ValueError("no")

        client = MagicMock()
        client.get.return_value = Resp()
        with patch(
            "thailand.banana._collect_browser",
            return_value=([], ["challenge"]),
        ):
            result = collect(client=client, allow_browser=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.collection_mode, "failed")

    def test_query_does_not_override_product_gpu(self) -> None:
        # catalog_gpu is candidate only; product without authoritative GPU → drop
        o = parse_banana_product_dict(
            {
                "external_id": "x",
                "name": "Gaming notebook special offer",
                "price_thb": 50000,
                "available": True,
                "availability_status": "in_stock",
            },
            catalog_gpu="RTX 5080",
        )
        self.assertIsNone(o)


if __name__ == "__main__":
    unittest.main()
