from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from buy_opportunity import (
    cooldown_allows,
    evaluate_buy_rules,
    explain_buy_decisions,
    load_state,
    model_key_for_deal,
    record_signal,
    record_signal_enqueued,
    save_state,
    select_buy_signals,
)
from buy_thailand_flow import enqueue_manual_thailand_job, evaluate_buy_opportunity_flow
from deal_ranking import RankedDeal, cluster_history_started_at, history_starts_from_rows
from thailand.job_queue import pending_count

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
MATURE = (T0 - timedelta(days=30)).isoformat()


def _deal(**kwargs) -> RankedDeal:
    store = kwargs.pop("store", "kns")
    ext = kwargs.pop("external_id", "1")
    defaults = dict(
        score=70.0,
        reasons=["x"],
        offer={"store": store, "external_id": ext, "name": "MSI Vector"},
        cluster_name="MSI Vector 17",
        cluster_key="msi-vector-17",
        gpu="RTX 5070 Ti",
        store=store,
        price=229990,
        url="https://example.test/1",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=17.0,
        cpu="Intel Core Ultra 9 275HX",
        confidence=100,
        historical_min=224990,
        history_started_at=MATURE,
    )
    defaults.update(kwargs)
    return RankedDeal(**defaults)


def _notified_state(deal: RankedDeal, *, at: datetime = T0) -> dict:
    sig = evaluate_buy_rules(deal)
    assert sig is not None
    state: dict = {"version": 1, "models": {}}
    record_signal(state, sig, thailand_scanned=False, top1_key=model_key_for_deal(deal), now=at)
    return state


def _decide(deal: RankedDeal, state: dict, *, at: datetime, manual: bool = False):
    decisions, _top1 = explain_buy_decisions([deal], state=state, manual=manual, now=at)
    assert len(decisions) == 1, decisions
    _sig, ok, reason = decisions[0]
    return ok, reason


