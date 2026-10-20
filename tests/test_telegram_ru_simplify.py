"""Acceptance for the Russian Telegram workflow, entirely offline."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

import config
from control_bot import (build_top_parts, buy_picker_keyboard, handle_personal_top,
                         handle_top,
                         handle_recommendation_pick, handle_run, handle_thailand_manual,
                         model_callback_token, notify_manual_result, process_update,
                         unique_russian_top)
from deal_ranking import RankedDeal
from manual_run import read_request, request_run, run_request, set_message_id
from purchase_recommendation import format_russian_recommendation
from tests.test_thai_catalog_reuse import _russian
from thai_catalog import russian_offer_views


def deal(number: int, **changes) -> RankedDeal:
    data = dict(score=80.0, reasons=["мощный HX-процессор"],
                offer={"sku": f"CODE-{number:03d}", "external_id": str(number),
                       "price_checked_at": "2026-10-19T09:00:00+00:00"},
                cluster_name=f'Model &quot;{number}&quot;', gpu="RTX 5080", cpu="Intel Core i9 14900HX",
                store="regard", price=250_000, url=f"https://example.test/{number}",
                ram_gb=32, ssd_gb=1024, screen_inch=16.0, screen_resolution="2560x1600",
                confidence=90, historical_min=245_000,
                history_started_at="2020-01-01T00:00:00+00:00")
    data.update(changes)
    return RankedDeal(**data)


def confirmed_quotes(rows):
    from deal_ranking import canonical_gpu
    from product_identity import normalize_identifier

    now = datetime.now(timezone.utc).isoformat()
    return {(item.store, str(item.offer.get("external_id") or "")): {
        "price": item.price, "url": item.url,
        "name": item.cluster_name,
        "sku_ids": {normalize_identifier(item.offer["sku"])} if item.offer.get("sku") else set(),
        "mpn_ids": {normalize_identifier(item.offer["mpn"])} if item.offer.get("mpn") else set(),
        "nominal_ssd_gb": None,
        "price_checked_at": now, "availability_checked_at": now,
        "gpu": canonical_gpu(item.gpu), "cpu": item.cpu, "ram_gb": item.ram_gb,
        "ssd_gb": item.ssd_gb, "screen_inch": item.screen_inch,
        "screen_resolution": item.screen_resolution,
    } for item in rows}


class RussianTopTests(unittest.TestCase):
    def test_counts_links_and_model_selection(self):
        for count in (0, 1, 6, 10):
            with self.subTest(count=count), patch("control_bot.unique_russian_top", return_value=[deal(n) for n in range(count)]):
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
        with patch("russian_deals.load_russian_ranked_deals", return_value=rows), patch(
            "control_bot._confirmed_russian_quotes", return_value=confirmed_quotes(rows)
        ):
            found = unique_russian_top("unused.db", limit=10)
        self.assertEqual([item.url for item in found], ["https://example.test/1", "https://example.test/4"])

    def test_same_name_variants_keep_separate_links_and_callbacks(self):
        first = deal(1, cluster_name="Same model", offer={"sku": "VARIANT-1", "external_id": "1"})
        second = deal(2, cluster_name="Same model", offer={"sku": "VARIANT-2", "external_id": "2"},
                      cpu=None, ssd_gb=2048)
        duplicate = deal(3, cluster_name="Same model", offer={"sku": "VARIANT-1", "external_id": "3"},
                         url="https://example.test/alternate")
        with patch("russian_deals.load_russian_ranked_deals", return_value=[first, second, duplicate]), patch(
            "control_bot._confirmed_russian_quotes", return_value=confirmed_quotes([first, second, duplicate])
        ):
            found = unique_russian_top("unused.db", limit=10)
        self.assertEqual([item.url for item in found], [first.url, second.url])
        callbacks = [row[0]["callback_data"] for row in buy_picker_keyboard(found)["inline_keyboard"]]
        self.assertEqual(len(set(callbacks)), 2)
        self.assertNotEqual(model_callback_token(first), model_callback_token(second))
        with patch("control_bot.unique_russian_top", return_value=found), patch(
            "control_bot.send_message"
        ) as send:
            handle_recommendation_pick(Mock(), "token", 1, model_callback_token(second), db_path="unused.db")
        self.assertIn(second.url, send.call_args.args[3])
        self.assertNotIn(first.url, send.call_args.args[3])
        self.assertIn("CPU не подтверждён", send.call_args.args[3])
        with patch("control_bot.unique_russian_top", return_value=found):
            text = "\n".join(build_top_parts("unused.db"))
        self.assertIn("CPU не подтверждён", text)
        self.assertNotIn("None", text)

    def test_cards_and_buttons_use_one_read_and_stale_variant_is_rejected(self):
        first, changed = deal(1), deal(2)
        with patch("control_bot.unique_russian_top", side_effect=[[first], [changed]]) as top, patch(
            "control_bot.send_message"
        ) as send:
            handle_top(Mock(), "token", 42, db_path="unused.db")
        top.assert_called_once()
        self.assertIn(first.url, send.call_args.args[3])
        button = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(button, "buy:" + model_callback_token(first))
        with patch("control_bot.unique_russian_top", return_value=[changed]), patch(
            "control_bot.send_message"
        ) as answer:
            handle_recommendation_pick(Mock(), "token", 42, button[len("buy:"):], db_path="unused.db")
        self.assertIn("Откройте список ещё раз", answer.call_args.args[3])
        self.assertNotIn(changed.url, answer.call_args.args[3])

    def test_daily_and_interactive_require_same_own_quote_and_spec_dates(self):
        item = deal(1, cluster_name="MSI Raider 16 HX AI A2XWHG-814XRU",
                    offer={"sku": "A2XWHG-814XRU", "external_id": "ru-01"},
                    gpu="RTX 5070 Ti", cpu="Intel Core Ultra 9 275HX")
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(minutes=20)).isoformat()
        stale = (now - timedelta(days=8)).isoformat()
        for case in ("fresh", "old_product", "legacy_specs"):
            with self.subTest(case=case):
                raw = copy.deepcopy(_russian())
                raw["created_at"] = fresh
                raw["source_coverage"][0]["latest_attempt_at"] = fresh
                row = raw["offers"][0]
                raw["offers"] = [row]
                row["source_url"] = item.url
                row["product_name"] = item.cluster_name
                row["sku_candidates"] = [item.offer["sku"]]
                row["price_checked_at"] = stale if case == "old_product" else fresh
                row["availability_checked_at"] = stale if case == "old_product" else fresh
                row["availability_evidence"]["captured_at"] = row["availability_checked_at"]
                for spec in row["specs"].values():
                    spec["evidence"]["captured_at"] = None if case == "legacy_specs" else fresh
                    spec["evidence"]["capture_state"] = "unknown" if case == "legacy_specs" else "known"
                daily, _ = russian_offer_views(raw, now=now)
                with patch("market_snapshot.load_russian_market_snapshot", return_value=raw), patch(
                    "russian_deals.load_russian_ranked_deals", return_value=[item]
                ), patch("control_bot._reply_or_edit") as favorites, patch(
                    "control_bot.send_message"
                ) as buy:
                    selected = unique_russian_top("unused.db", limit=10)
                    top_text = "\n".join(build_top_parts("unused.db"))
                    handle_personal_top(Mock(), "token", 42)
                    handle_recommendation_pick(Mock(), "token", 42, "best", db_path="unused.db")
                self.assertEqual(bool(daily), bool(selected))
                if case == "fresh":
                    self.assertIn(item.url, top_text)
                    self.assertIn(item.url, favorites.call_args.args[3])
                    self.assertIn(item.url, buy.call_args.args[3])
                    self.assertIn("Проверено: ", top_text)
                    self.assertIn((now - timedelta(minutes=20)).astimezone(
                        __import__("zoneinfo").ZoneInfo("Europe/Moscow")
                    ).strftime("%d.%m %H:%M МСК"), top_text)
                else:
                    self.assertNotIn(item.url, top_text)
                    self.assertNotIn(item.url, favorites.call_args.args[3])
                    self.assertNotIn(item.url, buy.call_args.args[3])
                    self.assertIn("Старые данные не считаются текущими", favorites.call_args.args[3])

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

    def test_lenovo_series_needs_confirmed_profile_and_shows_variant(self):
        compatible = deal(1, cluster_name="Lenovo Legion Pro 7 16AFR10H",
                          offer={"name": "Lenovo Legion Pro 7 16AFR10H", "sku": "16AFR10H-82ZZ0001RU",
                                 "external_id": "1"}, gpu="RTX 5070 Ti")
        incompatible = deal(2, cluster_name="Lenovo Legion Pro 7 16AFR10H",
                            offer={"name": "Lenovo Legion Pro 7 16AFR10H", "sku": "16AFR10H-82ZZ0002RU",
                                   "external_id": "2"}, gpu="RTX 5080")
        with patch("control_bot.unique_russian_top", return_value=[incompatible, compatible]), patch(
            "control_bot._reply_or_edit"
        ) as reply:
            handle_personal_top(Mock(), "token", 1)
        text = reply.call_args.args[3]
        self.assertIn("Вариант серии: 16AFR10H-82ZZ0001RU", text)
        self.assertIn(compatible.url, text)
        self.assertNotIn(incompatible.url, text)
        with patch("control_bot.unique_russian_top", return_value=[incompatible]), patch(
            "control_bot._reply_or_edit"
        ) as reply:
            handle_personal_top(Mock(), "token", 1)
        self.assertNotIn("Вариант серии:", reply.call_args.args[3])
        self.assertNotIn(incompatible.url, reply.call_args.args[3])


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
            state = __import__("manual_run").reconcile(path=path)
            self.assertEqual(state["status"], "unconfirmed")
            self.assertIn("не подтверждён", state["error"])

    def test_interleaved_scheduled_run_cannot_confirm_killed_manual_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp, patch("manual_run._pipeline_busy", return_value=False):
            path = Path(tmp) / "manual.json"
            manual, accepted = request_run(42, path=path, spawn=lambda *_: 999999)
            self.assertTrue(accepted)
            from manual_run import _save, reconcile
            manual.update(status="running", pid=999999, started_at=manual["created_at"])
            _save(path, manual)
            # A newer scheduled pipeline exists; its SUCCESS is unrelated to this request.
            scheduled = {"id": 123, "started_at": manual["created_at"], "status": "success"}
            with patch("storage.read_pipeline_runs_readonly", return_value=[scheduled]) as runs:
                state = reconcile(path=path)
                restarted_state = reconcile(path=path)
            runs.assert_not_called()
            self.assertEqual(state["status"], "unconfirmed")
            self.assertEqual(restarted_state, state)
            self.assertNotIn("run_id", state)


class ManualThailandTests(unittest.TestCase):
    def test_cached_result_does_not_intercept_explicit_refresh(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            config, "THAILAND_JOBS_DIR", tmp
        ), patch.object(config, "stage22_market_telegram_enabled", return_value=True), patch(
            "thailand.market_telegram.latest_result", return_value={"generated_at": "2020-01-01T00:00:00Z"}
        ) as cached, patch("thailand.market_telegram.stream_ready_view") as stream, patch(
            "control_bot.send_message"
        ) as send, patch("thailand.catalog_refresh.execute_catalog_plan") as collect:
            handle_thailand_manual(Mock(), "token", 42, compare=False)
            files = list((Path(tmp) / "pending").glob("*.json"))
            self.assertEqual(len(files), 1)
            event = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(event["trigger_type"], "manual")
        self.assertEqual(event["requested_view"], "thai_top")
        cached.assert_not_called()
        stream.assert_not_called()
        collect.assert_not_called()
        self.assertIn("поставлена в очередь", send.call_args.args[3])

        from thailand.market_events import _run_plan
        with patch("thailand.market_events.evaluate_catalog_reuse", return_value={"action": "DEFERRED"}) as planner, patch(
            "thailand.market_events.execute_catalog_plan", return_value={"status": "deferred", "http_calls": 0}
        ) as execute:
            decision, outcome, _ = _run_plan([event], catalog_root="unused", health_path=None,
                                             fx=None, russian_snapshot=None, transport=None, clock=None, now=None)
        self.assertEqual(decision["action"], "DEFERRED")
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual(planner.call_args.kwargs["trigger"], "manual")
        execute.assert_called_once()

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
        marked.assert_called_once_with(state["id"], delivery="edited")

    def test_permanent_edit_failure_uses_one_fallback_and_unblocks_next_run(self):
        from manual_run import _save, claim_fallback, mark_notified, record_delivery_attempt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.json"
            old = {"id": "a" * 32, "chat_id": "42", "status": "unconfirmed",
                   "error": "process stopped", "message_id": 51, "notified": False}
            _save(path, old)
            calls = []

            def handler(request):
                calls.append(request.url.path)
                if request.url.path.endswith("editMessageText"):
                    return httpx.Response(400, json={"ok": False, "description": "Bad Request: message to edit not found"})
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 52}})

            with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch(
                "control_bot.reconcile", side_effect=lambda: read_request(path)
            ), patch("control_bot.record_delivery_attempt", side_effect=lambda ident, kind: record_delivery_attempt(ident, kind, path=path)), patch(
                "control_bot.claim_fallback", side_effect=lambda ident: claim_fallback(ident, path=path)
            ), patch("control_bot.mark_notified", side_effect=lambda ident, delivery: mark_notified(ident, delivery=delivery, path=path)):
                notify_manual_result(client, "token")
                notify_manual_result(client, "token")
            receipt = read_request(path)
            self.assertEqual(receipt["delivery_status"], "fallback_sent")
            self.assertEqual(receipt["delivery_attempts"], 1)
            self.assertTrue(receipt["notified"])
            self.assertEqual(sum(name.endswith("sendMessage") for name in calls), 1)
            with patch("manual_run._pipeline_busy", return_value=False):
                next_run, accepted = request_run(42, path=path, spawn=lambda *_: 999999)
            self.assertTrue(accepted)
            self.assertNotEqual(next_run["id"], old["id"])

    def test_temporary_edit_failure_is_bounded_without_duplicate_fallback(self):
        from manual_run import _save, mark_notified, record_delivery_attempt

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.json"
            _save(path, {"id": "b" * 32, "chat_id": "42", "status": "success",
                         "message_id": 51, "notified": False})
            calls = []

            def handler(request):
                calls.append(request.url.path)
                raise httpx.ConnectError("offline")

            with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch(
                "control_bot.reconcile", side_effect=lambda: read_request(path)
            ), patch("control_bot.record_delivery_attempt", side_effect=lambda ident, kind: record_delivery_attempt(ident, kind, path=path)), patch(
                "control_bot.mark_notified", side_effect=lambda ident, delivery: mark_notified(ident, delivery=delivery, path=path)
            ):
                for _ in range(5):
                    notify_manual_result(client, "token")
            receipt = read_request(path)
            self.assertEqual(receipt["delivery_status"], "edit_unconfirmed")
            self.assertEqual(receipt["delivery_attempts"], 3)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(name.endswith("editMessageText") for name in calls))

    def test_restart_after_successful_edit_accepts_message_not_modified(self):
        from manual_run import _save, mark_notified

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.json"
            _save(path, {"id": "c" * 32, "chat_id": "42", "status": "success",
                         "message_id": 51, "notified": False})
            replies = iter([
                httpx.Response(200, json={"ok": True, "result": True}),
                httpx.Response(400, json={"ok": False, "description": "Bad Request: message is not modified"}),
            ])
            with httpx.Client(transport=httpx.MockTransport(lambda _request: next(replies))) as client, patch(
                "control_bot.reconcile", side_effect=lambda: read_request(path)
            ):
                with patch("control_bot.mark_notified"):
                    notify_manual_result(client, "token")
                self.assertFalse(read_request(path)["notified"])
                with patch("control_bot.mark_notified", side_effect=lambda ident, delivery: mark_notified(ident, delivery=delivery, path=path)):
                    notify_manual_result(client, "token")
            receipt = read_request(path)
            self.assertTrue(receipt["notified"])
            self.assertEqual(receipt["delivery_status"], "already_visible")

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
