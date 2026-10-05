from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from deal_ranking import RankedDeal
from purchase_recommendation import (
    BUY_NOW_RUSSIA,
    GOOD_PRICE_NOT_URGENT,
    INSUFFICIENT_DATA,
    THAILAND_BETTER,
    WAIT,
    build_recommendation,
    country_price_gap,
    format_purchase_recommendation,
)
from control_bot import (
    _keyboard,
    handle_recommendation_menu,
    handle_recommendation_pick,
    process_update,
    recommendation_keyboard,
)
from buy_thailand_flow import enqueue_recommendation_job, evaluate_buy_opportunity_flow, execute_thailand_job

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
MATURE = "2020-01-01T00:00:00+00:00"
IMMATURE = (NOW - timedelta(days=8)).isoformat()


def _deal(**kwargs) -> RankedDeal:
    defaults = dict(
        score=80.0,
        reasons=["x"],
        offer={"store": "citilink", "external_id": "1", "sku": "G614PR-RV027", "mpn": "90NR0NJ7-M001J0"},
        cluster_name="ASUS ROG Strix G16 G614PR-RV027",
        gpu="RTX 5070 Ti",
        store="citilink",
        price=220_000,
        url="https://example.test/g",
        ram_gb=16,
        ssd_gb=1024,
        screen_inch=16.0,
        cpu="AMD Ryzen 9 8940HX",
        confidence=90,
        historical_min=220_000,
        history_started_at=MATURE,
    )
    defaults.update(kwargs)
    return RankedDeal(**defaults)


def _row(price: int, *, store: str = "jib", oos: bool = False, diffs: list[str] | None = None) -> dict:
    return {
        "store": store,
        "price_rub": price,
        "price_thb": price,
        "out_of_stock": oos,
        "name": "ASUS ROG Strix G16 G614PR-TS113W",
        "spec_differences": diffs or [],
        "manufacturer_part_number": "G614PR-TS113W",
    }


def _search(level: str, rows: list[dict]) -> dict:
    key = {
        "EXACT": "exact_matches",
        "SAME_FAMILY": "same_family_matches",
        "EQUIVALENT": "equivalent_matches",
    }.get(level)
    found = {"match_level": level, "exact_matches": [], "same_family_matches": [], "equivalent_matches": []}
    if key:
        found[key] = rows
    return found


class DecisionTests(unittest.TestCase):
    def test_strong_buy_comparable_thailand_is_buy_now(self) -> None:
        rec = build_recommendation(_deal(), _search("EXACT", [_row(215_000)]), now=NOW)
        self.assertEqual(rec.verdict, BUY_NOW_RUSSIA)
        self.assertTrue(rec.history_mature)
        self.assertEqual(rec.buy_level, "STRONG_BUY")

    def test_exact_15_percent_cheaper_is_thailand(self) -> None:
        rec = build_recommendation(_deal(), _search("EXACT", [_row(187_000)]), now=NOW)
        self.assertEqual(rec.verdict, THAILAND_BETTER)
        self.assertGreaterEqual(rec.thailand_difference_pct or 0, 10)

    def test_same_family_same_specs_12_percent_is_thailand(self) -> None:
        deal = _deal(score=70)
        rec = build_recommendation(deal, _search("SAME_FAMILY", [_row(193_600)]), now=NOW)
        self.assertEqual(rec.buy_level, "BUY")
        self.assertEqual(rec.verdict, THAILAND_BETTER)

    def test_same_family_different_specs_has_no_country_verdict(self) -> None:
        rec = build_recommendation(
            _deal(),
            _search("SAME_FAMILY", [_row(150_000, diffs=["GPU: RTX 5070 Ti и RTX 5080"])]),
            now=NOW,
        )
        self.assertNotEqual(rec.verdict, THAILAND_BETTER)
        self.assertFalse(rec.country_comparison_trusted)
        self.assertIn("thai_specs_differ", rec.reasons)

    def test_equivalent_is_softer_wording(self) -> None:
        deal = _deal()
        rec = build_recommendation(deal, _search("EQUIVALENT", [_row(187_000)]), now=NOW)
        self.assertNotEqual(rec.verdict, THAILAND_BETTER)
        text = format_purchase_recommendation(deal, rec)
        self.assertIn("Сопоставимая конфигурация", text)
        self.assertNotIn("Эта модель в Таиланде дешевле", text)

    def test_out_of_stock_is_not_thailand_better(self) -> None:
        rec = build_recommendation(_deal(), _search("EXACT", [_row(150_000, oos=True)]), now=NOW)
        self.assertNotEqual(rec.verdict, THAILAND_BETTER)
        self.assertIn("thai_out_of_stock", rec.reasons)
        text = format_purchase_recommendation(_deal(), rec)
        self.assertIn("нет в наличии", text)

    def test_not_found_does_not_say_russia_is_cheaper(self) -> None:
        deal = _deal()
        rec = build_recommendation(deal, {"match_level": "NOT_FOUND"}, now=NOW)
        text = format_purchase_recommendation(deal, rec)
        self.assertNotEqual(rec.verdict, THAILAND_BETTER)
        self.assertIn("Прямого сравнения с Таиландом нет", text)
        self.assertNotIn("Россия дешевле", text)
        self.assertNotIn("Россия выгоднее", text)

    def test_rate_limit_adds_coverage_caveat(self) -> None:
        deal = _deal()
        rec = build_recommendation(
            deal,
            {"match_level": "NOT_FOUND"},
            stores=[{"store": "speedcom", "ok": True, "error_code": "RATE_LIMITED", "collection_mode": "rate_limited"}],
            now=NOW,
        )
        self.assertFalse(rec.thailand_coverage_complete)
        self.assertNotEqual(rec.verdict, THAILAND_BETTER)
        text = format_purchase_recommendation(deal, rec)
        self.assertIn("не проверена", text)
        self.assertNotIn("Россия дешевле", text)

    def test_immature_history_is_not_a_historical_best(self) -> None:
        deal = _deal(history_started_at=IMMATURE)
        rec = build_recommendation(deal, _search("EXACT", [_row(215_000)]), now=NOW)
        self.assertNotEqual(rec.verdict, BUY_NOW_RUSSIA)
        self.assertEqual(rec.verdict, INSUFFICIENT_DATA)
        text = format_purchase_recommendation(deal, rec)
        self.assertIn("исторически лучшее время", text)
        self.assertIn("не вывод", text)

    def test_far_above_minimum_waits(self) -> None:
        deal = _deal(price=280_000, historical_min=220_000, score=40, confidence=100)
        rec = build_recommendation(deal, {"match_level": "NOT_FOUND"}, now=NOW)
        self.assertIsNone(rec.buy_level)
        self.assertEqual(rec.verdict, WAIT)


