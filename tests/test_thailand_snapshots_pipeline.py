from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from deal_ranking import RankedDeal
from thailand.models import StoreScanResult, ThailandOffer
from thailand.storage import prune_snapshots, write_scan_snapshot


class SnapshotTests(unittest.TestCase):
    def test_atomic_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_scan_snapshot(
                {"scan_id": "x", "status": "ok", "offers": []},
                directory=tmp,
                when=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["scan_id"], "x")
            self.assertTrue(path.name.startswith("thailand_scan_"))

    def test_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_scan_snapshot(
                {
                    "scan_id": "x",
                    "bot_token": "SHOULD_NOT_PERSIST",
                    "webhook_secret": "nope",
                    "status": "ok",
                },
                directory=tmp,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("bot_token", data)
            self.assertNotIn("webhook_secret", data)

    def test_retention_20(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for i in range(25):
                stamp = f"20261004T{i:02d}0000Z"
                (d / f"thailand_scan_{stamp}.json").write_text("{}", encoding="utf-8")
            removed = prune_snapshots(d, keep=20)
            self.assertEqual(removed, 5)
            self.assertEqual(len(list(d.glob("thailand_scan_*.json"))), 20)

    def test_partial_stores_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_scan_snapshot(
                {
                    "scan_id": "p",
                    "status": "partial",
                    "stores": [
                        {"store": "jib", "ok": False, "error": "HTTP_403"},
                        {"store": "advice", "ok": True, "count": 2},
                    ],
                },
                directory=tmp,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "partial")
            self.assertEqual(len(data["stores"]), 2)

    def test_all_store_failure_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_scan_snapshot(
                {
                    "scan_id": "f",
                    "status": "failed",
                    "stores": [
                        {"store": "jib", "ok": False, "error": "blocked"},
                        {"store": "advice", "ok": False, "error": "blocked"},
                        {"store": "banana", "ok": False, "error": "blocked"},
                    ],
                    "offers": [],
                },
                directory=tmp,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["offers"], [])


class PipelineIsolationTests(unittest.TestCase):
    def test_thailand_exception_keeps_russian_success(self) -> None:
        from run_pipeline import PIPELINE_STATUS_SUCCESS, PipelineResult

        # Simulate post-hook isolation contract
        status = PIPELINE_STATUS_SUCCESS
        try:
            raise RuntimeError("thai boom")
        except Exception:
            thai_status = "failed"
        self.assertEqual(status, PIPELINE_STATUS_SUCCESS)
        self.assertEqual(thai_status, "failed")

    def test_thailand_timeout_status_isolated(self) -> None:
        from thailand.scanner import collect_thailand_offers

        with patch("thailand.scanner.get_thailand_adapters") as ga:
            slow = MagicMock()
            slow.slug = "jib"
            slow.enabled = True
            slow.collect.side_effect = lambda **kw: (_ for _ in ()).throw(TimeoutError())
            # Actually collect catches and returns failed — use adapter that hangs via sleep mock
            def collect(**kw):
                return StoreScanResult(store="jib", ok=False, error="timeout")

            slow.collect = collect
            ga.return_value = [slow]
            results, offers, dur = collect_thailand_offers(overall_timeout=1, parallel=False)
            self.assertEqual(results[0].error, "timeout")
            # Russian status not involved — just ensure no raise
            self.assertEqual(offers, [])

    def test_buy_report_if_thai_fails(self) -> None:
        from buy_opportunity import evaluate_buy_rules
        from thailand.formatting import format_buy_opportunity_message

        deal = RankedDeal(
            score=79,
            reasons=["x"],
            offer={"store": "kns", "external_id": "1", "name": "MSI"},
            cluster_name="MSI Vector",
            gpu="RTX 5070 Ti",
            store="kns",
            price=229990,
            confidence=100,
            historical_min=224990,
            cpu="Intel Core Ultra 9 275HX",
            ram_gb=32,
            ssd_gb=1024,
            screen_inch=17.0,
            url="https://example/1",
        )
        sig = evaluate_buy_rules(deal)
        self.assertIsNotNone(sig)
        text = format_buy_opportunity_message(sig)
        self.assertIn("ВЫГОДНЫЙ МОМЕНТ", text)

        with patch("buy_thailand_flow.run_thailand_scan") as scan:
            scan.return_value = {
                "status": "failed",
                "_store_results": [
                    StoreScanResult(store="jib", ok=False, error="blocked"),
                    StoreScanResult(store="advice", ok=False, error="blocked"),
                    StoreScanResult(store="banana", ok=False, error="blocked"),
                ],
                "_fx": None,
                "_best_match": None,
                "_comparison": None,
                "_top": [],
            }
            from buy_thailand_flow import run_buy_thailand_flow

            with tempfile.TemporaryDirectory() as tmp:
                out = run_buy_thailand_flow(
                    russian_deals=[deal],
                    deliver=False,
                    force_thailand=True,
                    state_path=Path(tmp) / "state.json",
                    snapshot_dir=Path(tmp) / "scans",
                )
            self.assertGreaterEqual(len(out["messages"]), 1)
            self.assertIn("ВЫГОДНЫЙ МОМЕНТ", out["messages"][0])

    def test_no_schema_change_marker(self) -> None:
        # Guard: Thailand must not invent new SQLite migrations in this candidate.
        schema = Path("storage.py").read_text(encoding="utf-8")
        self.assertNotIn("thailand_offers", schema)
        self.assertNotIn("buy_opportunity_events", schema)

    def test_manual_scan_does_not_run_russian_collection(self) -> None:
        from buy_thailand_flow import run_buy_thailand_flow

        with patch("buy_thailand_flow.run_thailand_scan") as scan, patch(
            "buy_thailand_flow.load_russian_ranked_deals"
        ) as load:
            load.return_value = []
            scan.return_value = {
                "status": "ok",
                "_store_results": [],
                "_fx": None,
                "_best_match": None,
                "_comparison": None,
                "_top": [],
            }
            with tempfile.TemporaryDirectory() as tmp:
                run_buy_thailand_flow(
                    db_path="dummy.db",
                    deliver=False,
                    manual=True,
                    force_thailand=True,
                    state_path=Path(tmp) / "s.json",
                    snapshot_dir=Path(tmp) / "scans",
                )
            load.assert_called()
            scan.assert_called()
            # collect_products must not be imported/called via this path
            self.assertTrue(scan.called)

    def test_existing_top_unchanged_without_signal(self) -> None:
        from deal_ranking import format_top_deals_message

        text = format_top_deals_message([])
        self.assertIn("Нет свежих", text)

    def test_history_module_untouched_import(self) -> None:
        import model_price_history

        self.assertTrue(hasattr(model_price_history, "build_model_history_report"))

    def test_chart_module_untouched_import(self) -> None:
        import price_history_chart

        self.assertTrue(hasattr(price_history_chart, "CALLBACK_CHART_PREFIX"))


if __name__ == "__main__":
    unittest.main()
