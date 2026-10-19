"""Acceptance for the Russian Telegram workflow, entirely offline."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
from control_bot import (build_top_parts, handle_personal_top, handle_recommendation_pick,
                         handle_run, notify_manual_result, process_update, unique_russian_top)
from deal_ranking import RankedDeal
from manual_run import read_request, request_run, run_request, set_message_id
from purchase_recommendation import format_russian_recommendation


def deal(number: int, **changes) -> RankedDeal:
    data = dict(score=80.0, reasons=["мощный HX-процессор"],
                offer={"sku": f"CODE-{number:03d}", "external_id": str(number)},
                cluster_name=f'Model &quot;{number}&quot;', gpu="RTX 5080", cpu="Intel Core i9 14900HX",
                store="regard", price=250_000, url=f"https://example.test/{number}",
                ram_gb=32, ssd_gb=1024, screen_inch=16.0, screen_resolution="2560x1600",
                confidence=90, historical_min=245_000,
                history_started_at="2020-01-01T00:00:00+00:00")
    data.update(changes)
    return RankedDeal(**data)


class RussianTopTests(unittest.TestCase):
    def test_counts_links_and_model_selection(self):
        for count in (0, 1, 6, 10):
            with self.subTest(count=count), patch("control_bot.unique_russian_top", return_value=[deal(n) for n in range(count)]), patch(
                "control_bot.get_latest_store_runs_readonly", return_value={"regard": {"finished_at": "2026-10-19T09:00:00+00:00"}}
            ):
                text = "\n".join(build_top_parts("unused.db"))
                self.assertIn("TOP-10 России", text)
                if count:
                    self.assertIn(f"{count} моделей", text)
                for n in range(count):
                    self.assertIn(f"https://example.test/{n}", text)
                self.assertNotIn("&quot;", text)
                self.assertIn("МСК", text) if count else None

    def test_selection_uses_confirmed_policy_and_unique_models(self):
        rows = [deal(1), deal(1), deal(2, price=290_001), deal(3, ram_gb=16),
                deal(4, cpu=None), deal(5, url=None), deal(6, screen_resolution="1920x1080")]
        with patch("russian_deals.load_russian_ranked_deals", return_value=rows):
            found = unique_russian_top("unused.db", limit=10)
        self.assertEqual([item.url for item in found], ["https://example.test/1"])

    def test_buy_explanation_is_russian_and_has_exact_url(self):
        item = deal(1)
        text = format_russian_recommendation(item)
        self.assertIn(item.url, text)
        self.assertIn("истории", text.lower())
        self.assertNotIn("Таиланд", text)
        self.assertNotIn("&quot;", text)
        with patch("control_bot.unique_russian_top", return_value=[item]), patch(
            "buy_thailand_flow.enqueue_recommendation_job"
        ) as enqueue, patch("control_bot.send_message") as send:
            handle_recommendation_pick(Mock(), "token", 1, "best", db_path="unused.db")
        enqueue.assert_not_called()
        self.assertIn(item.url, send.call_args.args[3])
        from purchase_recommendation import Recommendation, INSUFFICIENT_DATA, format_purchase_recommendation
        self.assertIn(item.url, format_purchase_recommendation(item, Recommendation(verdict=INSUFFICIENT_DATA)))

    def test_favorites_ignore_old_thai_state(self):
        sku = config.PERSONAL_TOP3[0]["exact_skus"][0]
        item = deal(1, offer={"sku": sku, "external_id": "1"})
        with patch("control_bot.unique_russian_top", return_value=[item]), patch(
            "personal_top.load_state", side_effect=AssertionError("old Thai state read")
        ), patch("control_bot._reply_or_edit") as reply:
            handle_personal_top(Mock(), "token", 1)
        text = reply.call_args.args[3]
        self.assertIn(item.url, text)
        self.assertIn("Старые данные не считаются текущими", text)
        self.assertNotIn("Таиланд", text)


class ManualRunTests(unittest.TestCase):
    def test_duplicate_completion_error_and_restart_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch("manual_run._pipeline_busy", return_value=False):
            path = Path(tmp) / "manual.json"
            first, accepted = request_run(42, path=path, spawn=lambda *_: 999999)
            self.assertTrue(accepted)
            duplicate, accepted = request_run(42, path=path, spawn=lambda *_: self.fail("duplicate spawn"))
            self.assertFalse(accepted)
            self.assertEqual(duplicate["id"], first["id"])
            run_request(first["id"], path=path,
                        runner=lambda *_args, **_kwargs: Mock(status="success", exit_code=0,
                                                               run_id=17, error_message=None, duration_seconds=1))
            self.assertEqual(read_request(path)["run_id"], 17)
            second, accepted = request_run(42, path=path, spawn=lambda *_: 999999)
            self.assertTrue(accepted)
            run_request(second["id"], path=path,
                        runner=lambda *_args, **_kwargs: Mock(status="failed", exit_code=1,
                                                               run_id=18, error_message="collection", duration_seconds=1))
            self.assertEqual(read_request(path)["status"], "failed")
            third, _ = request_run(42, path=path, spawn=lambda *_: 999999)
            from manual_run import _save
            third["status"] = "running"
            _save(path, third)
            with patch("manual_run.read_pipeline_runs_readonly", return_value=[]):
                state = __import__("manual_run").reconcile(path=path)
            self.assertEqual(state["status"], "failed")
            self.assertIn("прервана", state["error"])

    def test_bot_accepts_without_running_pipeline_and_edits_once(self):
        state = {"id": "a" * 32, "status": "pending", "chat_id": "42"}
        with patch("control_bot.request_run", return_value=(state, True)), patch(
            "control_bot._post_telegram", return_value={"result": {"message_id": 51}}
        ), patch("control_bot.set_message_id") as saved, patch("control_bot.run_pipeline") as pipeline:
            handle_run(Mock(), "token", 42)
        pipeline.assert_not_called()
        saved.assert_called_once_with(state["id"], 51)
        done = {**state, "status": "success", "message_id": 51, "notified": False}
        with patch("control_bot.reconcile", side_effect=[done, {**done, "notified": True}]), patch(
            "control_bot.edit_message", return_value=True
        ) as edit, patch("control_bot.mark_notified") as marked:
            notify_manual_result(Mock(), "token")
            notify_manual_result(Mock(), "token")
        edit.assert_called_once()
        marked.assert_called_once_with(state["id"])

    def test_status_command_dispatch_while_check_pending(self):
        update = {"message": {"chat": {"id": 42}, "text": "/status"}}
        with patch("control_bot._is_admin", return_value=True), patch(
            "control_bot.build_status_text", return_value="Ручная проверка: running"
        ), patch("control_bot.send_message") as send, patch("control_bot.run_pipeline") as pipeline:
            process_update(Mock(), "token", update)
        self.assertIn("running", send.call_args.args[3])
        pipeline.assert_not_called()


class ThailandPauseTests(unittest.TestCase):
    def test_daily_ru_event_skips_thai_collection(self):
        from thailand.market_events import _run_plan

        with patch.object(config, "THAILAND_AUTOMATIC_ENABLED", False), patch(
            "thailand.market_events.evaluate_catalog_reuse"
        ) as scan:
            plan, result, passes = _run_plan(
                [{"trigger_type": "daily", "instance_id": "test"}], catalog_root="unused",
                health_path=None, fx=None, russian_snapshot=None, transport=None,
                clock=None, now=None,
            )
        self.assertEqual(plan["action"], "RU_ONLY")
        self.assertEqual(result["http_calls"], 0)
        self.assertEqual(passes, 0)
        scan.assert_not_called()

    def test_personal_timer_does_not_enqueue(self):
        from personal_top import main

        with patch.object(config, "THAILAND_AUTOMATIC_ENABLED", False), patch(
            "thailand.market_schedule.enqueue_personal_market"
        ) as enqueue, patch("personal_top.enqueue_personal_job") as old:
            self.assertEqual(main(["enqueue"]), 0)
        enqueue.assert_not_called()
        old.assert_not_called()

    def test_russian_buy_does_not_enqueue_thailand(self):
        from buy_thailand_flow import evaluate_buy_opportunity_flow

        with tempfile.TemporaryDirectory() as tmp, patch("buy_thailand_flow._safe_enqueue") as enqueue:
            result = evaluate_buy_opportunity_flow(
                russian_deals=[deal(1)], deliver=False, state_path=Path(tmp) / "buy.json",
                enqueue_thailand=False,
            )
        enqueue.assert_not_called()
        self.assertFalse(result["thailand_scan_triggered"])
        self.assertTrue(all("Таиланд" not in text for text in result["messages"]))