def _rate_limited_stores() -> list[dict]:
    return [
        {"store": "jib", "ok": True, "error_code": None, "collection_mode": "http"},
        {"store": "speedcom", "ok": True, "error_code": "RATE_LIMITED", "collection_mode": "rate_limited"},
    ]


class CoverageTests(unittest.TestCase):
    def test_complete_coverage_comparable_is_buy_now(self) -> None:
        stores = [
            {"store": "jib", "ok": True, "collection_mode": "http"},
            {"store": "speedcom", "ok": True, "collection_mode": "http"},
        ]
        rec = build_recommendation(_deal(), _search("EXACT", [_row(215_000)]), stores=stores, now=NOW)
        self.assertTrue(rec.thailand_coverage_complete)
        self.assertEqual(rec.verdict, BUY_NOW_RUSSIA)

    def test_rate_limited_small_gap_is_not_buy_now(self) -> None:
        rec = build_recommendation(
            _deal(),
            _search("EXACT", [_row(211_200)]),
            stores=_rate_limited_stores(),
            now=NOW,
        )
        self.assertEqual(rec.buy_level, "STRONG_BUY")
        self.assertFalse(rec.thailand_coverage_complete)
        self.assertNotEqual(rec.verdict, BUY_NOW_RUSSIA)
        self.assertAlmostEqual(rec.thailand_difference_pct or 0, 4.0, places=1)

    def test_rate_limited_near_minimum_is_good_price(self) -> None:
        rec = build_recommendation(
            _deal(),
            _search("EXACT", [_row(211_200)]),
            stores=_rate_limited_stores(),
            now=NOW,
        )
        self.assertEqual(rec.ru_price_quality, "AT_HISTORICAL_LOW")
        self.assertEqual(rec.verdict, GOOD_PRICE_NOT_URGENT)

    def test_incomplete_coverage_and_poor_price_is_not_wait(self) -> None:
        deal = _deal(price=280_000, historical_min=220_000, score=40, confidence=100)
        rec = build_recommendation(
            deal,
            {"match_level": "NOT_FOUND"},
            stores=_rate_limited_stores(),
            now=NOW,
        )
        self.assertNotEqual(rec.verdict, WAIT)
        self.assertNotEqual(rec.verdict, BUY_NOW_RUSSIA)
        self.assertEqual(rec.verdict, INSUFFICIENT_DATA)

    def test_incomplete_coverage_exact_15_percent_stays_thailand(self) -> None:
        rec = build_recommendation(
            _deal(),
            _search("EXACT", [_row(187_000)]),
            stores=_rate_limited_stores(),
            now=NOW,
        )
        self.assertEqual(rec.verdict, THAILAND_BETTER)
        self.assertIn("thai_coverage_incomplete", rec.reasons)
        text = format_purchase_recommendation(_deal(), rec)
        self.assertIn("не проверена", text)

    def test_no_results_keeps_coverage_complete(self) -> None:
        stores = [{"store": "advice", "ok": True, "error_code": "NO_RESULTS", "collection_mode": "http"}]
        rec = build_recommendation(_deal(), _search("EXACT", [_row(215_000)]), stores=stores, now=NOW)
        self.assertTrue(rec.thailand_coverage_complete)
        self.assertEqual(rec.verdict, BUY_NOW_RUSSIA)

    def test_policy_disabled_stores_do_not_break_coverage(self) -> None:
        stores = [
            {"store": "jib", "ok": True, "collection_mode": "http"},
            {"store": "banana", "ok": True, "policy_disabled": True, "collection_mode": "disabled"},
            {"store": "lazada", "ok": True, "policy_disabled": True, "collection_mode": "disabled"},
        ]
        rec = build_recommendation(_deal(), _search("EXACT", [_row(215_000)]), stores=stores, now=NOW)
        self.assertTrue(rec.thailand_coverage_complete)
        self.assertEqual(rec.verdict, BUY_NOW_RUSSIA)


