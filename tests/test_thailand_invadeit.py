from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from thailand.eligibility import offer_user_facing_eligible
from thailand.errors import RATE_LIMITED
from thailand.fx import FxRate
from thailand.invadeit import (
    enrich_offer_from_specifications,
    parse_invadeit_item,
    product_url,
    specifications_to_text,
)
from thailand.verification import is_verified_for_ranking

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invadeit"
EXPECTED_RESOLUTION = {
    "79530": "2560x1600",
    "79542": "3840x2400",
    "81984": "2560x1600",
    "81682": "2560x1600",
    "81991": "3840x2400",
}


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
        inside = parse_invadeit_item(_item(price=330000, keySpecs="32GB DDR5 / 1TB SSD / 2560x1600 / RTX 5070 Ti"))
        outside = parse_invadeit_item(_item(price=330001, keySpecs="32GB DDR5 / 1TB SSD / 2560x1600 / RTX 5070 Ti"))
        assert inside and outside
        ok_in, _, _ = offer_user_facing_eligible(inside, fx=_fx())
        ok_out, reason, _ = offer_user_facing_eligible(outside, fx=_fx())
        self.assertTrue(ok_in)
        self.assertFalse(ok_out)
        self.assertEqual(reason, "price_above_cap_rub")


class SpecificationFixtureTests(unittest.TestCase):
    def test_saved_display_values_extract_numeric_resolution(self) -> None:
        fetched = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
        for product_id, expected in EXPECTED_RESOLUTION.items():
            payload = json.loads((FIXTURES / f"{product_id}.json").read_text(encoding="utf-8"))
            offer = parse_invadeit_item(
                _item(
                    id=int(product_id),
                    name=f"Notebook {product_id}",
                    keySpecs="16\" 2.5K / RTX 5070 Ti\n32GB DDR5 / 1TB SSD",
                )
            )
            assert offer is not None
            self.assertIsNone(offer.screen_resolution)
            enrich_offer_from_specifications(offer, payload, fetched_at=fetched)
            self.assertEqual(offer.screen_resolution, expected)
            evidence = offer.metadata["screen_resolution_evidence"]
            self.assertEqual(evidence["product_id"], product_id)
            self.assertEqual(evidence["screen_resolution"], expected)
            self.assertEqual(evidence["source"], "/specifications")
            self.assertEqual(evidence["fetched_at"], fetched.isoformat())
            normalized = evidence["display_value"].replace(" ", "").replace("×", "x")
            self.assertIn(expected, normalized)
            self.assertNotIn("items", evidence)

    def test_old_flat_rows_still_fill_memory(self) -> None:
        text = specifications_to_text(
            [{"name": "Memory", "value": "32GB DDR5"}, {"name": "Storage", "value": "1TB SSD"}]
        )
        self.assertIn("Memory: 32GB DDR5", text)

    def test_nested_groups_are_read(self) -> None:
        payload = [
            {
                "name": "Specifications",
                "items": [{"name": "Display", "value": '16" (2560 x 1600)'}],
            },
            {"name": "Dimensions", "items": [{"name": "Weight", "value": "2.5 kg"}]},
        ]
        self.assertIn("Display:", specifications_to_text(payload))
        self.assertIn("Weight:", specifications_to_text(payload))

    def test_marketing_labels_are_not_turned_into_pixels(self) -> None:
        for label in ("2.5K", "WQXGA", "QHD+", "UHD+", "4K"):
            offer = parse_invadeit_item(_item(id=1, keySpecs=f'16" {label} / RTX 5070 Ti'))
            assert offer is not None
            enrich_offer_from_specifications(
                offer,
                [{"name": "Display", "value": f'16" {label} 240Hz'}],
            )
            self.assertIsNone(offer.screen_resolution)
            self.assertNotIn("screen_resolution_evidence", offer.metadata)

    def test_hdmi_and_webcam_are_not_the_built_in_panel(self) -> None:
        offer = parse_invadeit_item(_item(id=1, keySpecs="RTX 5070 Ti"))
        assert offer is not None
        enrich_offer_from_specifications(
            offer,
            [
                {
                    "name": "Specifications",
                    "items": [
                        {"name": "Display", "value": '16" 2.5K 300Hz'},
                        {"name": "Webcam", "value": "FHD 1920x1080"},
                        {"name": "Other port", "value": "HDMI external monitor 1920 x 1080"},
                    ],
                }
            ],
        )
        self.assertIsNone(offer.screen_resolution)

    def test_resolution_is_not_copied_onto_another_sku(self) -> None:
        payload = json.loads((FIXTURES / "81984.json").read_text(encoding="utf-8"))
        first = parse_invadeit_item(_item(id=81984, productNumber="SKU-A", name="Notebook SKU-A"))
        second = parse_invadeit_item(
            _item(id=81991, productNumber="SKU-B", name="Notebook SKU-B G835LWG")
        )
        assert first is not None and second is not None
        enrich_offer_from_specifications(first, payload)
        self.assertEqual(first.screen_resolution, "2560x1600")
        self.assertIsNone(second.screen_resolution)
        self.assertNotIn("screen_resolution_evidence", second.metadata)