class RepeatSemanticsTests(unittest.TestCase):
    def test_01_first_eligible_buy_notifies(self) -> None:
        actionable, all_signals, _ = select_buy_signals([_deal()], state={}, now=T0)
        self.assertEqual(len(all_signals), 1)
        self.assertEqual(len(actionable), 1)
        self.assertEqual(_decide(_deal(), {}, at=T0), (True, "first_signal"))

    def test_02_same_price_after_24h_no_notify(self) -> None:
        deal = _deal()
        state = _notified_state(deal)
        ok, reason = _decide(deal, state, at=T0 + timedelta(hours=24, minutes=1))
        self.assertFalse(ok)
        self.assertEqual(reason, "deduped")

    def test_03_same_price_after_48h_no_notify(self) -> None:
        deal = _deal()
        state = _notified_state(deal)
        for hours in (25, 48, 24 * 7):
            ok, reason = _decide(deal, state, at=T0 + timedelta(hours=hours))
            self.assertFalse(ok, hours)
            self.assertEqual(reason, "deduped")

    def test_04_price_improves_3pct_notifies(self) -> None:
        state = _notified_state(_deal())
        better = _deal(price=222990, historical_min=220000)
        self.assertEqual(_decide(better, state, at=T0 + timedelta(hours=1)), (True, "price_improved_pct"))

    def test_05_absolute_drop_5000_notifies(self) -> None:
        state = _notified_state(_deal())
        cheaper = _deal(price=224990, historical_min=222000)
        self.assertEqual(_decide(cheaper, state, at=T0 + timedelta(hours=1)), (True, "price_drop_abs"))

    def test_05b_small_drop_does_not_notify(self) -> None:
        state = _notified_state(_deal())
        ok, reason = _decide(_deal(price=226990), state, at=T0 + timedelta(hours=30))
        self.assertFalse(ok)
        self.assertEqual(reason, "deduped")

    def test_06_buy_to_strong_buy_notifies(self) -> None:
        state = _notified_state(_deal(score=70))
        strong = _deal(score=79)
        self.assertEqual(evaluate_buy_rules(strong).level, "STRONG_BUY")
        self.assertEqual(_decide(strong, state, at=T0 + timedelta(hours=1)), (True, "upgraded_to_strong"))

    def test_06b_strong_buy_to_buy_does_not_notify(self) -> None:
        state = _notified_state(_deal(score=79))
        ok, _reason = _decide(_deal(score=70), state, at=T0 + timedelta(hours=30))
        self.assertFalse(ok)

    def test_07_true_new_historical_low_notifies(self) -> None:
        state = _notified_state(_deal(price=229990, historical_min=229990))
        new_low = _deal(price=226990, historical_min=226990)
        self.assertEqual(_decide(new_low, state, at=T0 + timedelta(hours=3)), (True, "new_historical_low"))

    def test_08_unchanged_historical_low_no_repeat(self) -> None:
        state = _notified_state(_deal(price=229990, historical_min=229990))
        same = _deal(price=229990, historical_min=229990)
        self.assertEqual(_decide(same, state, at=T0 + timedelta(hours=30)), (False, "deduped"))
        tiny = _deal(price=228990, historical_min=228990)
        self.assertEqual(_decide(tiny, state, at=T0 + timedelta(hours=30)), (False, "deduped"))

    def test_08b_legacy_state_without_notified_low_no_repeat(self) -> None:
        deal = _deal(price=229990, historical_min=229990)
        state = {
            "models": {
                model_key_for_deal(deal): {
                    "last_signal_at": T0.isoformat(),
                    "last_ru_price": 229990,
                    "last_signal_level": "BUY",
                }
            }
        }
        self.assertEqual(_decide(deal, state, at=T0 + timedelta(hours=30)), (False, "deduped"))

    def test_09_new_top1_model_preserved(self) -> None:
        deal = _deal()
        sig = evaluate_buy_rules(deal)
        state: dict = {"version": 1, "models": {}}
        record_signal(state, sig, thailand_scanned=False, top1_key="other:99:Other Laptop", now=T0)
        self.assertEqual(state["last_top1_cluster"], "Other Laptop")
        ok, reason = cooldown_allows(
            state=state, signal=sig, top1_key=sig.model_key, now=T0 + timedelta(hours=2)
        )
        self.assertEqual((ok, reason), (True, "new_top1_model"))

    def test_09b_same_top1_does_not_repeat(self) -> None:
        deal = _deal()
        state = _notified_state(deal)
        self.assertEqual(state["last_top1_cluster"], deal.cluster_name)
        self.assertEqual(_decide(deal, state, at=T0 + timedelta(hours=30)), (False, "deduped"))

    def test_09c_top1_flapping_within_cooldown_not_repeated(self) -> None:
        deal = _deal()
        sig = evaluate_buy_rules(deal)
        state = _notified_state(deal)
        state["last_top1_key"] = "other:99:Other Laptop"
        state["last_top1_cluster"] = "Other Laptop"
        ok, _ = cooldown_allows(
            state=state, signal=sig, top1_key=sig.model_key, now=T0 + timedelta(hours=2)
        )
        self.assertFalse(ok)

    def test_10_history_13_9_days_no_automatic_buy(self) -> None:
        deal = _deal(history_started_at=(T0 - timedelta(days=13.9)).isoformat())
        actionable, all_signals, _ = select_buy_signals([deal], state={}, now=T0)
        self.assertEqual(len(all_signals), 1)
        self.assertEqual(actionable, [])
        self.assertEqual(_decide(deal, {}, at=T0), (False, "history_immature"))

    def test_10b_unknown_history_no_automatic_buy(self) -> None:
        deal = _deal(history_started_at=None)
        self.assertEqual(_decide(deal, {}, at=T0), (False, "history_immature"))

    def test_11_history_14_days_eligible(self) -> None:
        self.assertEqual(config.BUY_MIN_HISTORY_DAYS, 14.0)
        deal = _deal(history_started_at=(T0 - timedelta(days=14)).isoformat())
        actionable, _all, _ = select_buy_signals([deal], state={}, now=T0)
        self.assertEqual(len(actionable), 1)

    def test_12_manual_thailand_works_without_mature_history(self) -> None:
        young = _deal(history_started_at=(T0 - timedelta(days=2)).isoformat())
        actionable, _all, _ = select_buy_signals([young], state={}, manual=True, now=T0)
        self.assertEqual(len(actionable), 1)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("buy_thailand_flow.run_thailand_scan", create=True):
                out = enqueue_manual_thailand_job(
                    compare=True,
                    chat_id="42",
                    russian_deals=[young],
                    jobs_dir=Path(tmp) / "jobs",
                )
            self.assertTrue(out["ok"])
            self.assertEqual(out["thailand_scan_status"], "queued")
            self.assertEqual(pending_count(Path(tmp) / "jobs"), 1)

    def test_13_restart_state_reload_does_not_resend(self) -> None:
        deal = _deal()
        sig = evaluate_buy_rules(deal)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buy_opportunity_state.json"
            state: dict = {"version": 1, "models": {}}
            record_signal_enqueued(
                state, sig, job_id="job-1", top1_key=sig.model_key, now=T0
            )
            save_state(state, path)
            reloaded = load_state(path)
        self.assertEqual(reloaded["models"][sig.model_key]["last_notified_price"], sig.price)
        actionable, _all, _ = select_buy_signals(
            [deal], state=reloaded, now=T0 + timedelta(hours=26)
        )
        self.assertEqual(actionable, [])

    def test_cheapest_store_flip_same_cluster_not_repeated(self) -> None:
        state = _notified_state(_deal(store="kns", external_id="1"))
        flipped = _deal(store="regard", external_id="77", price=229490)
        self.assertNotIn(model_key_for_deal(flipped), state["models"])
        self.assertEqual(_decide(flipped, state, at=T0 + timedelta(hours=30)), (False, "deduped"))