class FormatterTests(unittest.TestCase):
    def _text(self, price: int, hist: int, *, mature: bool = True) -> str:
        deal = _deal(
            price=price,
            historical_min=hist,
            history_started_at=MATURE if mature else IMMATURE,
            score=40,
            confidence=50,
        )
        rec = build_recommendation(deal, {"match_level": "NOT_FOUND"}, now=NOW)
        return format_purchase_recommendation(deal, rec)

    def test_zero_percent_is_at_minimum(self) -> None:
        text = self._text(220_000, 220_000)
        self.assertIn("у исторического минимума", text)

    def test_0_8_percent_is_at_historical_low(self) -> None:
        text = self._text(220_000, 218_254)
        self.assertIn("у исторического минимума", text)
        self.assertNotIn("около исторического минимума", text)

    def test_1_6_percent_is_near_minimum(self) -> None:
        text = self._text(220_000, 216_535)
        self.assertIn("рядом с историческим минимумом", text)
        self.assertIn("+1,6%", text)

    def test_4_percent_is_good(self) -> None:
        text = self._text(220_000, 211_538)
        self.assertIn("Цена хорошая относительно истории", text)
        self.assertIn("+4,0%", text)

    def test_7_percent_is_normal(self) -> None:
        text = self._text(220_000, 205_607)
        self.assertIn("Сейчас +7,0% от исторического минимума", text)

    def test_immature_keeps_the_maturity_caveat(self) -> None:
        text = self._text(262_990, 258_900, mature=False)
        self.assertIn("меньше 14 дней", text)
        self.assertIn("исторически лучшее время", text)
        self.assertIn("не вывод", text)


class PriceMathTests(unittest.TestCase):
    def test_thailand_cheaper_rub_and_percent(self) -> None:
        gap = country_price_gap(262_990, 215_862)
        self.assertEqual(gap.difference_rub, 262_990 - 215_862)
        self.assertAlmostEqual(gap.difference_pct or 0, (262_990 - 215_862) / 262_990 * 100, places=2)
        self.assertTrue(gap.thailand_cheaper)
        self.assertEqual(gap.band, "substantial")

    def test_russia_cheaper(self) -> None:
        gap = country_price_gap(200_000, 230_000)
        self.assertLess(gap.difference_rub or 0, 0)
        self.assertFalse(gap.thailand_cheaper)
        self.assertEqual(gap.band, "substantial")

    def test_under_five_percent_is_comparable(self) -> None:
        self.assertEqual(country_price_gap(100_000, 97_000).band, "comparable")

    def test_ten_percent_is_substantial(self) -> None:
        self.assertEqual(country_price_gap(100_000, 90_000).band, "substantial")
        self.assertEqual(country_price_gap(100_000, 93_000).band, "slight")

    def test_invalid_prices_are_safe(self) -> None:
        self.assertIsNone(country_price_gap(0, 100).difference_rub)
        self.assertIsNone(country_price_gap(100, None).difference_pct)
        self.assertIsNone(country_price_gap(None, None).band)


