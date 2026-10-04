from __future__ import annotations

import unittest

from thailand.jib import parse_read_product_html, parse_suggestion_rec
from thailand.specs_parse import exclude_non_target_gpu
from thailand.verification import (
    GPU_SOURCE_EXPLICIT_TITLE,
    GPU_SOURCE_STRUCTURED_API,
    GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
    UNVERIFIED,
    VERIFIED,
    compute_verification,
    is_purchasable_confirmed,
    is_verified_for_ranking,
)


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
        self.assertEqual(offers[0].gpu_source, GPU_SOURCE_EXPLICIT_TITLE)

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

    def test_availability_unknown_from_suggestion(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5070 Ti")
        self.assertFalse(offers[0].available)
        self.assertEqual(offers[0].availability_status, "unknown")
        self.assertFalse(offers[0].availability_confirmed)

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

    def test_tuf_a18_query_5080_not_authoritative(self) -> None:
        """CRITICAL regression: search query must not invent RTX 5080."""
        offers = parse_suggestion_rec(
            [
                self._rec(
                    id="0000086245",
                    title="NOTEBOOK ASUS TUF GAMING A18 FA808UH-S9096W - JAEGER GRAY",
                    price="58490.00",
                    salePrice="57590.00",
                )
            ],
            catalog_gpu="RTX 5080",
        )
        self.assertEqual(len(offers), 1)
        o = offers[0]
        self.assertNotEqual(o.gpu, "RTX 5080")
        self.assertIsNone(o.gpu)
        self.assertEqual(o.candidate_gpu, "RTX 5080")
        self.assertNotEqual(o.verification_status, VERIFIED)
        self.assertFalse(is_verified_for_ranking(o))

    def test_false_5070ti_from_query_only(self) -> None:
        offers = parse_suggestion_rec(
            [
                self._rec(
                    id="x1",
                    title="NOTEBOOK MSI CYBORG 15 MAX C2WG-257TH - PLATINUM GRAY",
                )
            ],
            catalog_gpu="RTX 5070 Ti",
        )
        self.assertEqual(len(offers), 1)
        self.assertIsNone(offers[0].gpu)
        self.assertEqual(offers[0].candidate_gpu, "RTX 5070 Ti")
        self.assertNotEqual(offers[0].verification_status, VERIFIED)

    def test_explicit_title_gpu_5070ti(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5080")
        self.assertEqual(offers[0].gpu, "RTX 5070 Ti")
        self.assertEqual(offers[0].gpu_source, GPU_SOURCE_EXPLICIT_TITLE)
        # Search context must not override title evidence
        self.assertEqual(offers[0].candidate_gpu, "RTX 5080")

    def test_explicit_title_gpu_5080(self) -> None:
        title = "NOTEBOOK ASUS ROG STRIX G18 RTX 5080 32GB 1TB"
        offers = parse_suggestion_rec(
            [self._rec(title=title)], catalog_gpu="RTX 5070 Ti"
        )
        self.assertEqual(offers[0].gpu, "RTX 5080")

    def test_conflict_query_5080_title_5070ti(self) -> None:
        offers = parse_suggestion_rec([self._rec()], catalog_gpu="RTX 5080")
        self.assertEqual(offers[0].gpu, "RTX 5070 Ti")

    def test_detail_gpu_overrides_seo_keywords(self) -> None:
        html = """
        <html><title>NOTEBOOK ASUS TUF GAMING A18 FA808UH-S9096W</title>
        <h1>NOTEBOOK ASUS TUF GAMING A18 FA808UH-S9096W</h1>
        <meta content="Nvidia GeForce RTX 5080,NvidiaGeForceRTX5080"/>
        <p>• กราฟิก : Nvidia GeForce RTX 5050 8GB GDDR7</p>
        <script>qty = 1;</script>
        </html>
        """
        detail = parse_read_product_html(html, external_id="0000086245")
        self.assertIsNone(detail.get("gpu"))  # 5050 excluded / non-target → None after canonical? 
        # parse keeps non-target gpu then apply_detail clears — here raw parse:
        # canonical_gpu("RTX 5050") returns None, so gpu stays None from _GPU_LINE match
        # Actually _GPU_LINE captures RTX 5050, canonical_gpu returns None for 5050
        self.assertIsNone(detail.get("gpu"))

    def test_detail_confirms_5070ti_and_stock(self) -> None:
        html = """
        <html><title>NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W</title>
        <h1>NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W</h1>
        <p>• ซีพียู : AMD Ryzen 9 8940HX</p>
        <p>• กราฟิก : Nvidia GeForce RTX 5070 Ti 12GB GDDR7</p>
        <p>RAM 16GB DDR5 และ SSD</p>
        <p>• เอสเอสดี : 1TB PCIe 4/NVMe M.2 SSD</p>
        <p>• จอแสดงผล : 16&quot; 2.5K (2560 x 1600, WQXGA)</p>
        <script>qty = 2;</script>
        </html>
        """
        detail = parse_read_product_html(html, external_id="0000085696")
        self.assertEqual(detail["gpu"], "RTX 5070 Ti")
        self.assertEqual(detail["gpu_source"], GPU_SOURCE_STRUCTURED_PRODUCT_PAGE)
        self.assertEqual(detail["cpu"], "AMD Ryzen 9 8940HX")
        self.assertEqual(detail["ram_gb"], 16)
        self.assertEqual(detail["ssd_gb"], 1024)
        self.assertEqual(detail["screen_size_inch"], 16.0)
        self.assertTrue(detail["availability_confirmed"])
        self.assertEqual(detail["availability_status"], "in_stock")


class AvailabilityPolicyTests(unittest.TestCase):
    def test_unknown_excluded_from_best(self) -> None:
        from datetime import datetime, timezone
        from thailand.models import ThailandOffer

        o = ThailandOffer(
            store="jib",
            external_id="1",
            name="X RTX 5070 Ti",
            url="https://example/1",
            price_thb=60000,
            available=False,
            availability_status="unknown",
            availability_confirmed=False,
            gpu="RTX 5070 Ti",
            gpu_source=GPU_SOURCE_EXPLICIT_TITLE,
            collected_at=datetime.now(timezone.utc),
        )
        compute_verification(o)
        self.assertFalse(is_purchasable_confirmed(o))
        self.assertNotEqual(o.verification_status, VERIFIED)

    def test_detail_in_stock_purchasable(self) -> None:
        from datetime import datetime, timezone
        from thailand.models import ThailandOffer

        o = ThailandOffer(
            store="jib",
            external_id="1",
            name="X RTX 5070 Ti",
            url="https://example/1",
            price_thb=60000,
            available=True,
            availability_status="in_stock",
            availability_confirmed=True,
            gpu="RTX 5070 Ti",
            gpu_source=GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
            collected_at=datetime.now(timezone.utc),
        )
        compute_verification(o)
        self.assertTrue(is_purchasable_confirmed(o))
        self.assertEqual(o.verification_status, VERIFIED)

    def test_oos_not_purchasable(self) -> None:
        from datetime import datetime, timezone
        from thailand.models import ThailandOffer

        o = ThailandOffer(
            store="jib",
            external_id="1",
            name="X RTX 5080",
            url="https://example/1",
            price_thb=80000,
            available=False,
            availability_status="out_of_stock",
            availability_confirmed=True,
            gpu="RTX 5080",
            gpu_source=GPU_SOURCE_EXPLICIT_TITLE,
            collected_at=datetime.now(timezone.utc),
        )
        compute_verification(o)
        self.assertFalse(is_purchasable_confirmed(o))
        self.assertEqual(o.verification_status, "PARTIAL")

    def test_pickup_allowed_labelled(self) -> None:
        from datetime import datetime, timezone
        from thailand.models import ThailandOffer

        o = ThailandOffer(
            store="banana",
            external_id="1",
            name="X RTX 5070 Ti",
            url="https://example/1",
            price_thb=60000,
            available=True,
            availability_status="store_pickup_only",
            availability_confirmed=True,
            gpu="RTX 5070 Ti",
            gpu_source=GPU_SOURCE_STRUCTURED_API,
            collected_at=datetime.now(timezone.utc),
        )
        compute_verification(o)
        self.assertTrue(is_purchasable_confirmed(o))
        self.assertEqual(o.verification_status, VERIFIED)

    def test_preorder_excluded(self) -> None:
        from datetime import datetime, timezone
        from thailand.models import ThailandOffer

        o = ThailandOffer(
            store="jib",
            external_id="1",
            name="X RTX 5070 Ti",
            url="https://example/1",
            price_thb=60000,
            available=True,
            availability_status="preorder",
            availability_confirmed=True,
            gpu="RTX 5070 Ti",
            gpu_source=GPU_SOURCE_EXPLICIT_TITLE,
            collected_at=datetime.now(timezone.utc),
        )
        self.assertFalse(is_purchasable_confirmed(o))


if __name__ == "__main__":
    unittest.main()
