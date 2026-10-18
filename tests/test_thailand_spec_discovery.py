"""Wide Thailand discovery is not limited to the personal TOP-3."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import config
from thailand.discovery import (
    GPU_SEARCH_QUERIES,
    LINE_SEARCH_QUERIES,
    QUERY_BUDGET,
    context_gpu,
    coverage_for_offers,
    search_queries,
)
from thailand.scanner import build_search_coverage
from thailand.verification import VERIFIED, UNVERIFIED
from thailand.models import FxRate, StoreScanResult, ThailandOffer


NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


def _offer(**kwargs) -> ThailandOffer:
    base = dict(
        store="jib",
        external_id="1",
        name="Laptop",
        url="https://example.test/1",
        price_thb=80000,
        available=True,
        collected_at=NOW,
        gpu="RTX 5070 Ti",
        ram_gb=32,
        ssd_gb=1024,
        screen_resolution="2560x1600",
        screen_size_inch=16,
    )
    base.update(kwargs)
    return ThailandOffer(**base)


def _fx(*, stale: bool = False) -> FxRate:
    return FxRate(
        source="test",
        currency="THB",
        nominal=1,
        official_rate=2.5,
        rub_per_thb=2.5,
        published_date="2026-10-14",
        fetched_at=NOW,
        stale=stale,
    )


class QueryListTests(unittest.TestCase):
    def test_gpu_and_line_queries_are_not_the_personal_top3(self) -> None:
        personal = [
            sku
            for model in config.PERSONAL_TOP3
            for sku in (*model.get("exact_skus", ()), *model.get("series_codes", ()))
        ]
        queries = search_queries()
        self.assertTrue(set(GPU_SEARCH_QUERIES) <= set(queries))
        self.assertTrue(any(query in queries for query in LINE_SEARCH_QUERIES[:4]))
        self.assertFalse(set(queries) <= set(personal))
        self.assertNotIn("A2XWHG-814XRU", queries)

    def test_regional_sku_is_an_extra_key(self) -> None:
        queries = search_queries(extra_codes=["A2XWHG-814XRU"])
        self.assertIn("A2XWHG-814XRU", queries)
        self.assertIn("notebook rtx 5070 ti", queries)
        self.assertIn("โน้ตบุ๊ค RTX 5080", queries)
        self.assertLess(queries.index("notebook rtx 5070 ti"), queries.index("A2XWHG-814XRU"))

    def test_rtx_5070_is_not_treated_as_5070_ti(self) -> None:
        for query in GPU_SEARCH_QUERIES + LINE_SEARCH_QUERIES:
            lowered = query.lower()
            if "5070" in lowered:
                self.assertIn("ti", lowered)
        self.assertIsNone(context_gpu("ROG Strix RTX 5070"))
        self.assertEqual(context_gpu("ROG Strix RTX 5070 Ti"), "RTX 5070 Ti")
        self.assertEqual(context_gpu("notebook rtx 5080"), "RTX 5080")

    def test_line_queries_rotate_without_dropping_gpu_search(self) -> None:
        first = search_queries(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        second = search_queries(now=datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(first[: len(GPU_SEARCH_QUERIES)], list(GPU_SEARCH_QUERIES))
        self.assertEqual(second[: len(GPU_SEARCH_QUERIES)], list(GPU_SEARCH_QUERIES))
        lines_first = [query for query in first if query in LINE_SEARCH_QUERIES]
        lines_second = [query for query in second if query in LINE_SEARCH_QUERIES]
        self.assertNotEqual(lines_first, lines_second)
        self.assertTrue(set(LINE_SEARCH_QUERIES) <= set(lines_first) | set(lines_second))

    def test_budget_keeps_gpu_queries_ahead_of_codes(self) -> None:
        codes = [f"SKU-{index}" for index in range(20)]
        queries = search_queries(extra_codes=codes)
        self.assertEqual(queries[: len(GPU_SEARCH_QUERIES)], list(GPU_SEARCH_QUERIES))
        self.assertEqual(len(queries), QUERY_BUDGET)
        self.assertTrue(queries[len(GPU_SEARCH_QUERIES)].startswith("SKU-"))


class CoverageTests(unittest.TestCase):
    def test_incomplete_offer_is_kept_out_of_the_purchase_gate(self) -> None:
        incomplete = _offer(external_id="partial", ram_gb=None, ssd_gb=None, screen_resolution=None)
        ready = _offer(external_id="ready", price_thb=80_000)
        report = coverage_for_offers([incomplete, ready], fx=_fx())
        self.assertEqual(report["discovered"], 2)
        self.assertEqual(report["spec_checked"], 1)
        self.assertEqual(report["meets_requirements"], 1)
        self.assertEqual(report["hardware_and_price"], 1)
        self.assertGreater(report["exclusion_reasons"].get("SPECS_MISSING", 0), 0)

    def test_unchecked_query_keys_are_not_inferred_from_a_count(self) -> None:
        report = coverage_for_offers(
            [],
            queries=["notebook rtx 5070 ti", "SKU-A", "SKU-B"],
            queries_finished=["notebook rtx 5070 ti", "SKU-B"],
            rate_limited=True,
        )
        self.assertEqual(report["queries_finished"], ["notebook rtx 5070 ti", "SKU-B"])
        self.assertEqual(report["queries_not_completed"], ["SKU-A"])
        self.assertEqual(report["query_skip_reason"], "stopped_on_429")

    def test_hardware_match_is_not_a_purchase_offer(self) -> None:
        unverified = _offer(verification_status=UNVERIFIED)
        verified = _offer(
            external_id="ready",
            verification_status=VERIFIED,
            availability_confirmed=True,
            available=True,
            availability_status="in_stock",
        )
        report = coverage_for_offers([unverified, verified], fx=_fx())
        self.assertEqual(report["hardware_and_price"], 2)
        self.assertEqual(report["purchasable"], 1)

    def test_purchasable_requires_stock_fx_and_variant_price(self) -> None:
        from thailand.seller_trust import TIER_A, TIER_C

        fx = _fx()
        stocked = _offer(
            verification_status=VERIFIED,
            availability_confirmed=True,
            available=True,
            availability_status="in_stock",
            gpu_source="explicit_title",
        )
        unconfirmed = _offer(
            external_id="nostock",
            verification_status=VERIFIED,
            availability_confirmed=False,
            available=True,
        )
        variant = _offer(
            external_id="variant",
            verification_status=VERIFIED,
            availability_confirmed=True,
            available=True,
            availability_status="in_stock",
            marketplace=True,
            price_verified_for_variant=False,
            seller_trust_tier=TIER_A,
        )
        untrusted = _offer(
            external_id="seller",
            verification_status=VERIFIED,
            availability_confirmed=True,
            available=True,
            availability_status="in_stock",
            marketplace=True,
            price_verified_for_variant=True,
            seller_trust_tier=TIER_C,
        )
        report = coverage_for_offers([stocked, unconfirmed, variant, untrusted], fx=fx)
        self.assertEqual(report["hardware_and_price"], 4)
        self.assertEqual(report["purchasable"], 1)
        stale = _fx(stale=True)
        stale_report = coverage_for_offers([stocked], fx=stale)
        self.assertEqual(stale_report["hardware_and_price"], 1)
        self.assertEqual(stale_report["purchasable"], 0)

    def test_confirmed_offer_meets_the_shared_gate(self) -> None:
        report = coverage_for_offers(
            [
                _offer(
                    verification_status=VERIFIED,
                    availability_confirmed=True,
                    available=True,
                    availability_status="in_stock",
                )
            ],
            fx=_fx(),
        )
        self.assertEqual(report["meets_requirements"], 1)
        self.assertEqual(report["purchasable"], 1)
        self.assertEqual(report["exclusion_reasons"], {})


class CollectorQueryTests(unittest.TestCase):
    def test_jib_stops_on_429_and_sends_a_line_query(self) -> None:
        from thailand.jib import collect

        seen: list[str] = []

        def get(url, params=None, timeout=None):
            term = (params or {}).get("term", "")
            seen.append(term)
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            if len(seen) >= 3:
                response.status_code = 429
                return response
            response.status_code = 200
            response.json.return_value = {"rec": []}
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, verify_details=False, target_codes=["G615LR-S5348"])
        self.assertIn("notebook rtx 5070 ti", seen)
        self.assertTrue(any("ROG" in term or "Legion" in term or "โน้ตบุ๊ค" in term for term in seen))
        self.assertNotIn("G615LR-S5348", seen)
        self.assertTrue(result.discovery_rate_limited)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "RATE_LIMITED")
        self.assertEqual(result.codes_checked, [])
        self.assertLess(len(seen), len(result.discovery_queries or []))

    def test_advice_queries_are_wider_than_one_sku(self) -> None:
        from thailand.advice import collect

        seen: list[str] = []

        def get(url, timeout=None):
            seen.append(url)
            response = mock.Mock()
            response.status_code = 200
            response.headers = {"content-type": "text/html"}
            response.text = "<html></html>"
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, target_codes=["16AFR10H"])
        joined = " ".join(seen)
        self.assertIn("notebook+rtx+5070", joined.lower().replace("%20", "+"))
        self.assertIn("16AFR10H", joined)
        self.assertGreater(len(result.discovery_queries or []), 2)
        self.assertEqual(result.queries_completed, len(result.discovery_queries or []))

    def test_jib_429_after_a_hit_keeps_the_offer_and_is_not_a_full_success(self) -> None:
        from thailand.jib import collect

        def get(url, params=None, timeout=None):
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            term = (params or {}).get("term", "")
            if term != "notebook rtx 5070 ti":
                response.status_code = 429
                return response
            response.status_code = 200
            response.json.return_value = {
                "rec": [{
                    "id": "77",
                    "title": "MSI Raider RTX 5070 Ti 32GB 1TB 2560x1600",
                    "price": "89900",
                }]
            }
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, verify_details=False, target_codes=["16AFR10H"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "RATE_LIMITED")
        self.assertTrue(result.discovery_rate_limited)
        self.assertGreaterEqual(result.discovered_count or 0, 1)
        self.assertEqual(result.codes_checked, [])
        self.assertIn("16AFR10H", result.targeted_codes or [])
        self.assertIn("stopped_on_429", result.discovery_limitations or [])

    def test_jib_429_does_not_open_product_pages(self) -> None:
        from thailand.jib import collect

        calls: list[str] = []

        def get(url, params=None, timeout=None):
            calls.append(str(url))
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            term = (params or {}).get("term", "")
            if term != "notebook rtx 5070 ti":
                response.status_code = 429
                return response
            response.status_code = 200
            response.json.return_value = {
                "rec": [{
                    "id": "77",
                    "title": "MSI Raider RTX 5070 Ti 32GB 1TB 2560x1600",
                    "price": "89900",
                }]
            }
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, verify_details=True, target_codes=["16AFR10H"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "RATE_LIMITED")
        self.assertGreaterEqual(result.discovered_count or 0, 1)
        self.assertEqual(calls, ["https://www.jib.co.th/web/product/search_suggestion"] * 2)
        self.assertFalse(any("readProduct" in url for url in calls))
        self.assertEqual(result.queries_finished, ["notebook rtx 5070 ti"])
        self.assertNotIn("16AFR10H", result.queries_finished or [])

    def test_jib_failed_middle_query_does_not_hide_a_later_code(self) -> None:
        from thailand.discovery import coverage_for_result
        from thailand.jib import collect

        def get(url, params=None, timeout=None):
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            term = (params or {}).get("term", "")
            if term == "notebook rtx 5080":
                response.status_code = 400
                return response
            response.status_code = 200
            response.json.return_value = {"rec": []}
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, verify_details=False, target_codes=["16AFR10H"])
        self.assertIn("16AFR10H", result.codes_checked or [])
        self.assertNotIn("notebook rtx 5080", result.queries_finished or [])
        report = coverage_for_result(result)
        self.assertEqual(report["queries_not_completed"], ["notebook rtx 5080"])
        self.assertIn("16AFR10H", report["queries_finished"])

    def test_advice_429_before_regional_sku_is_partial(self) -> None:
        from thailand.advice import collect

        def get(url, timeout=None):
            response = mock.Mock()
            response.headers = {"content-type": "text/html"}
            response.text = "<html></html>"
            response.status_code = 429 if "16AFR10H" in url else 200
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, target_codes=["16AFR10H"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "RATE_LIMITED")
        self.assertEqual(result.codes_checked, [])
        self.assertIn("16AFR10H", result.discovery_queries or [])
        self.assertLess(result.queries_completed or 0, len(result.discovery_queries or []))

    def test_unchecked_code_is_not_marked_found_or_missing(self) -> None:
        limited = StoreScanResult(
            store="jib",
            ok=False,
            offers=[_offer()],
            error_code="RATE_LIMITED",
            collection_mode="http",
        )
        limited.targeted_codes = ["A2XWHG-814XRU", "16AFR10H"]
        limited.codes_checked = ["A2XWHG-814XRU"]
        limited.discovery_rate_limited = True
        coverage = build_search_coverage(["A2XWHG-814XRU", "16AFR10H"], [limited])
        row = coverage["stores"][0]
        self.assertEqual(row["mode"], "partial")
        self.assertEqual(row["codes"], ["A2XWHG-814XRU"])
        self.assertEqual(row["codes_not_checked"], ["16AFR10H"])

    def test_disabled_stores_stay_disabled(self) -> None:
        from thailand.registry import get_thailand_adapters

        enabled = {adapter.slug: adapter.enabled for adapter in get_thailand_adapters()}
        self.assertFalse(enabled["banana"])
        self.assertFalse(enabled["lazada"])
        self.assertTrue(enabled["jib"])
        self.assertTrue(enabled["advice"])
        self.assertTrue(enabled["itcity"])
        self.assertTrue(enabled["invadeit"])
        self.assertTrue(enabled["speedcom"])


if __name__ == "__main__":
    unittest.main()
