"""Personal TOP-3 codes reach the Thailand collectors in one shared pass."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import config
from personal_top import assess_model, coverage_lines, format_personal_board, target_for
from thailand.errors import RATE_LIMITED
from thailand.models import FxRate, StoreScanResult
from thailand.scanner import (
    build_search_coverage,
    collect_thailand_offers,
    collector_search_codes,
    run_thailand_scan,
)


NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
CODES = ["9S7-15M361-814", "A2XWHG-814XRU", "G615LR-S5348", "16AFR10H"]


def _targets() -> list[dict]:
    return [target_for(model, {}) for model in config.PERSONAL_TOP3]


class CollectorCodeTests(unittest.TestCase):
    def test_personal_codes_include_exact_skus_and_lenovo_series_only(self) -> None:
        targets = _targets()
        codes = collector_search_codes(None, targets)
        self.assertEqual(codes, CODES)
        self.assertNotIn("G615LR", codes)
        lenovo = targets[2]
        self.assertEqual(list(lenovo["model_codes"]), [])
        self.assertIn("16AFR10H", lenovo["series_codes"])

    def test_scan_sends_every_personal_code_in_one_collect(self) -> None:
        calls: list[list[str]] = []

        def fake_collect(**kwargs):
            calls.append(list(kwargs.get("target_codes") or []))
            return [], [], [], 0.0

        fx = FxRate(
            source="test",
            currency="THB",
            nominal=1,
            official_rate=2.5,
            rub_per_thb=2.5,
            published_date="2026-10-12",
            fetched_at=NOW,
        )
        with (
            mock.patch("thailand.scanner.fetch_cbr_thb_rate", return_value=fx),
            mock.patch("thailand.scanner.collect_thailand_offers", side_effect=fake_collect),
        ):
            run_thailand_scan(
                trigger_type="personal_manual",
                personal_targets=_targets(),
                write_snapshot=False,
            )
        self.assertEqual(calls, [CODES])

    def test_stub_adapters_receive_the_three_targets_once(self) -> None:
        class Targeted:
            slug = "speedcom"
            enabled = True
            calls = 0
            codes: list[str] = []

            def collect(self, timeout=60.0, target_codes=None):
                self.calls += 1
                self.codes = list(target_codes or [])
                return StoreScanResult(store=self.slug, ok=True, offers=[], collection_mode="http")

        class Catalog:
            slug = "jib"
            enabled = True
            calls = 0

            def collect(self, timeout=60.0):
                self.calls += 1
                return StoreScanResult(store=self.slug, ok=True, offers=[], collection_mode="http")

        targeted = Targeted()
        catalog = Catalog()
        with (
            mock.patch("thailand.scanner.get_thailand_adapters", return_value=[targeted, catalog]),
            mock.patch("thailand.scanner.skip_reason", return_value=None),
            mock.patch("thailand.scanner.record_observation"),
        ):
            results, _, _, _ = collect_thailand_offers(target_codes=CODES, parallel=False)
        self.assertEqual(targeted.calls, 1)
        self.assertEqual(catalog.calls, 1)
        self.assertEqual(targeted.codes, CODES)
        by_store = {row.store: row for row in results}
        self.assertEqual(by_store["speedcom"].targeted_codes, CODES)
        self.assertEqual(by_store["speedcom"].codes_checked, CODES)
        self.assertEqual(by_store["jib"].targeted_codes, [])
        coverage = build_search_coverage(CODES, results)
        modes = {row["store"]: row["mode"] for row in coverage["stores"]}
        self.assertEqual(modes["speedcom"], "targeted")
        self.assertEqual(modes["jib"], "catalog")


class AdapterSupportTests(unittest.TestCase):
    def test_keyword_stores_accept_target_codes(self) -> None:
        import inspect

        from thailand.registry import get_thailand_adapters

        accepting = sorted(
            adapter.slug
            for adapter in get_thailand_adapters()
            if "target_codes" in inspect.signature(adapter.collect).parameters
        )
        self.assertEqual(accepting, ["advice", "itcity", "jib", "speedcom"])


class SpeedComCodeBudgetTests(unittest.TestCase):
    def test_one_429_stops_the_remaining_codes(self) -> None:
        from thailand.speedcom import collect

        seen: list[str] = []

        def get(url):
            seen.append(url)
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            response.text = ""
            if "suggest.json" in url:
                suggests = [item for item in seen if "suggest.json" in item]
                if len(suggests) >= 2:
                    response.status_code = 429
                    response.headers = {"content-type": "text/plain", "Retry-After": "30"}
                    return response
                response.status_code = 200
                response.json.return_value = {"resources": {"results": {"products": []}}}
                return response
            response.status_code = 200
            response.json.return_value = {"products": []}
            return response

        client = mock.Mock()
        client.get.side_effect = get
        result = collect(client=client, target_codes=["A2XWHG-814XRU", "G615LR-S5348", "16AFR10H"])
        suggests = [url for url in seen if "suggest.json" in url]
        self.assertEqual(result.error_code, RATE_LIMITED)
        self.assertFalse(result.ok)
        self.assertEqual(len(suggests), 2)
        self.assertIn("A2XWHG-814XRU", suggests[0])
        self.assertTrue(result.discovery_rate_limited)
        self.assertFalse(any("16AFR10H" in url for url in seen))
        self.assertEqual(result.codes_checked, ["A2XWHG-814XRU"])
        self.assertNotIn("G615LR-S5348", result.codes_checked)


class CoverageTextTests(unittest.TestCase):
    def test_not_found_stays_separate_from_not_checked(self) -> None:
        search = {
            "exact_matches": [],
            "same_family_matches": [],
            "equivalent_matches": [],
            "series_candidates": [],
            "stores_skipped": [],
            "coverage": {
                "codes": ["A2XWHG-814XRU"],
                "stores": [
                    {
                        "store": "speedcom",
                        "checked": True,
                        "mode": "targeted",
                        "codes": ["A2XWHG-814XRU"],
                        "reason": None,
                    },
                    {
                        "store": "jib",
                        "checked": True,
                        "mode": "catalog",
                        "codes": [],
                        "reason": "http",
                    },
                    {
                        "store": "itcity",
                        "checked": False,
                        "mode": "not_checked",
                        "codes": [],
                        "reason": "RATE_LIMITED",
                    },
                ],
            },
        }
        card = assess_model(
            config.PERSONAL_TOP3[0],
            {"price": None, "candidates": []},
            search,
            fx={"rub_per_thb": 2.5, "stale": False, "published_date": "2026-10-12"},
            checked_at=NOW.isoformat(),
            now=NOW,
        )
        text = format_personal_board([card])
        self.assertIn("Таиланд: точной модели нет.", text)
        self.assertIn("A2XWHG-814XRU: адресный поиск SpeedCom; каталог JIB; не проверено IT City (лимит запросов)", text)
        self.assertNotIn("не найдено", text)
        self.assertEqual(card["verdict"], "no_fresh_thai")

    def test_unchecked_stores_are_not_reported_as_missing(self) -> None:
        search = {
            "exact_matches": [],
            "same_family_matches": [],
            "equivalent_matches": [],
            "coverage": {
                "codes": ["16AFR10H"],
                "stores": [
                    {
                        "store": "advice",
                        "checked": False,
                        "mode": "not_checked",
                        "codes": [],
                        "reason": "RATE_LIMITED",
                    }
                ],
            },
        }
        card = assess_model(
            config.PERSONAL_TOP3[2],
            {"price": None, "candidates": []},
            search,
            fx={"rub_per_thb": 2.5, "stale": False},
            checked_at=NOW.isoformat(),
            now=NOW,
        )
        text = "\n".join(coverage_lines(card["coverage"]))
        board = format_personal_board([card])
        self.assertEqual(card["verdict"], "not_checked")
        self.assertIn("Таиланд: не проверено.", board)
        self.assertNotIn("точной модели нет", board)
        self.assertIn("16AFR10H: не проверено Advice (лимит запросов)", text)


if __name__ == "__main__":
    unittest.main()
