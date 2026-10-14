"""Wide Thailand discovery is not limited to the personal TOP-3."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import config
from laptop_eligibility import evaluate_purchase
from thailand.discovery import (
    GPU_SEARCH_QUERIES,
    LINE_SEARCH_QUERIES,
    coverage_for_offers,
    search_queries,
)
from thailand.models import ThailandOffer


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
    )
    base.update(kwargs)
    return ThailandOffer(**base)


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


class CoverageTests(unittest.TestCase):
    def test_incomplete_offer_is_kept_out_of_the_purchase_gate(self) -> None:
        class Fx:
            rub_per_thb = 2.5

        incomplete = _offer(external_id="partial", ram_gb=None, ssd_gb=None, screen_resolution=None)
        ready = _offer(external_id="ready", price_thb=80_000)
        report = coverage_for_offers([incomplete, ready], fx=Fx())
        self.assertEqual(report["discovered"], 2)
        self.assertEqual(report["spec_checked"], 1)
        self.assertEqual(report["meets_requirements"], 1)
        self.assertGreater(report["exclusion_reasons"].get("SPECS_MISSING", 0), 0)
        blocked = evaluate_purchase(
            price_rub=200_000,
            gpu=incomplete.gpu,
            ram_gb=incomplete.ram_gb,
            ssd_gb=incomplete.ssd_gb,
            screen_resolution=incomplete.screen_resolution,
        )
        self.assertFalse(blocked.eligible)

    def test_confirmed_offer_meets_the_shared_gate(self) -> None:
        class Fx:
            rub_per_thb = 2.5

        report = coverage_for_offers([_offer()], fx=Fx())
        self.assertEqual(report["meets_requirements"], 1)
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
