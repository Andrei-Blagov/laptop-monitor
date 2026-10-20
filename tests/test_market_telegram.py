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
            clock=kwargs.get("clock") or ManualClock(kwargs.get("when", NOW)),
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

    def test_ready_result_does_not_suppress_explicit_refresh(self) -> None:
        self._catalog(2)
        enqueue_view_refresh(chat_id=42, view="general_top", now=NOW)
        self._run(sender=TextSender())
        pending_before = list(self.jobs.rglob("*.json"))
        client = FakeClient()
        handle_thailand_manual(client, "token", 42, compare=True)
        self.assertEqual(len(list(self.jobs.rglob("*.json"))), len(pending_before) + 1)
        pending = list((self.jobs / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0].read_text())["trigger_type"], "manual")
        shown = "\n".join(call["json"].get("text", "") for call in client.calls)
        self.assertIn("поставлена в очередь", shown)
        self.assertNotIn("https://example.test/th-1", shown)

    def test_manual_daily_and_buy_share_scan_but_keep_distinct_daily_text(self) -> None:
        self._catalog(2)
        for trigger, event_id in (("daily", "daily-1"), ("manual", "manual-1"), ("buy", "buy-1")):
            enqueue_market_event(build_market_event(
                instance_id=INSTANCE, trigger_type=trigger, requested_view="general_top",
                recipients=[trigger], event_id=event_id, now=NOW,
                source_ru_snapshot_id="ru-buy" if trigger == "buy" else None,
            ), root=self.jobs, now=NOW)
        outcome = self._run()
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual({item["recipient"] for item in self.sender.sent}, {"daily", "manual", "buy"})
        archived = list((self.jobs / "archive").glob("*.json"))
        self.assertEqual({path.stem for path in archived}, {"daily-1", "manual-1", "buy-1"})
        self.assertEqual(len({item["text"] for item in self.sender.sent}), 2)

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
                "name": "Legion Pro 7", "url": "https://example.test/th-regional", "source": "jib", "external_id": "regional",
                "price": "80000", "availability_state": "in_stock",
                "price_checked_at": fresh, "availability_checked_at": fresh,
                "specs": {"gpu": {"value": "RTX 5070 Ti"}, "ram_gb": {"value": 32}, "ssd_gb": {"value": 1024}},
            },
            "ru_observation": {
                "name": "Legion Pro 7 RU", "url": "https://example.test/ru-regional", "source": "regard",
                "price": "250000", "price_checked_at": fresh, "availability_checked_at": fresh,
            },
        }
        for field in pair["th_observation"]["specs"].values():
            field["evidence"] = {"source":"jib", "external_id":"regional", "captured_at":fresh, "method":"structured_api"}
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
                    "variant_id": "solo", "best_offer_id": "solo-1",
                    "offers": [{"offer_id": "solo-1", "source": "jib", "url": "https://example.test/unmatched",
                                "price": "90000", "availability_state": "in_stock",
                                "price_checked_at": fresh, "availability_checked_at": fresh}],
                }],
                "unmatched_value_top": [],
                "market_coverage_complete": False,
                "limitations": ["inspected_coverage_incomplete"],
                "best_pairs_by_th_variant_id": {"v-regional": "regional"},
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
        self.assertIn("не означает, что цена устарела", text)
        self.assertIn("Таиланд дешевле России", text)
        self.assertIn("30000 ₽", text)
        self.assertIn("13%", text)
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
            "comparison": {"pairs": [pair], "best_pairs_by_th_variant_id": {"v": "stale-fx"},
                           "market_coverage_complete": True, "limitations": []},
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
        text = "\n".join(item["text"] for item in self.sender.sent)
        self.assertTrue(self.sender.sent)
        self.assertTrue(all(not item["current"] for item in self.sender.sent))
        self.assertIn("Текущая выгода не подтверждена", text)
        self.assertIn("14.10.2026 15:00 МСК", text)
        self.assertNotIn("Наблюдение не текущее", text)

    def test_partial_delivery_retries_only_the_missing_part(self) -> None:
        long_name = "Модель " + ("X" * 3600)
        messages = format_market_messages(
            {"rankings": {"thai_top_value": [{"model_id": "long", "name": long_name, "offers": [{"url": "https://example.test/long", "price": "1"}]}]},
             "comparison": {}, "fx": _fx()},
            {"requested_view": "thai_top", "recipients": ["a"]},
            now=NOW,
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
        first = enqueue_buy_candidate(signal, instance_id=INSTANCE, jobs_dir=self.jobs, ru_snapshot_id="ru-1", now=NOW, recipients=["42"])
        self._run()
        calls = len(self.sender.sent)
        second = enqueue_buy_candidate(signal, instance_id=INSTANCE, jobs_dir=self.jobs, ru_snapshot_id="ru-1", now=NOW, recipients=["42"])
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

    def test_compare_markets_cards_use_the_selected_pair_and_best_offer(self) -> None:
        from tests.test_thai_catalog_reuse import _russian

        observed = NOW - timedelta(hours=2)
        offers = [_offer(f"th-{index}", f"SKU-TH-{index}", 70_000 + index, observed) for index in range(1, 6)]
        offers.append(_offer("th-z-match", "SKU-MATCH17", 80_000, observed))
        _publish(self.catalog, offers, observed=observed)
        enqueue_view_refresh(chat_id=42, view="general_top", now=NOW)
        outcome = self._run(russian_snapshot=_russian(["model-ru-17"]), sender=TextSender())
        result = json.loads((self.jobs / "results" / f"{outcome['result_id']}.json").read_text(encoding="utf-8"))
        pairs = result["comparison"]["pairs"]
        confirmed = [pair for pair in pairs if pair.get("comparison_status") == "confirmed"]
        self.assertTrue(confirmed)
        chosen_id = next(iter(result["comparison"]["best_pairs_by_th_variant_id"].values()))
        rest = [pair for pair in pairs if pair.get("pair_id") != chosen_id]
        result["comparison"]["pairs"] = [*rest, next(pair for pair in pairs if pair.get("pair_id") == chosen_id)]
        self.assertGreater(len(result["comparison"]["pairs"]), 5)
        self.assertEqual(result["comparison"]["pairs"][-1]["pair_id"], chosen_id)
        parts = format_market_messages(result, {"requested_view": "general_top"}, now=NOW)
        text = "\n".join(part["text"] for part in parts)
        chosen = result["comparison"]["pairs"][-1]
        self.assertIn(chosen["th_observation"]["url"], text)
        self.assertIn(f"Цена TH: {chosen['th_observation']['price']} ฿", text)
        self.assertIn(f"Цена RU: {chosen['ru_observation']['price']} ₽", text)
        self.assertIn("2560x1600", text)
        row = result["rankings"]["thai_top_value"][0]
        best = next(offer for offer in row["offers"] if offer["offer_id"] == row["best_offer_id"])
        decoy = dict(best, source="not-the-best", url="https://example.test/decoy", offer_id="decoy")
        row["offers"] = [decoy, *row["offers"]]
        shown = "\n".join(part["text"] for part in format_market_messages(result, {"requested_view": "thai_top"}, now=NOW))
        self.assertIn(best["source"], shown)
        self.assertIn(best["url"], shown)
        self.assertNotIn("https://example.test/decoy", shown)
        empty = format_market_messages(
            {"rankings": {}, "comparison": {"pairs": [], "best_pairs_by_th_variant_id": {}}},
            {"requested_view": "general_top"}, now=NOW,
        )
        self.assertIn("показывать нечего", empty[0]["text"])

    def test_fresh_and_stale_sections_stay_distinct(self) -> None:
        fresh = NOW.isoformat()
        stale = (NOW - timedelta(hours=9)).isoformat()
        result = {
            "fx": _fx(),
            "rankings": {"thai_top_value": [{
                "model_id": "fresh-top", "name": "Fresh top", "best_offer_id": "best-1",
                "price_thb": "80000", "price_rub": "200000",
                "offers": [{"offer_id": "best-1", "source": "jib", "url": "https://example.test/fresh-top",
                            "price": "80000", "availability_state": "in_stock",
                            "price_checked_at": fresh, "availability_checked_at": fresh}],
            }]},
            "comparison": {
                "pairs": [{
                    "pair_id": "old-pair", "comparison_status": "confirmed", "match_class": "EXACT",
                    "saving_rub": "1000", "saving_pct": "1",
                    "ru_membership": {"in_general_top": True},
                    "th_observation": {"name": "Old", "price": "1", "source": "jib", "price_checked_at": stale,
                                       "availability_checked_at": stale, "url": "https://example.test/old"},
                    "ru_observation": {"price": "2", "source": "regard", "price_checked_at": stale,
                                       "availability_checked_at": stale},
                }],
                "best_pairs_by_th_variant_id": {"v": "old-pair"},
                "source_coverage": {"TH": [{"source": "advice", "state": "rate_limited"}]},
                "market_coverage_complete": False,
            },
        }
        text = "\n".join(part["text"] for part in format_market_messages(
            result, {"requested_view": "general_top"}, now=NOW,
        ))
        thai = text.split("самостоятельный TOP", 1)[1].split("Россия ↔ Таиланд", 1)[0]
        self.assertIn("остаются информационными", thai)
        self.assertNotIn("не текущая", thai)
        self.assertIn("Эта пара не текущая", text)
        self.assertIn("advice: магазин ограничил запросы", text)

    def test_configured_sender_posts_through_mock_transport(self) -> None:
        import httpx

        self._catalog(2)
        enqueue_view_refresh(chat_id="111", view="thai_top", now=NOW)
        path = next((self.jobs / "pending").glob("*.json"))
        event = json.loads(path.read_text(encoding="utf-8"))
        event["recipients"] = ["111", "222"]
        path.write_text(json.dumps(event), encoding="utf-8")
        calls: list[dict] = []
        seen_on_disk: list[list] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            calls.append(payload)
            if len(calls) == 1:
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            stored = json.loads(next((self.jobs / "delivering").glob("*.json")).read_text(encoding="utf-8"))
            seen_on_disk.append(list(stored.get("delivered_part_ids") or []))
            return httpx.Response(500, json={"ok": False, "description": "fail", "error_code": 500})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with patch.object(config, "TELEGRAM_MIN_SEND_INTERVAL_SECONDS", 0), patch.object(
            config, "get_telegram_credentials", return_value=("123:ABC", "111")
        ), patch(
            "telegram_sender.httpx.Client",
            side_effect=lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout", 30)),
        ):
            failed = process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
                sender=None, clock=ManualClock(NOW), deliver=True, state_path=self.root / "state.json",
            )
        self.assertEqual(failed["status"], "delivery_failed")
        self.assertEqual(str(calls[0]["chat_id"]), "111")
        self.assertTrue(seen_on_disk and seen_on_disk[0])
        self.assertTrue(any(
            "ctrl:ptop_go" in json.dumps(call.get("reply_markup"), ensure_ascii=False) for call in calls
        ))
        first_part = seen_on_disk[0][0]
        retry_calls: list[dict] = []

        def retry_handler(request: httpx.Request) -> httpx.Response:
            retry_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

        retry_transport = httpx.MockTransport(retry_handler)
        with patch.object(config, "TELEGRAM_MIN_SEND_INTERVAL_SECONDS", 0), patch.object(
            config, "get_telegram_credentials", return_value=("123:ABC", "111")
        ), patch(
            "telegram_sender.httpx.Client",
            side_effect=lambda *args, **kwargs: real_client(transport=retry_transport, timeout=kwargs.get("timeout", 30)),
        ):
            process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
                sender=None, clock=ManualClock(NOW + timedelta(minutes=31)), deliver=True,
                state_path=self.root / "state.json",
            )
        resent_ids = []
        stored_parts = json.loads(next((self.jobs / "archive").glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn(first_part, stored_parts.get("delivered_part_ids") or [])
        self.assertEqual(len(retry_calls), len(stored_parts["delivered_part_ids"]) - 1)
        self.assertTrue(all(call["chat_id"] in {"111", "222"} for call in calls + retry_calls))
        self.assertNotIn(first_part, resent_ids)

    def test_deliver_false_and_flag_off_do_not_open_telegram(self) -> None:
        self._catalog(2)
        enqueue_view_refresh(chat_id=42, view="thai_top", now=NOW)
        with patch("telegram_sender.httpx.Client", side_effect=AssertionError("telegram")):
            outcome = process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
                sender=None, clock=ManualClock(NOW), deliver=False, state_path=self.root / "state.json",
            )
        self.assertNotEqual(outcome["status"], "delivered")
        self.flag.stop()
        self.flag = patch.object(config, "stage22_market_telegram_enabled", return_value=False)
        self.flag.start()
        with patch("telegram_sender.httpx.Client", side_effect=AssertionError("telegram")):
            process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
                sender=None, clock=ManualClock(NOW), deliver=True, state_path=self.root / "state.json",
            )

    def test_buy_uses_the_configured_chat(self) -> None:
        signal = BuySignal(
            level="BUY", model_key="m", cluster_name="Model", price=100, store="regard",
            gpu="RTX 5070 Ti", cpu=None, ram_gb=32, ssd_gb=1024, screen_inch=16,
            score=1, confidence=1, historical_min=None, target_price=None, url=None,
            reasons=[], rule_ids=[], over_hist_pct=None, fingerprint="chat-fp",
            cluster_key=None, history_started_at=None,
        )
        with patch.object(config, "get_telegram_credentials", return_value=("token", "555")):
            stored = enqueue_buy_candidate(signal, instance_id=INSTANCE, jobs_dir=self.jobs, now=NOW)
        body = json.loads((self.jobs / "pending" / f"{stored['event_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(body["recipients"], ["555"])

    def _empty_result(self, event_id: str, recipients: list[str]) -> None:
        (self.jobs / "results").mkdir(parents=True, exist_ok=True)
        (self.jobs / "delivery").mkdir(parents=True, exist_ok=True)
        (self.jobs / "results" / f"{event_id}.json").write_text(json.dumps({
            "result_id": event_id, "scan_id": event_id, "rankings": {}, "comparison": None, "coverage": [],
        }), encoding="utf-8")
        event = build_market_event(
            instance_id=INSTANCE, trigger_type="manual", requested_view="general_top",
            recipients=recipients, event_id=event_id, now=NOW,
        )
        event.update(subscription_scan_id=event_id, result_id=event_id)
        (self.jobs / "delivery" / f"{event_id}.json").write_text(json.dumps(event), encoding="utf-8")

    def test_empty_result_reaches_each_recipient(self) -> None:
        import httpx

        self._empty_result("empty-two", ["111", "222"])
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with patch.object(config, "TELEGRAM_MIN_SEND_INTERVAL_SECONDS", 0), patch.object(
            config, "get_telegram_credentials", return_value=("123:ABC", "111")
        ), patch(
            "telegram_sender.httpx.Client",
            side_effect=lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout", 30)),
        ):
            outcome = process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
                sender=None, clock=ManualClock(NOW), deliver=True, state_path=self.root / "state.json",
            )
            self.assertEqual([str(call["chat_id"]) for call in calls], ["111", "222"])
            self.assertTrue(all("показывать нечего" in call["text"] for call in calls))
            self.assertFalse(any(str(call["chat_id"]) == "" for call in calls))
            sent = len(calls)
            process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
                sender=None, clock=ManualClock(NOW), deliver=True, state_path=self.root / "state.json",
            )
        self.assertEqual(len(calls), sent)
        self.assertEqual(outcome["status"], "delivered")

    def test_missing_recipient_does_not_ack_delivery(self) -> None:
        import httpx

        self._empty_result("empty-none", [])
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with patch.object(config, "TELEGRAM_MIN_SEND_INTERVAL_SECONDS", 0), patch.object(
            config, "get_telegram_credentials", return_value=("123:ABC", "111")
        ), patch(
            "telegram_sender.httpx.Client",
            side_effect=lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout", 30)),
        ):
            outcome = process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
                sender=None, clock=ManualClock(NOW), deliver=True, state_path=self.root / "state.json",
            )
        self.assertEqual(calls, [])
        self.assertEqual(outcome["status"], "delivery_failed")
        self.assertFalse((self.jobs / "archive" / "empty-none.json").exists())
        deferred = json.loads((self.jobs / "deferred" / "empty-none.json").read_text(encoding="utf-8"))
        self.assertNotEqual(deferred.get("delivery_state"), "sent")

    def test_manual_compare_with_cached_result_enqueues_without_immediate_stream(self) -> None:
        import httpx

        fresh = NOW.isoformat()
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "results" / "ready.json").write_text(json.dumps({
            "result_id": "ready", "scan_id": "ready", "fx": _fx(),
            "rankings": {},
            "comparison": {
                "pairs": [{
                        "pair_id": "ready-pair", "comparison_status": "confirmed", "match_class": "EXACT",
                        "saving_rub": "50000", "saving_pct": "20",
                        "ru_membership": {"in_general_top": True},
                        "th_observation": {
                            "name": "Ready", "price": "80000", "source": "jib", "url": "https://example.test/ready",
                            "availability_state": "in_stock", "price_checked_at": fresh, "availability_checked_at": fresh,
                            "specs": {"screen_resolution": {"value": "2560x1600"}},
                        },
                        "ru_observation": {
                            "price": "250000", "source": "regard", "price_checked_at": fresh, "availability_checked_at": fresh,
                        },
                        "price_gap": {"thai_base_rub": "200000", "ru_price_rub": "250000"},
                    }, {
                        "pair_id": "later-pair", "comparison_status": "confirmed", "match_class": "EXACT",
                        "saving_rub": "10000", "saving_pct": "10",
                        "ru_membership": {"in_general_top": True},
                        "th_observation": {
                            "name": "Later", "price": "70000", "source": "advice", "url": "https://example.test/later",
                            "availability_state": "in_stock", "price_checked_at": fresh, "availability_checked_at": fresh,
                        },
                        "ru_observation": {
                            "price": "180000", "source": "regard", "price_checked_at": fresh, "availability_checked_at": fresh,
                        },
                    }],
                    "best_pairs_by_th_variant_id": {"v": "ready-pair", "w": "later-pair"},
            },
        }), encoding="utf-8")
        before = sorted(path.name for path in self.jobs.rglob("*.json"))

        class Clock:
            def __init__(self) -> None:
                self.current = NOW

            def __call__(self):
                return self.current

        clock = Clock()
        calls: list[dict] = []
        shifted = {"done": False}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            calls.append({"url": str(request.url), "json": payload})
            if str(request.url).endswith("sendMessage") and not shifted["done"]:
                shifted["done"] = True
                clock.current = NOW + timedelta(hours=48)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(config, "get_telegram_admin_chat_ids", return_value={"42"}), patch(
            "thailand.market_telegram.utc_now", clock
        ):
            process_update(client, "token", {
                "callback_query": {
                    "id": "cb-view",
                    "data": BTN_TH_COMPARE,
                    "message": {"message_id": 3, "chat": {"id": 42}},
                },
            })
        messages = [call["json"] for call in calls if call["url"].endswith("sendMessage")]
        self.assertEqual(len(messages), 1)
        self.assertIn("поставлена в очередь", messages[0]["text"])
        self.assertEqual(len(list(self.jobs.rglob("*.json"))), len(before) + 1)
        self.assertTrue(all("api.telegram.org" in call["url"] for call in calls))

    def test_old_unmatched_price_is_historical_without_blaming_the_missing_pair(self) -> None:
        fresh = NOW.isoformat()
        old = (NOW - timedelta(hours=48)).isoformat()
        offer = {
            "offer_id": "best-old", "source": "jib", "url": "https://example.test/old-value",
            "price": "90000", "availability_state": "in_stock",
            "price_checked_at": old, "availability_checked_at": old,
        }
        fresh_offer = dict(offer, offer_id="best-fresh", price_checked_at=fresh, availability_checked_at=fresh,
                           url="https://example.test/fresh-value")
        result = {
            "fx": {**_fx(), "fetched_at": old},
            "comparison": {"unmatched_th": [
                {"name": "Старая выгода", "variant_id": "old", "best_offer_id": "best-old", "offers": [offer],
                 "unmatched_value_candidate": True, "reason": "not_observed", "price_thb": "90000"},
                {"name": "Свежая без пары", "variant_id": "fresh", "best_offer_id": "best-fresh", "offers": [fresh_offer],
                 "unmatched_value_candidate": False, "reason": "not_observed", "price_thb": "80000"},
            ], "pairs": [], "best_pairs_by_th_variant_id": {}},
            "rankings": {},
        }
        parts = format_market_messages(result, {"requested_view": "general_top", "recipients": ["1"]}, now=NOW)
        text = "\n".join(part["text"] for part in parts)
        self.assertIn("12.10.2026 15:00 МСК", text)
        self.assertIn("историческ", text.lower())
        self.assertIn("не означает", text)
        fresh_state = next(part["text"] for part in parts if "Свежая без пары" in part["text"])
        self.assertNotIn("историческ", fresh_state.lower())

    def test_first_recipient_moves_the_clock_before_the_next(self) -> None:
        fresh = NOW.isoformat()

        class Clock:
            def __init__(self) -> None:
                self.current = NOW

            def now(self):
                return self.current

        clock = Clock()
        sender = TextSender()
        original = sender.send_text

        def send_text(event, text, **kwargs):
            original(event, text, **kwargs)
            if kwargs["recipient"] == "111":
                clock.current = NOW + timedelta(hours=48)

        sender.send_text = send_text
        result = {
            "fx": _fx(),
            "rankings": {"thai_top_value": [{
                "model_id": "shift", "name": "Shift", "best_offer_id": "s",
                "offers": [{"offer_id": "s", "source": "jib", "price": "80000", "url": "https://example.test/shift",
                            "availability_state": "in_stock", "price_checked_at": fresh, "availability_checked_at": fresh}],
            }]},
            "comparison": {},
        }
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        (self.jobs / "results" / "shift.json").write_text(json.dumps(result), encoding="utf-8")
        event = build_market_event(
            instance_id=INSTANCE, trigger_type="manual", requested_view="thai_top",
            recipients=["111", "222"], event_id="shift", now=NOW,
        )
        event.update(subscription_scan_id="shift", result_id="shift")
        (self.jobs / "delivery" / "shift.json").write_text(json.dumps(event), encoding="utf-8")
        self._run(sender=sender, clock=clock)
        first = " ".join(item["text"] for item in sender.sent if item["recipient"] == "111")
        second = " ".join(item["text"] for item in sender.sent if item["recipient"] == "222")
        self.assertIn("информационными", first)
        self.assertIn("не текущая", second)

    def test_confirmed_part_survives_both_chunk_boundaries(self) -> None:
        from thailand.market_telegram import MarketTextSender

        def row(name: str) -> dict:
            return {
                "model_id": "edge", "name": name, "best_offer_id": "e",
                "offers": [{"offer_id": "e", "source": "jib", "price": "1", "url": "https://example.test/edge",
                            "availability_state": "in_stock", "price_checked_at": NOW.isoformat(),
                            "availability_checked_at": NOW.isoformat()}],
            }

        def body_ids(name: str) -> list[str]:
            rendered = format_market_messages(
                {"rankings": {"thai_top_value": [row(name)]}, "comparison": {}, "fx": _fx()},
                {"requested_view": "thai_top", "recipients": ["1"]}, now=NOW,
            )
            return [part["part_id"] for part in rendered if part["part_id"].startswith("thai:edge#") and "state" not in part["part_id"]]

        short_name = "A"
        while len(body_ids(short_name)) < 1:
            short_name += "A"
        while len(body_ids(short_name)) == 1:
            one_name = short_name
            short_name += "A" * 40
        two_name = short_name
        self.assertEqual(body_ids(one_name), ["thai:edge#0"])
        self.assertEqual(body_ids(two_name), ["thai:edge#0", "thai:edge#1"])
        one_parts = format_market_messages(
            {"rankings": {"thai_top_value": [row(one_name)]}, "comparison": {}, "fx": _fx()},
            {"requested_view": "thai_top"}, now=NOW,
        )
        two_parts = format_market_messages(
            {"rankings": {"thai_top_value": [row(two_name)]}, "comparison": {}, "fx": _fx()},
            {"requested_view": "thai_top"}, now=NOW,
        )
        (self.jobs / "delivering").mkdir(parents=True)
        event = {"event_id": "edge", "recipients": ["1"], "requested_view": "thai_top", "delivered_part_ids": [
            {"id": "1:thai:edge#0", "text": next(part["text"] for part in two_parts if part["part_id"] == "thai:edge#0")},
        ]}
        (self.jobs / "delivering" / "edge.json").write_text(json.dumps(event), encoding="utf-8")
        grown = TextSender()
        MarketTextSender(grown, jobs_dir=self.jobs, now_fn=lambda: NOW).send(
            event, {"rankings": {"thai_top_value": [row(two_name)]}, "comparison": {}, "fx": _fx()},
            current=False, historical=True, info_current=True,
        )
        grown_ids = [item["part_id"] for item in grown.sent]
        self.assertNotIn("1:thai:edge#0", grown_ids)
        self.assertIn("1:thai:edge#1", grown_ids)
        event["delivered_part_ids"] = [
            {"id": "1:thai:edge#0", "text": next(part["text"] for part in two_parts if part["part_id"] == "thai:edge#0")},
            {"id": "1:thai:edge#1", "text": next(part["text"] for part in two_parts if part["part_id"] == "thai:edge#1")},
            {"id": "1:thai:edge#state:info", "text": "kept"},
        ]
        again = TextSender()
        MarketTextSender(again, jobs_dir=self.jobs, now_fn=lambda: NOW).send(
            event, {"rankings": {"thai_top_value": [row(two_name)]}, "comparison": {}, "fx": _fx()},
            current=False, historical=True, info_current=True,
        )
        again_ids = [item["part_id"] for item in again.sent]
        self.assertNotIn("1:thai:edge#0", again_ids)
        self.assertNotIn("1:thai:edge#1", again_ids)
        event["delivered_part_ids"] = [
            {"id": "1:thai:edge#0", "text": next(part["text"] for part in two_parts if part["part_id"] == "thai:edge#0")},
            {"id": "1:thai:edge#1", "text": next(part["text"] for part in two_parts if part["part_id"] == "thai:edge#1")},
            {"id": "1:thai:edge#state:info", "text": "kept"},
        ]
        shrunk = TextSender()
        MarketTextSender(shrunk, jobs_dir=self.jobs, now_fn=lambda: NOW + timedelta(hours=48)).send(
            event, {"rankings": {"thai_top_value": [row(one_name)]}, "comparison": {}, "fx": _fx()},
            current=False, historical=True, info_current=False,
        )
        shrunk_ids = [item["part_id"] for item in shrunk.sent]
        self.assertFalse(any(item == "1:thai:edge" for item in shrunk_ids))
        self.assertTrue(any(item.startswith("1:thai:edge#") for item in shrunk_ids))
        self.assertTrue(any("не текущая" in item["text"] for item in shrunk.sent))

    def test_th_only_coverage_reaches_the_message(self) -> None:
        from tests.test_thai_catalog_reuse import _result

        observed = NOW - timedelta(hours=2)
        _publish(
            self.catalog,
            [_offer("th-jib", "SKU-AAA017", 80_000, observed)],
            observed=observed,
            extra=[_result([], store="advice", rate_limited=True)],
        )
        enqueue_view_refresh(chat_id=42, view="thai_top", now=NOW)
        outcome = self._run(russian_snapshot=None, fx=_fx())
        result = json.loads((self.jobs / "results" / f"{outcome['result_id']}.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["comparison"])
        states = {row["source"]: row["state"] for row in result["coverage"]}
        self.assertEqual(states["jib"], "ok")
        self.assertEqual(states["advice"], "429")
        self.assertIn("failed", states.values())
        text = "\n".join(item["text"] for item in self.sender.sent)
        self.assertIn("advice: магазин ограничил запросы", text)
        self.assertIn("со сбоем", text)


if __name__ == "__main__":
    unittest.main()