class ThailandEffectTests(unittest.TestCase):
    def test_unchanged_buy_after_24h_does_not_enqueue_thailand_job(self) -> None:
        deal = _deal()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            state_path = root / "s.json"
            with patch("buy_opportunity._now", return_value=T0):
                first = evaluate_buy_opportunity_flow(
                    russian_deals=[deal], deliver=False, state_path=state_path, jobs_dir=jobs
                )
            self.assertEqual(first["buy_actionable"], 1)
            self.assertEqual(first["thailand_scan_status"], "queued")
            self.assertEqual(pending_count(jobs), 1)

            for hours in (24.5, 48):
                with patch("buy_opportunity._now", return_value=T0 + timedelta(hours=hours)):
                    again = evaluate_buy_opportunity_flow(
                        russian_deals=[deal], deliver=False, state_path=state_path, jobs_dir=jobs
                    )
                self.assertEqual(again["buy_signals"], 1)
                self.assertEqual(again["buy_actionable"], 0)
                self.assertFalse(again["thailand_scan_triggered"])
                self.assertEqual(again["messages"], [])
                self.assertEqual(pending_count(jobs), 1)

    def test_immature_history_does_not_enqueue(self) -> None:
        young = _deal(history_started_at=(T0 - timedelta(days=8)).isoformat())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("buy_opportunity._now", return_value=T0):
                out = evaluate_buy_opportunity_flow(
                    russian_deals=[young], deliver=False, state_path=root / "s.json", jobs_dir=root / "jobs"
                )
            self.assertEqual(out["buy_signals"], 1)
            self.assertEqual(out["buy_actionable"], 0)
            self.assertEqual(pending_count(root / "jobs"), 0)


class HistorySpanTests(unittest.TestCase):
    def test_history_start_is_earliest_row_per_product(self) -> None:
        rows = {
            1: [
                {"price": 1, "checked_at": "2026-10-03T10:00:00"},
                {"price": 2, "checked_at": "2026-09-27T08:00:00"},
            ],
            2: [{"price": 3, "checked_at": "2026-10-01T00:00:00+00:00"}],
            3: [],
        }
        starts = history_starts_from_rows(rows)
        self.assertEqual(set(starts), {1, 2})
        self.assertTrue(starts[1].startswith("2026-09-27T08:00:00"))

    def test_cluster_start_uses_earliest_offer(self) -> None:
        class _Offer:
            def __init__(self, pid: int) -> None:
                self.product_id = pid

        class _Match:
            offers = [_Offer(2), _Offer(1)]

        starts = {1: "2026-09-27T08:00:00+00:00", 2: "2026-10-01T00:00:00+00:00"}
        self.assertEqual(cluster_history_started_at(_Match(), starts), starts[1])
        self.assertIsNone(cluster_history_started_at(_Match(), {}))


if __name__ == "__main__":
    unittest.main()
