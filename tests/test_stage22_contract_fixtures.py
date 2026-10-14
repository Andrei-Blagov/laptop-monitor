"""Offline baseline checks for the synthetic Stage 2.2 contract fixture.

These checks validate test data and agreed policy examples only. They do not
claim the currently deployed matching implementation supports Stage 2.2.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


_FIXTURE = Path(__file__).parent / "fixtures" / "stage22" / "acceptance_cases.json"


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class Stage22ContractsFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _load()
        cls.cases = {case["id"]: case for case in cls.fixture["cases"]}

    def test_cases_are_synthetic_and_unique(self) -> None:
        data = self.fixture
        self.assertEqual(data["fixture_schema_version"], 1)
        self.assertIs(data["all_data_synthetic"], True)
        self.assertGreaterEqual(len(data["cases"]), 15)
        self.assertEqual(len(self.cases), len(data["cases"]))
        self.assertTrue(all("expected" in case for case in data["cases"]))

    def test_six_core_fields_present_on_matching_examples(self) -> None:
        required = {"gpu", "cpu", "ram_gb", "ssd_gb", "screen_size_inch", "screen_resolution"}
        for case in self.fixture["cases"]:
            for side in ("ru_offers", "th_offers"):
                for offer in case.get(side, []):
                    self.assertTrue(required <= set(offer["specs"]), case["id"])

    def test_russian_match_outside_top_and_job_cap(self) -> None:
        case = self.cases["R1_ALL_RU_NOT_TOP"]
        self.assertEqual(len(case["ru_offers"]), 25)
        self.assertEqual(len(case["ru_top_offer_ids"]), 10)
        self.assertEqual(case["expected"]["ru_match_offer_ids"], ["ru-017"])
        self.assertNotIn("ru-017", case["ru_top_offer_ids"])

    def test_country_gap_example_uses_decimal_and_signed_direction(self) -> None:
        case = self.cases["M1_EXACT_SAVING"]
        rub = Decimal(str(case["ru_offers"][0]["price_rub"]))
        thb = Decimal(str(case["th_offers"][0]["price_thb"]))
        fx = Decimal(case["fx_rub_per_thb"])
        costs = Decimal(case["known_costs_rub"])
        saving = rub - (thb * fx + costs)
        saving_pct = saving / rub * 100
        self.assertEqual(str(saving), case["expected"]["saving_rub"])
        self.assertAlmostEqual(float(saving_pct), float(case["expected"]["saving_pct_approx"]), places=7)

    def test_no_fake_savings_from_incomplete_or_conflicting_pairs(self) -> None:
        for id_ in (
            "C3_SAME_MPN_RESOLUTION_CONFLICT",
            "M3_CPU_MODEL_DIFFERENT",
            "M7_OTHER_BRAND_SAME_CORE",
            "C4_MISSING_CPU",
            "U1_TH_ONLY_VALUE",
        ):
            expected = self.cases[id_]["expected"]
            self.assertIsNone(expected["saving_pct"], id_)
            if "saving_rub" in expected:
                self.assertIsNone(expected["saving_rub"], id_)

    def test_reuse_is_decided_by_offer_observation_age(self) -> None:
        case = self.cases["F1_REUSE_FRESH"]
        now = _utc(case["now"])
        for field in ("price_checked_at", "availability_checked_at"):
            age_hours = (now - _utc(case[field])).total_seconds() / 3600
            self.assertGreaterEqual(age_hours, 0)
            self.assertLessEqual(age_hours, 6)
        self.assertEqual(case["expected"]["broad_store_http_calls"], 0)
        self.assertEqual(case["expected"]["detail_store_http_calls"], 0)

        stale = self.cases["F4_NO_OLD_OFFER_REVIVAL"]
        self.assertLess((_utc(stale["now"]) - _utc(stale["catalog_finished_at"])).total_seconds(), 3600)
        self.assertGreater((_utc(stale["now"]) - _utc(stale["price_checked_at"])).total_seconds(), 6 * 3600)
        self.assertIs(stale["expected"]["current_purchase_eligible"], False)

    def test_fx_staleness_is_not_hidden_by_snapshot_reuse(self) -> None:
        case = self.cases["FX1_STALE"]
        age_hours = (_utc(case["now"]) - _utc(case["fx_fetched_at"])).total_seconds() / 3600
        self.assertGreater(age_hours, 36)
        self.assertIs(case["expected"]["fx_usable"], False)

    def test_coalesced_events_keep_every_recipient(self) -> None:
        case = self.cases["Q1_COALESCE"]
        self.assertEqual(len({event["event_id"] for event in case["events"]}), 3)
        self.assertEqual(case["expected"]["heavy_scan_count"], 1)
        self.assertEqual(case["expected"]["delivered_event_count"], len(case["events"]))
        self.assertEqual(case["expected"]["repeat_russian_buy_count"], 0)


if __name__ == "__main__":
    unittest.main()
