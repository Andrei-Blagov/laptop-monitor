"""Telegram cards and button wiring on the existing market-event queue."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import config
from buy_opportunity import BuySignal
from control_bot import BTN_PERSONAL_REFRESH, BTN_TH_COMPARE, handle_thailand_manual, process_update
from tests.test_thai_catalog_reuse import NOW, _fx, _offer, _publish
from thailand.catalog_refresh import ManualClock
from thailand.market_events import build_market_event, enqueue_market_event
from thailand.market_messages import MESSAGE_LIMIT, format_market_messages
from thailand.market_telegram import (
    apply_configured_inputs,
    enqueue_buy_candidate,
    enqueue_view_refresh,
    sender_from_config,
)
from thailand.worker import process_one

INSTANCE = "c22-test"


class TextSender:
    def __init__(self, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.sent: list[dict] = []

    def send_text(self, event, text, *, part_id, current, historical, info_current, reply_markup, recipient) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("partial")
        self.sent.append({
            "part_id": part_id,
            "text": text,
            "current": current,
            "historical": historical,
            "info_current": info_current,
            "reply_markup": reply_markup,
            "recipient": recipient,
            "event_id": event["event_id"],
        })


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json or {}})

        class Response:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}

        return Response()


class MarketTelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs"
        self.catalog = self.root / "catalog"
        self.health = self.root / "health.json"
        self.fx_path = self.root / "fx.json"
        self.ru = self.root / "ru"
        self.jobs.mkdir()
        self.health.write_text('{"version": 1, "stores": {}}\n', encoding="utf-8")
        self.sender = TextSender()
        self.flag = patch.object(config, "stage22_market_telegram_enabled", return_value=True)
        self.flag.start()
        self.paths = patch.multiple(
            config,
            THAILAND_JOBS_DIR=str(self.jobs),
            THAI_CATALOG_ROOT=str(self.catalog),
            RUSSIAN_MARKET_ROOT=str(self.ru),
            FX_OBSERVATION_PATH=str(self.fx_path),
        )
        self.paths.start()
        self.instance = patch.object(config, "get_instance_id", return_value=INSTANCE)
        self.instance.start()

    def tearDown(self) -> None:
        self.instance.stop()
        self.paths.stop()
        self.flag.stop()
        self.tmp.cleanup()

    def _catalog(self, hours: int = 2) -> None:
        observed = NOW - timedelta(hours=hours)
        _publish(self.catalog, [_offer("th-1", "SKU-AAA017", 80_000, observed)], observed=observed)

    def _run(self, **kwargs):
        return process_one(
            jobs_dir=self.jobs,
            catalog_root=kwargs.get("catalog_root", self.catalog),
            health_path=self.health,
            fx=kwargs.get("fx", _fx()),
            transport=kwargs.get("transport"),
            sender=kwargs.get("sender", self.sender),
            clock=ManualClock(kwargs.get("when", NOW)),
            deliver=True,
            state_path=self.root / "state.json",
            russian_snapshot=kwargs.get("russian_snapshot"),
            russian_snapshot_root=kwargs.get("russian_snapshot_root"),
        )

    def test_flag_off_keeps_the_current_enqueue(self) -> None:
        self.flag.stop()
        self.flag = patch.object(config, "stage22_market_telegram_enabled", return_value=False)
        self.flag.start()
        client = FakeClient()
        with patch("buy_thailand_flow.enqueue_manual_thailand_job", return_value={"ok": True}) as manual:
            handle_thailand_manual(client, "token", 42, compare=False)
        manual.assert_called_once()
        self.assertEqual(list(self.jobs.glob("pending/*.json")), [])
        self.assertIn("запущена", client.calls[-1]["json"]["text"])

    def test_refresh_button_enqueues_and_worker_formats_without_http(self) -> None:
        self._catalog(2)
        client = FakeClient()
        with patch.object(config, "get_telegram_admin_chat_ids", return_value={"42"}):
            process_update(client, "token", {
                "callback_query": {
                    "id": "cb-1",
                    "data": BTN_PERSONAL_REFRESH,
                    "message": {"message_id": 7, "chat": {"id": 42}},
                },
            })
        self.assertIn("answerCallbackQuery", client.calls[0]["url"])
        self.assertIn("очеред", client.calls[1]["json"]["text"])
        self.assertTrue(list((self.jobs / "pending").glob("*.json")) or list((self.jobs / "processing").glob("*.json")) or list((self.jobs / "archive").glob("*.json")) or list((self.jobs / "delivery").glob("*.json")))
        outcome = self._run()
        self.assertEqual(outcome["http_calls"], 0)
        text = "\n".join(item["text"] for item in self.sender.sent)
        self.assertIn("https://example.test/th-1", text)
        self.assertIn("80000 ฿", text)
        self.assertIn("самостоятельный TOP", text)
        self.assertNotIn("устаревш", text.lower())
        self.assertIn("ctrl:ptop_go", json.dumps(self.sender.sent[-1]["reply_markup"], ensure_ascii=False))
        self.assertLessEqual(max(len(item["text"]) for item in self.sender.sent), 4096)

    def test_ready_result_is_shown_without_a_new_event(self) -> None:
        self._catalog(2)
        enqueue_view_refresh(chat_id=42, view="general_top", now=NOW)
        self._run(sender=TextSender())
        pending_before = list(self.jobs.rglob("*.json"))
        client = FakeClient()
        handle_thailand_manual(client, "token", 42, compare=True)
        self.assertEqual(list(self.jobs.rglob("*.json")), pending_before)
        shown = "\n".join(call["json"].get("text", "") for call in client.calls)
        self.assertIn("https://example.test/th-1", shown)
        self.assertIn("ctrl:ptop_go", json.dumps(client.calls[-1]["json"].get("reply_markup"), ensure_ascii=False))

    def test_manual_daily_and_buy_share_one_formatted_pass(self) -> None:
        self._catalog(2)
        for trigger, event_id in (("daily", "daily-1"), ("manual", "manual-1"), ("buy", "buy-1")):
            enqueue_market_event(build_market_event(
                instance_id=INSTANCE, trigger_type=trigger, requested_view="general_top",
                recipients=[trigger], event_id=event_id, now=NOW,
                source_ru_snapshot_id="ru-buy" if trigger == "buy" else None,
            ), root=self.jobs, now=NOW)
        outcome = self._run()
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual(len({item["event_id"] for item in self.sender.sent}), 3)
        self.assertEqual(len({item["text"] for item in self.sender.sent}), len(self.sender.sent) // 3 or 1)

    def test_regional_and_unmatched_cards_keep_their_meaning(self) -> None:
        fresh = NOW.isoformat()
        pair = {
            "pair_id": "regional",
            "comparison_status": "confirmed",
            "match_class": "EQUIVALENT",
            "saving_rub": "30000",
            "saving_pct": "13",
            "unknown_costs": ["перелёт", "таможня"],
            "confirmed_expenses": [{"label": "доставка", "amount_rub": "1500"}],
            "commercial_differences": ["регион"],
            "ru_membership": {"in_general_top": True, "in_personal_top3": True},
            "th_observation": {
                "name": "Legion Pro 7", "url": "https://example.test/th-regional", "source": "jib",
                "price": "80000", "availability_state": "in_stock",
                "price_checked_at": fresh, "availability_checked_at": fresh,
                "specs": {"gpu": {"value": "RTX 5070 Ti"}, "ram_gb": {"value": 32}, "ssd_gb": {"value": 1024}},
            },
            "ru_observation": {
                "name": "Legion Pro 7 RU", "url": "https://example.test/ru-regional", "source": "regard",
                "price": "250000", "price_checked_at": fresh, "availability_checked_at": fresh,
            },
        }
        alternative = dict(pair, pair_id="other", match_class="ALTERNATIVE", comparison_status="not_comparable", saving_rub=None)
        result = {
            "result_id": "cards", "scan_id": "cards", "fx": _fx(),
            "rankings": {"thai_top_value": [{
                "model_id": "m1", "name": "Thai only", "url": "https://example.test/thai-only",
                "price_thb": "70000", "price_rub": "175000", "in_personal_top3": True,
                "offers": [{"source": "advice", "price": "70000", "availability_state": "in_stock",
                            "price_checked_at": fresh, "availability_checked_at": fresh, "url": "https://example.test/thai-only"}],
            }]},
            "comparison": {
                "pairs": [pair, alternative],
                "unmatched_th": [{
                    "name": "Только Таиланд", "url": "https://example.test/unmatched",
                    "price_thb": "90000", "price_rub": "225000", "gpu": "RTX 5080",
                    "reason": "not_observed", "unmatched_value_candidate": True,
                }],
                "unmatched_value_top": [],
                "market_coverage_complete": False,
                "limitations": ["inspected_coverage_incomplete"],
            },
            "views": {"general_top": ["regional"], "all": ["regional"], "personal_top3": ["regional"]},
        }
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        (self.jobs / "results" / "cards.json").write_text(json.dumps(result), encoding="utf-8")
        event = build_market_event(
            instance_id=INSTANCE, trigger_type="manual", requested_view="general_top",
            recipients=["42"], event_id="cards-event", now=NOW,
        )
        event["subscription_scan_id"] = "cards"
        event["result_id"] = "cards"
        (self.jobs / "delivery" / "cards-event.json").write_text(json.dumps(event), encoding="utf-8")
        self._run(catalog_root=self.catalog)
        text = "\n".join(item["text"] for item in self.sender.sent)
        self.assertIn("https://example.test/th-regional", text)
        self.assertIn("https://example.test/ru-regional", text)
        self.assertIn("Региональные артикулы", text)
        self.assertIn("Другая конфигурация. Это не «та же модель дешевле».", text)
        self.assertIn("Неизвестные расходы не равны нулю: перелёт, таможня", text)
        self.assertIn("Покрытие неполное", text)
        self.assertIn("Личный TOP-3", text)
        self.assertIn("Выгодная по порогу, не сигнал BUY", text)
        self.assertIn("не значит, что данные устарели", text)
        self.assertIn("Подтверждённая разница: 30000 ₽", text)
        self.assertIn("jib", text)
        self.assertIn("в наличии", text)
        self.assertIn("RTX 5070 Ti", text)

    def test_stale_fx_does_not_call_fresh_observations_outdated(self) -> None:
        fresh = NOW.isoformat()
        old = (NOW - timedelta(days=5)).isoformat()
        pair = {
            "pair_id": "stale-fx", "comparison_status": "confirmed", "match_class": "EXACT",
            "saving_rub": "1000", "saving_pct": "1", "unknown_costs": [], "confirmed_expenses": [],
            "ru_membership": {"in_general_top": True, "in_personal_top3": False},
            "th_observation": {
                "name": "Fresh TH", "url": "https://example.test/fresh", "source": "jib", "price": "80000",
                "availability_state": "in_stock", "price_checked_at": fresh, "availability_checked_at": fresh,
            },
            "ru_observation": {"name": "Fresh RU", "source": "regard", "price": "200000",
                               "price_checked_at": fresh, "availability_checked_at": fresh},
        }
        result = {
            "result_id": "fx", "scan_id": "fx", "fx": {**_fx(), "fetched_at": old},
            "comparison": {"pairs": [pair], "market_coverage_complete": True, "limitations": []},
            "views": {"general_top": ["stale-fx"], "all": ["stale-fx"], "personal_top3": []},
            "rankings": {"thai_top_value": []},
        }
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        (self.jobs / "results" / "fx.json").write_text(json.dumps(result), encoding="utf-8")
        event = build_market_event(
            instance_id=INSTANCE, trigger_type="manual", requested_view="general_top",
            recipients=["42"], event_id="fx-event", now=NOW,
        )
        event.update(subscription_scan_id="fx", result_id="fx")
        (self.jobs / "delivery" / "fx-event.json").write_text(json.dumps(event), encoding="utf-8")
        self._run()
        text = self.sender.sent[0]["text"]
        self.assertFalse(self.sender.sent[0]["current"])
        self.assertTrue(self.sender.sent[0]["info_current"])
        self.assertIn("Текущая выгода не подтверждена", text)
        self.assertIn(fresh, text)
        self.assertNotIn("Наблюдение не текущее", text)

    def test_partial_delivery_retries_only_the_missing_part(self) -> None:
        long_name = "Модель " + ("X" * 3600)
        messages = format_market_messages(
            {"rankings": {"thai_top_value": [{"name": long_name, "offers": [{"url": "https://example.test/long", "price": "1"}]}]},
             "comparison": {}, "fx": _fx()},
            {"requested_view": "thai_top", "recipients": ["a"]},
            purchase_current=False, info_current=True,
        )
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(item["text"]) <= 4096 for item in messages))
        result = {
            "result_id": "parts", "scan_id": "parts", "fx": _fx(),
            "rankings": {"thai_top_value": [{"name": long_name, "model_id": "long", "offers": [
                {"url": "https://example.test/long", "price": "1", "price_checked_at": NOW.isoformat(),
                 "availability_checked_at": NOW.isoformat(), "availability_state": "in_stock"},
            ]}]},
            "views": {"general_top": ["long"], "all": [], "personal_top3": []},
        }
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        (self.jobs / "results" / "parts.json").write_text(json.dumps(result), encoding="utf-8")
        event = build_market_event(
            instance_id=INSTANCE, trigger_type="manual", requested_view="thai_top",
            recipients=["a", "b"], event_id="parts-event", now=NOW,
        )
        event.update(subscription_scan_id="parts", result_id="parts")
        (self.jobs / "delivery" / "parts-event.json").write_text(json.dumps(event), encoding="utf-8")
        flaky = TextSender(fail_after=1)
        failed = self._run(sender=flaky)
        self.assertEqual(failed["status"], "delivery_failed")
        first = flaky.sent[0]["part_id"]
        retry_sender = TextSender()
        retried = self._run(sender=retry_sender, when=NOW + timedelta(minutes=31))
        self.assertNotEqual(retried["status"], "delivery_failed")
        resent = [item["part_id"] for item in retry_sender.sent]
        self.assertNotIn(first, resent)
        self.assertTrue(resent)

    def test_source_limit_does_not_format_a_new_purchase(self) -> None:
        self._catalog(9)
        until = (NOW + timedelta(hours=2)).isoformat()
        self.health.write_text(json.dumps({"version": 1, "stores": {"jib": {
            "disabled_until": until, "consecutive_failures": 3,
        }}}), encoding="utf-8")
        enqueue_view_refresh(chat_id=42, view="general_top", now=NOW)
        outcome = self._run()
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual(outcome["status"], "deferred")
        self.assertEqual(self.sender.sent, [])

    def test_buy_repeat_does_not_start_another_collection(self) -> None:
        self._catalog(2)
        signal = BuySignal(
            level="BUY", model_key="m", cluster_name="Model", price=100, store="regard",
            gpu="RTX 5070 Ti", cpu=None, ram_gb=32, ssd_gb=1024, screen_inch=16,
            score=1, confidence=1, historical_min=None, target_price=None, url=None,
            reasons=[], rule_ids=[], over_hist_pct=None, fingerprint="buy-fp",
            cluster_key=None, history_started_at=None,
        )
        first = enqueue_buy_candidate(signal, instance_id=INSTANCE, jobs_dir=self.jobs, ru_snapshot_id="ru-1", now=NOW)
        self._run()
        calls = len(self.sender.sent)
        second = enqueue_buy_candidate(signal, instance_id=INSTANCE, jobs_dir=self.jobs, ru_snapshot_id="ru-1", now=NOW)
        self.assertTrue(second["duplicate"])
        self.assertIsNone(self._run(sender=TextSender()))
        self.assertEqual(len(self.sender.sent), calls)
        self.assertNotEqual(first["event_id"], "")

    def test_configured_inputs_are_read_without_network(self) -> None:
        self._catalog(2)
        fx = _fx()
        self.fx_path.write_text(json.dumps(fx), encoding="utf-8")
        snapshot_id = json.loads((self.catalog / "latest.json").read_text(encoding="utf-8"))["snapshot_id"]
        (self.ru / "snapshots").mkdir(parents=True)
        document = {"schema_version": 1, "snapshot_id": "aabbccdd", "instance_id": INSTANCE, "offers": []}
        raw = json.dumps(document).encode("utf-8")
        (self.ru / "snapshots" / "aabbccdd.json").write_bytes(raw)
        (self.ru / "latest.json").write_text(json.dumps({
            "snapshot_id": "aabbccdd", "sha256": hashlib.sha256(raw).hexdigest(),
            "path": "snapshots/aabbccdd.json", "instance_id": INSTANCE,
        }), encoding="utf-8")
        catalog_root, _health, loaded_fx, russian, ru_root = apply_configured_inputs(
            catalog_root=None, health_path=None, fx=None, russian_snapshot=None, russian_snapshot_root=None,
        )
        self.assertEqual(catalog_root, str(self.catalog))
        self.assertEqual(loaded_fx["rub_per_thb"], fx["rub_per_thb"])
        self.assertEqual(russian["snapshot_id"], "aabbccdd")
        self.assertEqual(str(ru_root), str(self.ru))
        self.assertTrue(snapshot_id)
        with patch.object(config, "get_telegram_credentials", return_value=("token", "42")):
            self.assertIsNotNone(sender_from_config())

    def test_non_admin_callback_does_not_enqueue(self) -> None:
        client = FakeClient()
        with patch.object(config, "get_telegram_admin_chat_ids", return_value={"1"}):
            process_update(client, "token", {
                "callback_query": {"id": "cb", "data": BTN_TH_COMPARE, "message": {"chat": {"id": 99}}},
            })
        self.assertIn("Недостаточно прав", client.calls[0]["json"].get("text", ""))
        self.assertEqual(list(self.jobs.glob("pending/*.json")), [])


if __name__ == "__main__":
    unittest.main()
