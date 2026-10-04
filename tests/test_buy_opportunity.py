from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from buy_opportunity import (
    BuySignal,
    atomic_write_json,
    cooldown_allows,
    evaluate_buy_rules,
    fingerprint_for_deal,
    load_state,
    model_key_for_deal,
    record_signal,
    save_state,
    select_buy_signals,
)
from deal_ranking import RankedDeal


def _deal(**kwargs) -> RankedDeal:
    defaults = dict(
        score=70.0,
        reasons=["x"],
        offer={"store": "kns", "external_id": "1", "name": "MSI Vector", "price": 229990},
        cluster_name="MSI Vector 17",
        gpu="RTX 5070 Ti",
        store="kns",
        price=229990,
        url="https://example.test/1",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=17.0,
        cpu="Intel Core Ultra 9 275HX",
        confidence=100,
        historical_min=224990,
    )
    defaults.update(kwargs)
    return RankedDeal(**defaults)


class BuyRulesTests(unittest.TestCase):
    def test_rule_a_target_near_hist(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=229990, historical_min=224990, score=70, confidence=100)
        )
        self.assertIsNotNone(sig)
        self.assertIn("A", sig.rule_ids)

    def test_rule_b_strong_score(self) -> None:
        # Price above target (230k) but strong score and within 3% of hist
        sig = evaluate_buy_rules(
            _deal(
                price=230000,
                historical_min=225000,
                score=76,
                confidence=100,
                gpu="RTX 5070 Ti",
            )
        )
        # 230000 > 230000? equal is <= target — use slightly over target
        sig = evaluate_buy_rules(
            _deal(
                price=231000,
                historical_min=225000,
                score=76,
                confidence=100,
            )
        )
        self.assertIsNotNone(sig)
        self.assertIn("B", sig.rule_ids)

    def test_rule_c_historical_low(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=226000, historical_min=225000, score=66, confidence=100)
        )
        self.assertIsNotNone(sig)
        self.assertIn("C", sig.rule_ids)

    def test_weak_deal_no_signal(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=280000, historical_min=224990, score=40, confidence=100)
        )
        self.assertIsNone(sig)

    def test_low_confidence_no_signal(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=229990, historical_min=224990, score=80, confidence=50)
        )
        self.assertIsNone(sig)

    def test_no_history_no_automatic_signal(self) -> None:
        sig = evaluate_buy_rules(_deal(historical_min=None, price=229990, score=90))
        self.assertIsNone(sig)

    def test_buy_classification(self) -> None:
        # Passes rules but not strong (price above strong hist band or lower score)
        sig = evaluate_buy_rules(
            _deal(
                price=229990,
                historical_min=215000,  # ~7% over min — rule A if <=5%? 229990/215000-1=0.069 > 0.05
                score=76,
                confidence=100,
            )
        )
        # Adjust: within 5% for A, but over 3% so not STRONG
        sig = evaluate_buy_rules(
            _deal(
                price=229990,
                historical_min=220000,  # +4.54% — A yes, strong needs <=3%
                score=76,
                confidence=100,
            )
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.level, "BUY")

    def test_strong_buy_classification(self) -> None:
        sig = evaluate_buy_rules(
            _deal(
                price=229990,
                historical_min=224990,  # +2.2%
                score=79,
                confidence=100,
            )
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.level, "STRONG_BUY")
        self.assertTrue(sig.reasons)


class CooldownTests(unittest.TestCase):
    def test_24h_dedupe(self) -> None:
        deal = _deal()
        sig = evaluate_buy_rules(deal)
        assert sig is not None
        state = {"models": {}, "last_top1_key": model_key_for_deal(deal)}
        now = datetime.now(timezone.utc)
        record_signal(state, sig, thailand_scanned=True, top1_key=model_key_for_deal(deal), now=now)
        ok, reason = cooldown_allows(
            state=state,
            signal=sig,
            top1_key=model_key_for_deal(deal),
            now=now + timedelta(hours=1),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "deduped")

    def test_price_improve_bypass(self) -> None:
        deal = _deal(price=200000)
        sig = evaluate_buy_rules(
            _deal(price=200000, historical_min=198000, score=80, confidence=100)
        )
        assert sig is not None
        state = {
            "models": {
                sig.model_key: {
                    "last_thailand_scan_at": datetime.now(timezone.utc).isoformat(),
                    "last_ru_price": 220000,
                    "last_signal_level": "BUY",
                }
            },
            "last_top1_key": sig.model_key,
        }
        ok, reason = cooldown_allows(
            state=state, signal=sig, top1_key=sig.model_key, manual=False
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "price_improved_pct")

    def test_absolute_drop_bypass(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=224000, historical_min=222000, score=80, confidence=100)
        )
        assert sig is not None
        state = {
            "models": {
                sig.model_key: {
                    "last_thailand_scan_at": datetime.now(timezone.utc).isoformat(),
                    "last_ru_price": 230000,  # drop 6000
                    "last_signal_level": "BUY",
                }
            }
        }
        ok, reason = cooldown_allows(state=state, signal=sig, top1_key=sig.model_key)
        self.assertTrue(ok)
        self.assertEqual(reason, "price_drop_abs")

    def test_upgrade_to_strong_bypass(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=229990, historical_min=224990, score=79, confidence=100)
        )
        assert sig is not None
        self.assertEqual(sig.level, "STRONG_BUY")
        state = {
            "models": {
                sig.model_key: {
                    "last_thailand_scan_at": datetime.now(timezone.utc).isoformat(),
                    "last_ru_price": 229990,
                    "last_signal_level": "BUY",
                }
            },
            "last_top1_key": sig.model_key,
        }
        ok, reason = cooldown_allows(state=state, signal=sig, top1_key=sig.model_key)
        self.assertTrue(ok)
        self.assertEqual(reason, "upgraded_to_strong")

    def test_new_top1_bypass(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=229990, historical_min=224990, score=79, confidence=100)
        )
        assert sig is not None
        state = {
            "models": {
                sig.model_key: {
                    "last_thailand_scan_at": datetime.now(timezone.utc).isoformat(),
                    "last_ru_price": 229990,
                    "last_signal_level": "STRONG_BUY",
                }
            },
            "last_top1_key": "other:99:Other",
        }
        ok, reason = cooldown_allows(
            state=state, signal=sig, top1_key=sig.model_key
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "new_top1_model")

    def test_manual_bypass(self) -> None:
        sig = evaluate_buy_rules(
            _deal(price=229990, historical_min=224990, score=79, confidence=100)
        )
        assert sig is not None
        state = {
            "models": {
                sig.model_key: {
                    "last_thailand_scan_at": datetime.now(timezone.utc).isoformat(),
                    "last_ru_price": 229990,
                    "last_signal_level": "STRONG_BUY",
                }
            },
            "last_top1_key": sig.model_key,
        }
        ok, reason = cooldown_allows(
            state=state, signal=sig, top1_key=sig.model_key, manual=True
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "manual")

    def test_state_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buy_opportunity_state.json"
            atomic_write_json(path, {"version": 1, "models": {"a": {"x": 1}}})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["models"]["a"]["x"], 1)

    def test_broken_state_failsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buy_opportunity_state.json"
            path.write_text("{not json", encoding="utf-8")
            state = load_state(path)
            self.assertEqual(state["models"], {})


if __name__ == "__main__":
    unittest.main()