class UxTests(unittest.TestCase):
    def test_main_menu_has_recommendation_button(self) -> None:
        text = json.dumps(_keyboard(), ensure_ascii=False)
        self.assertIn("Что покупать сейчас", text)
        self.assertIn("ctrl:recommend", text)

    def test_selector_is_best_plus_five_compact_callbacks(self) -> None:
        deals = [_deal(cluster_name=f"Model {i}", offer={"external_id": str(i), "sku": f"SKU-{i}", "mpn": f"SKU-{i}"}) for i in range(8)]
        kb = recommendation_keyboard(deals)
        labels = [row[0]["text"] for row in kb["inline_keyboard"]]
        self.assertEqual(labels[0], "🏆 Лучший вариант")
        self.assertLessEqual(len(kb["inline_keyboard"]) - 2, 5)
        for row in kb["inline_keyboard"][:-1]:
            self.assertLessEqual(len(row[0]["callback_data"]), 64)
            self.assertTrue(row[0]["callback_data"].startswith("rq:"))

    def test_pick_enqueues_without_scan(self) -> None:
        deal = _deal()
        with patch("control_bot.unique_russian_top", return_value=[deal]), patch(
            "buy_thailand_flow.enqueue_recommendation_job", return_value={"ok": True, "thailand_job_id": "abc"}
        ) as enq, patch("thailand.scanner.run_thailand_scan") as scan, patch("control_bot.send_message"):
            handle_recommendation_pick(object(), "token", 1, "best")
        enq.assert_called_once()
        scan.assert_not_called()
        self.assertEqual(enq.call_args.args[0].cluster_name, deal.cluster_name)

    def test_non_admin_blocked(self) -> None:
        update = {"callback_query": {"id": "1", "data": "rq:best", "message": {"chat": {"id": 999}, "message_id": 1}}}
        with patch("control_bot._is_admin", return_value=False), patch(
            "control_bot.answer_callback"
        ), patch("buy_thailand_flow.enqueue_recommendation_job") as enq:
            process_update(object(), "token", update)
        enq.assert_not_called()

    def test_stale_russian_data_blocked(self) -> None:
        sent = []

        class _Client:
            def post(self, *args, **kwargs):
                sent.append(kwargs.get("json") or {})

                class _R:
                    def json(self):
                        return {"ok": True, "result": {}}

                return _R()

        with patch("control_bot.unique_russian_top", return_value=[]):
            handle_recommendation_menu(_Client(), "token", 1)
        blob = json.dumps(sent, ensure_ascii=False)
        self.assertIn("Нет свежих данных", blob)


class BuyMessageTests(unittest.TestCase):
    def test_buy_signal_once_and_worker_one_recommendation(self) -> None:
        deal = _deal()
        sent = []

        class _Sender:
            def send_message(self, text):
                sent.append(text)
                return self

            @property
            def ok(self):
                return True

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp, patch(
            "buy_thailand_flow.enqueue_job", return_value={"ok": True, "job_id": "job1"}
        ):
            out = evaluate_buy_opportunity_flow(
                russian_deals=[deal],
                sender=_Sender(),
                deliver=True,
                state_path=Path(tmp) / "state.json",
            )
        self.assertEqual(out["messages_sent"], 1)
        self.assertEqual(len(sent), 1)

        def scan_fn(**kwargs):
            return {
                "status": "ok",
                "stores": [{"store": "jib", "ok": True, "error_code": None, "collection_mode": "http"}],
                "thailand_top": [],
                "_top": [],
                "target_model": kwargs.get("target_model"),
                "target_search": _search("EXACT", [_row(187_000)]),
            }

        job = {
            "job_id": "job1",
            "trigger_type": "buy_signal",
            "signals": [],
            "russian_context": [{
                "cluster_name": deal.cluster_name,
                "store": deal.store,
                "price": deal.price,
                "gpu": deal.gpu,
                "cpu": deal.cpu,
                "ram_gb": deal.ram_gb,
                "ssd_gb": deal.ssd_gb,
                "screen_inch": deal.screen_inch,
                "score": deal.score,
                "confidence": deal.confidence,
                "historical_min": deal.historical_min,
                "history_started_at": deal.history_started_at,
                "external_id": "1",
                "sku": "G614PR-RV027",
                "mpn": "90NR0NJ7-M001J0",
            }],
            "target_model": {"canonical_model_code": "G614PR-RV027", "model_codes": ["G614PR-RV027"]},
        }
        result = execute_thailand_job(job, deliver=False, scan_fn=scan_fn, state_path=Path(tempfile.mkdtemp()) / "s.json")
        self.assertEqual(len(result["messages"]), 1)
        self.assertIn("ЧТО ПОКУПАТЬ", result["messages"][0])
        self.assertNotIn("ЛУЧШИЕ ЦЕНЫ", result["messages"][0])
        self.assertNotIn("ЭТА МОДЕЛЬ В ТАИЛАНДЕ", result["messages"][0])

    def test_manual_compare_trigger_constant_round_trip(self) -> None:
        deal = _deal()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "buy_thailand_flow.enqueue_job", return_value={"ok": True, "job_id": "r1"}
        ) as enq:
            out = enqueue_recommendation_job(deal, chat_id=1, jobs_dir=Path(tmp))
        self.assertTrue(out["ok"])
        job = enq.call_args.args[0]
        self.assertEqual(job["trigger_type"], "manual_recommendation")
        self.assertEqual(job["russian_context"][0]["history_started_at"], MATURE)
