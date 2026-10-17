"""Validate the premises and expectations of all Stage 2.2-A policy examples.

These are intentionally simplified synthetic fixtures, not runtime snapshots.
No production matcher, scanner, queue or delivery code is invoked. Passing here
validates the contract examples; implementation acceptance belongs to B/C/D/E/F.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


_FIXTURE = Path(__file__).parent / "fixtures" / "stage22" / "acceptance_cases.json"
_CORE = {"gpu", "cpu", "ram_gb", "ssd_gb", "screen_size_inch", "screen_resolution"}
_CASE_KINDS = {
    "R1_ALL_RU_NOT_TOP": "cross_market_match",
    "R2_EMPTY_RU": "th_independence",
    "M1_EXACT_SAVING": "country_gap",
    "C3_SAME_MPN_RESOLUTION_CONFLICT": "config_conflict",
    "M2_REGIONAL_FULL_SPEC": "regional_equivalence",
    "M3_CPU_MODEL_DIFFERENT": "config_conflict",
    "M7_OTHER_BRAND_SAME_CORE": "analogue_only",
    "C4_MISSING_CPU": "incomplete_comparison",
    "O1_STOCK_OUT": "availability_gate",
    "U1_TH_ONLY_VALUE": "th_unmatched_value",
    "F1_REUSE_FRESH": "cache",
    "F4_NO_OLD_OFFER_REVIVAL": "cache",
    "F2_REFRESH_OLDER_QUOTE": "cache",
    "FX1_STALE": "fx",
    "Q1_COALESCE": "events",
    "S1_429_PARTIAL": "source_limits",
}


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _load() -> dict:
    return json.loads(
        _FIXTURE.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.utcoffset() != timedelta(0):
        raise ValueError("contract timestamps must use explicit UTC")
    return value.astimezone(timezone.utc)


def _decimal(raw: object) -> Decimal:
    if isinstance(raw, bool):
        raise ValueError("bool is not a money value")
    value = Decimal(str(raw))
    if not value.is_finite():
        raise ValueError("money must be finite")
    return value


class Stage22ContractsFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _load()
        cls.cases = {case["id"]: case for case in cls.fixture["cases"]}

    def test_cases_are_synthetic_unique_and_all_supported(self) -> None:
        data = self.fixture
        self.assertEqual(data["fixture_schema_version"], 1)
        self.assertIs(data["all_data_synthetic"], True)
        self.assertEqual(data["fixture_format"], "simplified_policy_examples_not_runtime_snapshots")
        self.assertEqual(len(data["cases"]), 16)
        self.assertEqual(len(self.cases), len(data["cases"]))
        self.assertEqual({case["id"]: case["kind"] for case in data["cases"]}, _CASE_KINDS)
        self.assertTrue(data["assumptions"])
        self.assertEqual(_utc(data["reference_now"]), datetime(2026, 10, 14, 12, tzinfo=timezone.utc))

    def test_fixture_policy_records_agreed_values(self) -> None:
        p = self.fixture["policy"]
        self.assertEqual(set(p["core_fields"]), _CORE)
        self.assertEqual(len(p["core_fields"]), 6)
        self.assertEqual(
            (p["ru_offer_fresh_minutes"], p["catalog_fresh_hours"], p["quote_fresh_hours"],
             p["daily_min_broad_hours"], p["fx_fresh_hours"], p["fx_published_max_days"]),
            (180, 26, 6, 18, 36, 4),
        )
        self.assertEqual(_decimal(p["max_price_rub"]), Decimal("330000"))
        self.assertEqual(p["unmatched_score_min"], 75)
        self.assertEqual(p["gpu_target_rub"], {"RTX 5070 Ti Laptop": "230000", "RTX 5080 Laptop": "300000"})
        self.assertEqual(p["country_band_percent"], {"slight": "5", "clear": "10"})

    def test_matching_examples_have_own_core_specs_and_offer_ids(self) -> None:
        for case in self.fixture["cases"]:
            ids = []
            for side, price_key in (("ru_offers", "price_rub"), ("th_offers", "price_thb")):
                for offer in case.get(side, []):
                    with self.subTest(case=case["id"], offer=offer["offer_id"]):
                        self.assertEqual(set(offer["specs"]), _CORE)
                        self.assertTrue(offer["manufacturer_part_number"])
                        self.assertTrue(offer["brand"])
                        self.assertIs(offer["verified"], True)
                        self.assertGreater(_decimal(offer[price_key]), 0)
                        self.assertIn(offer["specs"]["gpu"], self.fixture["policy"]["gpu_target_rub"])
                        self.assertGreaterEqual(offer["specs"]["ram_gb"], 32)
                        self.assertGreaterEqual(offer["specs"]["ssd_gb"], 1000)
                        ids.append(offer["offer_id"])
            self.assertEqual(len(ids), len(set(ids)), case["id"])

    def test_r1_match_is_really_the_seventeenth_offer_outside_top(self) -> None:
        c = self.cases["R1_ALL_RU_NOT_TOP"]
        offers = c["ru_offers"]
        th = c["th_offers"][0]
        self.assertEqual(len(offers), 25)
        self.assertEqual(len(c["ru_top_offer_ids"]), 10)
        self.assertTrue(set(c["ru_top_offer_ids"]) <= {o["offer_id"] for o in offers})
        hits = [o for o in offers if o["manufacturer_part_number"] == th["manufacturer_part_number"]]
        self.assertEqual(hits, [offers[16]])
        self.assertEqual(hits[0]["specs"], th["specs"])
        self.assertEqual(hits[0]["brand"], th["brand"])
        self.assertNotIn(hits[0]["offer_id"], c["ru_top_offer_ids"])
        self.assertEqual(c["expected"], {"ru_eligible_count": 25, "ru_match_offer_ids": ["ru-017"], "match_class": "EXACT", "found_outside_ru_top": True})

    def test_r2_empty_ru_has_an_independent_thai_candidate(self) -> None:
        c = self.cases["R2_EMPTY_RU"]
        self.assertEqual(c["ru_offers"], [])
        self.assertTrue(c["th_offers"])
        self.assertEqual(c["th_offers"][0]["availability_status"], "in_stock")
        self.assertEqual(c["expected"], {"th_discovery_still_runs": True, "th_top_can_be_nonempty": True, "no_ru_match": True})

    def test_m1_confirmed_pair_gap_is_computed_in_decimal(self) -> None:
        c = self.cases["M1_EXACT_SAVING"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertEqual(ru["brand"], th["brand"])
        self.assertEqual(ru["manufacturer_part_number"], th["manufacturer_part_number"])
        self.assertEqual(ru["specs"], th["specs"])
        saving = _decimal(ru["price_rub"]) - (_decimal(th["price_thb"]) * _decimal(c["fx_rub_per_thb"]) + _decimal(c["known_costs_rub"]))
        pct = saving / _decimal(ru["price_rub"]) * 100
        self.assertEqual(saving, _decimal(c["expected"]["saving_rub"]))
        self.assertLess(abs(pct - _decimal(c["expected"]["saving_pct_approx"])), Decimal("0.0000000001"))
        self.assertGreaterEqual(pct, _decimal(self.fixture["policy"]["country_band_percent"]["clear"]))
        self.assertEqual(c["expected"], {"match_class": "EXACT", "comparison_status": "confirmed", "saving_rub": "30000", "saving_pct_approx": "13.0434782609", "band": "clear"})

    def test_c3_same_mpn_really_has_a_resolution_conflict(self) -> None:
        c = self.cases["C3_SAME_MPN_RESOLUTION_CONFLICT"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertEqual(ru["manufacturer_part_number"], th["manufacturer_part_number"])
        self.assertEqual({k for k in _CORE if ru["specs"][k] != th["specs"][k]}, {"screen_resolution"})
        self.assertEqual(c["expected"], {"comparison_status": "conflict", "match_class_not": "EXACT", "saving_rub": None, "saving_pct": None})

    def test_m2_regional_equivalence_has_explicit_family_generation(self) -> None:
        c = self.cases["M2_REGIONAL_FULL_SPEC"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertNotEqual(ru["manufacturer_part_number"], th["manufacturer_part_number"])
        self.assertEqual(ru["brand"], th["brand"])
        self.assertEqual(ru["manufacturer_family"], c["manufacturer_family"])
        self.assertEqual(ru["manufacturer_family"], th["manufacturer_family"])
        self.assertTrue(ru["manufacturer_generation"])
        self.assertEqual(ru["manufacturer_generation"], th["manufacturer_generation"])
        self.assertEqual(ru["specs"], th["specs"])
        self.assertEqual(c["expected"], {"match_class": "EQUIVALENT", "not_exact": True, "comparison_status": "confirmed", "show_regional_difference": True})

    def test_m3_cpu_class_does_not_hide_different_models(self) -> None:
        c = self.cases["M3_CPU_MODEL_DIFFERENT"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertEqual(ru["manufacturer_part_number"], th["manufacturer_part_number"])
        self.assertEqual({k for k in _CORE if ru["specs"][k] != th["specs"][k]}, {"cpu"})
        self.assertTrue(ru["specs"]["cpu"].startswith("Intel Core Ultra 9"))
        self.assertTrue(th["specs"]["cpu"].startswith("Intel Core Ultra 9"))
        self.assertEqual(c["expected"], {"comparison_status": "conflict", "saving_rub": None, "saving_pct": None})

    def test_m7_equal_core_different_brand_is_an_alternative(self) -> None:
        c = self.cases["M7_OTHER_BRAND_SAME_CORE"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertNotEqual(ru["brand"], th["brand"])
        self.assertNotEqual(ru["manufacturer_part_number"], th["manufacturer_part_number"])
        self.assertEqual(ru["specs"], th["specs"])
        self.assertEqual(c["expected"], {"match_class": "ALTERNATIVE", "saving_rub": None, "saving_pct": None})

    def test_c4_missing_cpu_is_not_equality(self) -> None:
        c = self.cases["C4_MISSING_CPU"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertEqual(ru["manufacturer_part_number"], th["manufacturer_part_number"])
        self.assertEqual({k for k in _CORE if ru["specs"][k] is None}, {"cpu"})
        self.assertTrue(all(th["specs"][k] is not None for k in _CORE))
        self.assertEqual(c["expected"], {"comparison_status": "incomplete", "saving_rub": None, "saving_pct": None})

    def test_o1_out_of_stock_quote_cannot_support_purchase(self) -> None:
        c = self.cases["O1_STOCK_OUT"]
        ru, th = c["ru_offers"][0], c["th_offers"][0]
        self.assertEqual(ru["specs"], th["specs"])
        self.assertEqual(ru["availability_status"], "in_stock")
        self.assertEqual(th["availability_status"], "out_of_stock")
        self.assertEqual(c["expected"], {"current_purchase_eligible": False, "current_price_verdict": False})

    def test_u1_unmatched_value_is_under_its_own_gpu_target(self) -> None:
        c = self.cases["U1_TH_ONLY_VALUE"]
        self.assertEqual(c["ru_offers"], [])
        th = c["th_offers"][0]
        rub = _decimal(th["price_thb"]) * _decimal(c["fx_rub_per_thb"])
        target = _decimal(self.fixture["policy"]["gpu_target_rub"][th["specs"]["gpu"]])
        self.assertEqual(rub, _decimal(c["expected"]["th_price_rub"]))
        self.assertLessEqual(rub, target)
        self.assertGreaterEqual(c["input_value_score"], self.fixture["policy"]["unmatched_score_min"])
        self.assertEqual(c["expected"], {"unmatched_ru": True, "th_price_rub": "250000", "unmatched_value_candidate": True, "saving_pct": None})

    def test_f1_catalogue_and_both_observations_are_fresh(self) -> None:
        c = self.cases["F1_REUSE_FRESH"]
        now = _utc(c["now"])
        for field, limit in (("discovery_checked_at", 26), ("price_checked_at", 6), ("availability_checked_at", 6)):
            self.assertLessEqual(timedelta(0), now - _utc(c[field]))
            self.assertLessEqual(now - _utc(c[field]), timedelta(hours=limit))
        self.assertEqual(set(c["events"]), {"manual", "daily", "buy"})
        self.assertEqual(c["expected"], {"broad_store_http_calls": 0, "detail_store_http_calls": 0, "one_catalogue_id_for_events": True})

    def test_f4_new_manifest_does_not_revive_quotes_or_discovery(self) -> None:
        c = self.cases["F4_NO_OLD_OFFER_REVIVAL"]
        now = _utc(c["now"])
        self.assertLess(now - _utc(c["catalog_finished_at"]), timedelta(hours=1))
        for field in ("price_checked_at", "availability_checked_at"):
            self.assertGreater(now - _utc(c[field]), timedelta(hours=6))
        self.assertGreater(now - _utc(c["discovery_checked_at"]), timedelta(hours=26))
        self.assertEqual(c["expected"], {"current_purchase_eligible": False, "old_observation_keeps_original_time": True, "broad_discovery_required": True})

    def test_f2_old_quotes_can_use_a_compatible_catalogue(self) -> None:
        c = self.cases["F2_REFRESH_OLDER_QUOTE"]
        now = _utc(c["now"])
        self.assertLessEqual(now - _utc(c["discovery_checked_at"]), timedelta(hours=26))
        for field in ("price_checked_at", "availability_checked_at"):
            self.assertGreater(now - _utc(c[field]), timedelta(hours=6))
        self.assertEqual(c["expected"], {"broad_discovery_required": False, "bounded_offer_refresh_required": True})

    def test_fx1_expired_retrieval_cannot_support_current_rubles(self) -> None:
        c = self.cases["FX1_STALE"]
        self.assertGreater(_utc(c["now"]) - _utc(c["fx_fetched_at"]), timedelta(hours=36))
        self.assertLessEqual((_utc(c["now"]).date() - date.fromisoformat(c["fx_published_date"])).days, 4)
        self.assertEqual(c["expected"], {"fx_usable": False, "allow_current_rub_verdict": False, "preserve_thb": True})

    def test_q1_every_subscriber_and_recipient_is_distinct(self) -> None:
        c = self.cases["Q1_COALESCE"]
        events = c["events"]
        self.assertEqual(len(events), 3)
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertEqual({e["trigger"] for e in events}, {"daily", "buy", "manual"})
        recipients = [r for e in events for r in e["recipients"]]
        self.assertEqual(len(recipients), 3)
        self.assertEqual(len(set(recipients)), 3)
        self.assertTrue(c["discovery_scope_hash"])
        self.assertEqual(c["expected"], {"heavy_scan_count": 1, "delivered_event_count": 3, "repeat_russian_buy_count": 0})

    def test_s1_429_has_completed_and_unfinished_work(self) -> None:
        c = self.cases["S1_429_PARTIAL"]
        self.assertEqual(c["response_at_next_request"], 429)
        self.assertTrue(c["finished_queries"])
        self.assertTrue(c["not_completed_queries"])
        self.assertFalse(set(c["finished_queries"]) & set(c["not_completed_queries"]))
        self.assertEqual(c["expected"], {"store_calls_after_429": 0, "scan_status": "partial", "keep_previous_offers": True, "not_completed_is_not_no_results": True})

    def test_freshness_boundaries_are_inclusive_and_not_future(self) -> None:
        now = _utc(self.fixture["reference_now"])
        for hours in (3, 6, 26, 36):
            with self.subTest(hours=hours):
                limit = timedelta(hours=hours)
                self.assertTrue(timedelta(0) <= now - (now - limit) <= limit)
                self.assertFalse(timedelta(0) <= now - (now - limit - timedelta(microseconds=1)) <= limit)
                self.assertFalse(timedelta(0) <= now - (now + timedelta(microseconds=1)) <= limit)

    def test_fx_publication_guard_uses_moscow_calendar_date(self) -> None:
        # Near midnight UTC, the official local date is already the next day.
        from zoneinfo import ZoneInfo
        now = _utc("2026-10-14T22:30:00Z")
        local_date = now.astimezone(ZoneInfo("Europe/Moscow")).date()
        self.assertEqual(local_date, date(2026, 10, 15))
        for published, usable in (("2026-10-15", True), ("2026-10-11", True), ("2026-10-10", False), ("2026-10-16", False)):
            age = (local_date - date.fromisoformat(published)).days
            self.assertEqual(0 <= age <= 4, usable, published)

    def test_country_band_boundaries_are_symmetric(self) -> None:
        for gap, expected in (("0", "comparable"), ("4.999", "comparable"), ("5", "slight"), ("9.999", "slight"), ("10", "clear"), ("10.001", "clear")):
            for sign in (1, -1):
                pct = _decimal(gap) * sign
                magnitude = abs(pct)
                band = "comparable" if magnitude < 5 else "slight" if magnitude < 10 else "clear"
                self.assertEqual(band, expected, pct)

    def test_budget_cutoff_uses_unrounded_decimal(self) -> None:
        cap = _decimal(self.fixture["policy"]["max_price_rub"])
        self.assertLessEqual(Decimal("132000") * Decimal("2.5"), cap)
        above = Decimal("132000.004") * Decimal("2.5")
        self.assertGreater(above, cap)
        self.assertEqual(above.quantize(Decimal("1")), cap)

    def test_expenses_can_reverse_the_price_advantage(self) -> None:
        c = self.cases["M1_EXACT_SAVING"]
        ru = _decimal(c["ru_offers"][0]["price_rub"])
        base = _decimal(c["th_offers"][0]["price_thb"]) * _decimal(c["fx_rub_per_thb"])
        self.assertEqual(ru - (base + Decimal("35000")), Decimal("-5000"))

    def test_timestamp_format_rejects_naive_and_non_utc(self) -> None:
        for raw in ("2026-10-14T12:00:00", "2026-10-14T15:00:00+03:00"):
            with self.assertRaises(ValueError):
                _utc(raw)
        self.assertEqual(_utc("2026-10-14T12:00:00Z"), _utc("2026-10-14T12:00:00+00:00"))

    def test_duplicate_json_keys_and_non_finite_money_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _unique_object([("id", "first"), ("id", "second")])
        for raw in (True, "NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                _decimal(raw)
        with self.assertRaises(ValueError):
            json.loads('{"price": NaN}', parse_constant=_reject_constant)


if __name__ == "__main__":
    unittest.main()