def _catalog_item(product_id: int, *, ram: str = "32GB", price: int = 80000) -> dict:
    return _item(
        id=product_id,
        name=f"Notebook {product_id}",
        productNumber=f"SKU-{product_id}",
        price=price,
        keySpecs=f'16" 2.5K / Ryzen 9\n{ram} DDR5 / 1TB SSD\nGeForce RTX 5070 Ti 12GB GDDR7',
    )


class SpecificationFetchTests(unittest.TestCase):
    def test_detail_cap_keeps_unchecked_cards(self) -> None:
        from thailand.invadeit import collect

        items = [_catalog_item(70000 + index) for index in range(16)]
        items.append(_catalog_item(16, ram="16GB"))
        seen: list[str] = []

        def get(url):
            seen.append(url)
            response = mock.Mock()
            response.status_code = 200
            response.headers = {"content-type": "application/json"}
            if "specifications" in url:
                response.json.return_value = [
                    {"name": "Display", "value": '16" (2560 x 1600)'}
                ]
                return response
            response.json.return_value = {"items": items, "hasNextPage": False}
            return response

        client = mock.Mock()
        client.get.side_effect = get
        fx = _fx()
        fx.rub_per_thb = 2.5
        result = collect(client=client, fx=fx)
        spec_calls = [url for url in seen if "specifications" in url]
        self.assertEqual(len(spec_calls), 15)
        self.assertTrue(result.ok)
        kept = result.offers + result.unverified_candidates
        self.assertEqual(len(kept), 17)
        resolved = [offer for offer in kept if offer.screen_resolution == "2560x1600"]
        unchecked = [offer for offer in kept if offer.screen_resolution is None]
        self.assertEqual(len(resolved), 15)
        self.assertEqual(
            sorted(offer.product_id or "" for offer in unchecked),
            ["16", "70015"],
        )
        note = " ".join(result.discovery_limitations or [])
        self.assertIn("not_checked specifications:", note)
        self.assertNotIn("NO_RESULTS", result.error_code or "")

    def test_429_stops_later_specification_calls_and_keeps_offers(self) -> None:
        from thailand.invadeit import collect

        items = [_catalog_item(79530), _catalog_item(79542)]
        seen: list[str] = []

        def get(url):
            seen.append(url)
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            if "specifications" in url:
                response.status_code = 429
                return response
            response.status_code = 200
            response.json.return_value = {"items": items, "hasNextPage": False}
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, fx=_fx())
        self.assertEqual(result.error_code, RATE_LIMITED)
        self.assertFalse(result.ok)
        self.assertTrue(result.discovery_rate_limited)
        self.assertEqual(sum(1 for url in seen if "specifications" in url), 1)
        kept = result.offers + result.unverified_candidates
        self.assertEqual({offer.product_id for offer in kept}, {"79530", "79542"})
        self.assertIn("stopped_on_429", result.discovery_limitations or [])
        self.assertTrue(all(offer.screen_resolution is None for offer in kept))
        from thailand.discovery import coverage_for_result

        report = coverage_for_result(result)
        self.assertIn("notebooks-laptops", report["queries_finished"])
        self.assertNotIn("notebooks-laptops", report["queries_not_completed"])
        self.assertEqual(
            report["queries_not_completed"],
            ["specifications:79530", "specifications:79542"],
        )
        self.assertEqual(report["query_skip_reason"], "stopped_on_429")

    def test_catalog_429_keeps_cards_from_earlier_pages(self) -> None:
        from thailand.invadeit import collect

        seen: list[str] = []

        def get(url):
            seen.append(url)
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            if "page=2" in url:
                response.status_code = 429
                return response
            response.status_code = 200
            response.json.return_value = {
                "items": [_catalog_item(81682)],
                "hasNextPage": True,
            }
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client)
        self.assertEqual(result.error_code, RATE_LIMITED)
        self.assertEqual([offer.product_id for offer in result.offers + result.unverified_candidates], ["81682"])
        self.assertFalse(any("specifications" in url for url in seen))
        self.assertIn("stopped_on_429", result.discovery_limitations or [])
        from thailand.discovery import coverage_for_result

        report = coverage_for_result(result)
        self.assertNotIn("notebooks-laptops", report["queries_finished"])
        self.assertIn("notebooks-laptops", report["queries_not_completed"])

    def test_scan_passes_fx_and_requests_priority_laptops_first(self) -> None:
        from thailand.discovery import coverage_for_result
        from thailand.invadeit import collect as real_collect
        from thailand.registry import ThailandStoreAdapter
        from thailand.scanner import run_thailand_scan

        items = [_catalog_item(index, ram="16GB") for index in (1, 2)]
        items.extend(_catalog_item(100 + index) for index in range(16))
        seen: list[str] = []
        received: dict = {}

        def get(url):
            seen.append(url)
            response = mock.Mock()
            response.status_code = 200
            response.headers = {"content-type": "application/json"}
            if "specifications" in url:
                response.json.return_value = [{"name": "Display", "value": '16" (2560 x 1600)'}]
                return response
            response.json.return_value = {"items": items, "hasNextPage": False}
            return response

        mock_http = mock.Mock()
        mock_http.get.side_effect = get
        fx = _fx()
        fx.rub_per_thb = 2.5

        def collect(*, timeout: float = 90.0, client=None, fx=None):
            received["fx"] = fx
            received["result"] = real_collect(timeout=timeout, client=mock_http, fx=fx)
            return received["result"]

        adapter = ThailandStoreAdapter(
            slug="invadeit",
            display_name="InvadeIT",
            enabled=True,
            collect=collect,
        )
        with mock.patch("thailand.scanner.get_thailand_adapters", return_value=[adapter]), mock.patch(
            "thailand.scanner.record_observation"
        ), mock.patch("thailand.scanner.fetch_cbr_thb_rate", return_value=fx):
            run_thailand_scan(trigger_type="manual", write_snapshot=False)
        self.assertIs(received["fx"], fx)
        spec_ids = [url.rsplit("/", 2)[-2] for url in seen if "specifications" in url]
        self.assertEqual(spec_ids, [str(100 + index) for index in range(15)])
        report = coverage_for_result(received["result"])
        self.assertIn("notebooks-laptops", report["queries_finished"])
        self.assertIn("specifications:115", report["queries_not_completed"])
        self.assertIn("specifications:1", report["queries_not_completed"])
        self.assertNotIn("specifications:100", report["queries_not_completed"])


if __name__ == "__main__":
    unittest.main()
