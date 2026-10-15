"""Handoff regressions: real identifiers, process locks and crash boundaries."""
from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_market_telegram import TextSender
from tests.test_thai_catalog_reuse import NOW, _fx
from thailand.market_events import VolumeLease, build_market_event, _retry_delivery
from thailand.market_messages import format_market_messages
from thailand.market_telegram import MarketTextSender


def _probe(path, output):
    lease = VolumeLease(Path(path))
    acquired = lease.try_acquire()
    output.put(acquired)
    if acquired:
        lease.release()


def _row(ident="one", title="ASUS G615LR-S5348"):
    return {"model_id": ident, "variant_id": ident, "name": title, "best_offer_id": ident,
            "offers": [{"offer_id": ident, "name": title, "source": "jib", "price": "80000",
                        "price_checked_at": NOW.isoformat(), "availability_checked_at": NOW.isoformat()}]}


class HandoffRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_fork_does_not_inherit_lease_ownership(self):
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("requires Linux fork")
        lease = VolumeLease(self.root / "scan.lease")
        self.assertTrue(lease.try_acquire())
        self.addCleanup(lease.release)
        ctx = multiprocessing.get_context("fork")
        output = ctx.Queue()
        child = ctx.Process(target=_probe, args=(str(lease.path), output))
        child.start()
        child.join(5)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 0)
        self.assertFalse(output.get(timeout=2))
        output.close()

    def test_lease_releases_in_either_nesting_order(self):
        first = VolumeLease(self.root / "scan.lease")
        second = VolumeLease(first.path)
        self.assertTrue(first.try_acquire())
        self.assertTrue(second.try_acquire())
        first.release()
        second.release()
        ctx = multiprocessing.get_context("spawn")
        output = ctx.Queue()
        child = ctx.Process(target=_probe, args=(str(first.path), output))
        child.start()
        child.join(5)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 0)
        self.assertTrue(output.get(timeout=2))
        output.close()

    def test_real_sku_one_th_offer_is_one_card(self):
        row = _row()
        pair = {"pair_id": "p", "th_variant_id": "one", "th_offer_id": "one",
                "th_observation": row["offers"][0], "ru_observation": {},
                "comparison_status": "confirmed", "match_class": "EXACT",
                "ru_membership": {"in_general_top": True}}
        result = {"fx": _fx(), "rankings": {"thai_top_value": [row]},
                  "comparison": {"pairs": [pair], "best_pairs_by_th_variant_id": {"one": "p"}}}
        parts = format_market_messages(result, {"requested_view": "general_top"}, now=NOW)
        self.assertEqual(len(parts), 1)

    def test_same_printed_sku_different_variants_are_not_hidden(self):
        result = {"fx": _fx(), "rankings": {"thai_top_value": [
            _row("one", "ASUS SKU-SAME 32GB"), _row("two", "ASUS SKU-SAME 64GB")]}}
        parts = format_market_messages(result, {"requested_view": "thai_top"}, now=NOW)
        self.assertEqual(len(parts), 2)
        self.assertIn("64GB", parts[1]["text"])

    def test_card_and_coverage_stay_below_telegram_limit(self):
        from thailand.market_messages import MESSAGE_LIMIT
        result = {"fx": _fx(), "rankings": {"thai_top_value": [_row(title="X" * 3480)]},
                  "coverage": [{"source": "jib" + str(i) + "x" * 300, "state": "partial"} for i in range(30)]}
        parts = format_market_messages(result, {"requested_view": "thai_top"}, now=NOW)
        self.assertTrue(all(len(p["text"]) <= MESSAGE_LIMIT for p in parts))

    def test_stream_empty_result_keeps_incomplete_coverage(self):
        from thailand.market_messages import iter_market_sends
        result = {"status": "partial", "coverage": [{"source": "jib", "state": "429"}]}
        event = {"requested_view": "general_top", "recipients": ["42"]}
        parts = list(iter_market_sends(result, event, lambda: NOW))
        self.assertIn("jib: 429", "\n".join(p["text"] for p in parts))
        self.assertIn("не доказательство", parts[0]["text"])

    def test_failure_message_is_split_without_losing_reasons(self):
        from thailand.market_messages import iter_market_sends, MESSAGE_LIMIT
        result = {"status": "failed", "errors": ["x" * 10000, "last_reason"]}
        parts = list(iter_market_sends(result, {"recipients": ["42"]}, lambda: NOW))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p["text"]) <= MESSAGE_LIMIT for p in parts))
        self.assertIn("last_reason", "".join(p["text"] for p in parts))

    def test_crash_after_primary_archive_does_not_repeat_group_delivery(self):
        for folder in ("results", "delivery", "delivering"):
            (self.root / folder).mkdir()
        result = {"fx": _fx(), "rankings": {"thai_top_value": [_row()]}, "comparison": {}}
        (self.root / "results" / "shared.json").write_text(json.dumps(result))
        for ident in ("a", "b"):
            event = build_market_event(instance_id="test", trigger_type="manual",
                requested_view="thai_top", recipients=["42"], event_id=ident, now=NOW)
            event.update(subscription_scan_id="shared", result_id="shared")
            (self.root / "delivery" / f"{ident}.json").write_text(json.dumps(event))
        sender = TextSender()
        wrapped = MarketTextSender(sender, jobs_dir=self.root, now_fn=lambda: NOW)
        from thailand.market_events import _write_event

        def crash(base, folder, event):
            if folder == "archive" and event["event_id"] == "b":
                raise SystemExit("process killed after primary archive")
            return _write_event(base, folder, event)

        with patch("thailand.market_events._write_event", side_effect=crash):
            with self.assertRaises(SystemExit):
                _retry_delivery(self.root, wrapped, fallback=NOW)
        self.assertEqual(len(sender.sent), 1)
        restarted = TextSender()
        _retry_delivery(self.root, MarketTextSender(restarted, jobs_dir=self.root, now_fn=lambda: NOW), fallback=NOW)
        self.assertEqual(restarted.sent, [])
        self.assertTrue((self.root / "archive" / "b.json").is_file())


if __name__ == "__main__":
    unittest.main()
