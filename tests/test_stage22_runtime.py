"""Stage 2.2-F2: real launch commands, mocked HTTP, temporary data."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import config
import httpx
from thai_catalog import export_thai_catalog
from thailand.discovery import GPU_SEARCH_QUERIES
from thailand.models import StoreScanResult, ThailandOffer

NOW = datetime.now(timezone.utc)
MOSCOW = ZoneInfo("Europe/Moscow")
INSTANCE = "c22-test"
CHAT = "42"


def _xml(when: datetime) -> bytes:
    published = when.astimezone(MOSCOW).strftime("%d.%m.%Y")
    text = f"""<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="{published}" name="Foreign Currency Market">
  <Valute ID="R01235">
    <CharCode>THB</CharCode>
    <Nominal>1</Nominal>
    <Value>2,50</Value>
  </Valute>
</ValCurs>
"""
    return text.encode("windows-1251")


def _evidence(when: datetime) -> dict:
    entry = {"method": "structured_api", "captured_at": when.isoformat()}
    return {name: dict(entry) for name in ("cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")}


def _offer(when: datetime) -> ThailandOffer:
    return ThailandOffer(
        store="jib",
        external_id="th-match",
        name="ASUS ROG Strix SKU-MATCH17",
        url="https://example.test/th-match",
        price_thb=80000,
        available=True,
        collected_at=when,
        sku="SKU-MATCH17",
        manufacturer_part_number="SKU-MATCH17",
        brand="ASUS",
        cpu="Intel Core Ultra 9 275HX",
        gpu="RTX 5070 Ti",
        gpu_source="structured_api",
        ram_gb=32,
        ssd_gb=1024,
        screen_size_inch=16.0,
        screen_resolution="2560x1600",
        availability_status="in_stock",
        availability_confirmed=True,
        availability_source="structured_api",
        price_source="structured_api",
        price_verified_for_variant=True,
        channel="direct",
        metadata={"field_evidence": _evidence(when)},
    )


def _publish_catalog(root: Path, when: datetime, *, discovered: datetime | None = None) -> None:
    checked = discovered or when
    export_thai_catalog(
        [StoreScanResult(
            store="jib",
            ok=True,
            offers=[_offer(when)],
            queries_finished=list(GPU_SEARCH_QUERIES),
            discovery_queries=list(GPU_SEARCH_QUERIES),
            request_count=len(GPU_SEARCH_QUERIES),
            collection_mode="http",
        )],
        snapshot_root=root,
        now=max(checked, when),
        discovery_checked_at=checked,
        instance_id=INSTANCE,
        started_at=checked,
    )


def _write_russian(root: Path, price: int, snapshot_id: str, when: datetime) -> None:
    observed = (when - timedelta(minutes=20)).isoformat()
    evidence = {
        "source": "regard",
        "external_id": "ru-17",
        "captured_at": observed,
        "method": "specs_cache",
        "capture_state": "known",
    }
    specs = {
        "cpu": "Intel Core Ultra 9 275HX",
        "gpu": "RTX 5070 Ti",
        "ram_gb": 32,
        "ssd_gb": 1024,
        "screen_inch": 16,
        "screen_resolution": "2560x1600",
    }
    offers = []
    for index in range(1, 26):
        code = "SKU-MATCH17" if index == 17 else f"SKU-RU-{index:02d}"
        eid = f"ru-{index:02d}"
        row_evidence = {**evidence, "external_id": eid}
        offers.append({
            "offer_id": eid,
            "external_id": eid,
            "store": "regard",
            "model_id": f"model-{eid}",
            "sku_candidates": [code],
            "mpn_candidates": [],
            "public_price_rub": price if index == 17 else 250000,
            "price_checked_at": observed,
            "availability_checked_at": observed,
            "observed_at": observed,
            "stock_state": "in_stock",
            "availability_evidence": {**row_evidence, "method": "store_listing", "available": True},
            "product_name": f"ASUS ROG Strix {code}",
            "specs": {name: {"value": value, "evidence": row_evidence} for name, value in specs.items()},
        })
    document = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "instance_id": INSTANCE,
        "created_at": when.isoformat(),
        "offers": offers,
        "source_coverage": [{"store": "regard", "state": "ok", "latest_attempt_at": observed}],
        "top_model_ids": ["model-ru-17", *[f"model-ru-{index:02d}" for index in range(1, 10)]],
        "personal_profiles": [],
    }
    folder = root / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (folder / f"{snapshot_id}.json").write_bytes(raw)
    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "path": f"snapshots/{snapshot_id}.json",
        "instance_id": INSTANCE,
        "created_at": when.isoformat(),
    }
    (root / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")


class Signal:
    fingerprint = "runtime-buy"
    cluster_name = "runtime-buy"


class RuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.jobs = self.root / "jobs"
        self.catalog = self.root / "catalog"
        self.russian = self.root / "russian"
        self.fx_path = self.root / "fx_thb.json"
        self.health = self.root / "health.json"
        self.health.write_text('{"version": 1, "stores": {}}\n', encoding="utf-8")
        self.cwd = Path.cwd()
        os.chdir(self.root)
        self.saved_env = {}
        for key, value in {
            "STAGE22_MARKET_TELEGRAM": "1",
            "LAPTOP_MONITOR_INSTANCE": INSTANCE,
            "TELEGRAM_BOT_TOKEN": "123:ABC",
            "TELEGRAM_CHAT_ID": CHAT,
            "TELEGRAM_ADMIN_CHAT_ID": CHAT,
        }.items():
            self.saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.calls: list[str] = []
        self.messages: list[dict] = []
        self.store_status = 503
        self.cbr_status = 200
        self.fail_send_after: int | None = None
        real_client = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            host = request.url.host
            if host.endswith("cbr.ru"):
                if self.cbr_status != 200:
                    return httpx.Response(self.cbr_status, text="no")
                return httpx.Response(200, content=_xml(datetime.now(timezone.utc)))
            if host == "api.telegram.org":
                payload = json.loads(request.content.decode("utf-8") or "{}")
                if str(request.url).endswith("sendMessage"):
                    self.messages.append(payload)
                    if self.fail_send_after is not None and len(self.messages) > self.fail_send_after:
                        return httpx.Response(500, json={"ok": False, "description": "fail"})
                return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.messages) or 1}})
            if self.store_status == 429:
                return httpx.Response(429, headers={"Retry-After": "3600"}, text="later")
            return httpx.Response(self.store_status, text="down")

        class RoutedClient(real_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        self.patches = [
            patch.object(config, "THAILAND_AUTOMATIC_ENABLED", True),
            patch.object(config, "THAILAND_JOBS_DIR", str(self.jobs)),
            patch.object(config, "THAI_CATALOG_ROOT", str(self.catalog)),
            patch.object(config, "RUSSIAN_MARKET_ROOT", str(self.russian)),
            patch.object(config, "FX_OBSERVATION_PATH", str(self.fx_path)),
            patch.object(config, "THAILAND_SOURCE_HEALTH_PATH", str(self.health)),
            patch.object(config, "THAILAND_WORKER_STATE_PATH", str(self.root / "worker.json")),
            patch.object(config, "TELEGRAM_MIN_SEND_INTERVAL_SECONDS", 0),
            patch("httpx.Client", RoutedClient),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.chdir(self.cwd)
        self._tmp.cleanup()

    def _store_calls(self) -> list[str]:
        return [url for url in self.calls if "cbr.ru" not in url and "api.telegram.org" not in url]

    def _cbr_calls(self) -> list[str]:
        return [url for url in self.calls if "cbr.ru" in url]

    def _wake(self) -> int:
        from thailand.worker import main

        return main(["--once"])

    def _daily(self, now: datetime | None = None) -> dict:
        from thailand.market_schedule import enqueue_daily

        if now is None:
            # These delivery cases require the daily 08:00 Moscow window to be open.
            now = datetime.now(MOSCOW).replace(hour=8, minute=1, second=0, microsecond=0)
        return enqueue_daily(now=now)

    def test_idle_wake_does_not_call_cbr_or_stores(self) -> None:
        self.assertEqual(self._wake(), 0)
        self.assertEqual(self.calls, [])

    def test_first_launch_with_empty_catalogs(self) -> None:
        self._daily()
        self._wake()
        self.assertGreater(len(self._store_calls()), 0)
        self.assertTrue(self.fx_path.is_file())
        saved = json.loads(self.fx_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["source"], "CBR")
        self.assertRegex(saved["published_date"], r"^\d{4}-\d{2}-\d{2}$")
        texts = [item["text"] for item in self.messages]
        self.assertEqual(len(texts), 1)
        self.assertTrue(all(str(item["chat_id"]) == CHAT for item in self.messages))
        self.assertIn("Обновление не удалось", texts[0])
        self.assertNotIn("показывать нечего", texts[0])
        self.assertNotIn("подходящих", texts[0])
        saved = json.loads(list((self.jobs / "results").glob("*.json"))[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "failed")
        self.assertTrue(saved["errors"])
        self.assertTrue(saved["coverage"])

    def test_daily_button_and_buy_share_one_pass(self) -> None:
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        _publish_catalog(self.catalog, fresh)
        _write_russian(self.russian, 250000, "ru-a", fresh)
        from thailand.market_telegram import enqueue_buy_candidate, enqueue_view_refresh

        self._daily()
        enqueue_view_refresh(chat_id=CHAT, view="general_top", trigger="manual")
        enqueue_buy_candidate(Signal(), instance_id=INSTANCE, jobs_dir=None, now=datetime.now(timezone.utc))
        before = len(self._store_calls())
        self._wake()
        self.assertEqual(self._store_calls(), [])
        self.assertGreater(len(self._cbr_calls()), 0)
        archived = list((self.jobs / "archive").glob("*.json"))
        self.assertEqual(len(archived), 3)
        scans = {json.loads(path.read_text(encoding="utf-8"))["subscription_scan_id"] for path in archived}
        self.assertEqual(len(scans), 1)
        texts = [item["text"] for item in self.messages]
        self.assertEqual(len(texts), 2)  # Compact daily and distinct manual; BUY reuses manual ACK.
        self.assertIn("Таиланд дешевле", texts[0])
        self.assertIn("50000.0", texts[0])
        self.assertEqual(before, 0)
        for path in archived:
            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["delivery_state"], "sent")
            self.assertTrue(event.get("delivered_part_ids"))
        Path(tempfile.gettempdir(), "f2_one_request.json").write_text(
            json.dumps({"count": len(texts), "texts": texts, "events": len(archived)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_fresh_repeat_does_not_call_stores(self) -> None:
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        _publish_catalog(self.catalog, fresh)
        _write_russian(self.russian, 250000, "ru-a", fresh)
        self._daily()
        self._wake()
        sent = len(self.messages)
        from thailand.market_telegram import enqueue_view_refresh

        enqueue_view_refresh(chat_id=CHAT, view="general_top", trigger="manual")
        self._wake()
        self.assertEqual(self._store_calls(), [])
        self.assertGreater(len(self.messages), sent)

    def test_stale_quotes_and_refused_sources_do_not_loop(self) -> None:
        now = datetime.now(timezone.utc)
        _publish_catalog(self.catalog, now - timedelta(hours=9), discovered=now - timedelta(hours=2))
        _write_russian(self.russian, 250000, "ru-a", now)
        self.store_status = 429
        self._daily()
        self._wake()
        self.assertGreater(len(self._store_calls()), 0)
        joined = "\n".join(item["text"] for item in self.messages)
        self.assertNotIn("Таиланд дешевле", joined)
        self.assertNotIn("SKU-MATCH17", joined)  # Stale TH is not a morning card.
        seen = len(self._store_calls())
        self._wake()
        self.assertEqual(len(self._store_calls()), seen)

    def test_deferred_wakes_only_after_the_deadline(self) -> None:
        tomorrow = datetime.now(MOSCOW).replace(hour=7, minute=0, second=0, microsecond=0) + timedelta(days=1)
        stored = self._daily(tomorrow)
        self.assertEqual(stored["status"], "deferred")
        self._wake()
        self._wake()
        self.assertEqual(self.calls, [])
        self.assertTrue((self.jobs / "deferred" / f"{stored['event_id']}.json").is_file())
        path = self.jobs / "deferred" / f"{stored['event_id']}.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        body["not_before"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(body), encoding="utf-8")
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        _publish_catalog(self.catalog, fresh)
        _write_russian(self.russian, 250000, "ru-due", fresh)
        self._wake()
        self.assertFalse(path.exists())
        self.assertTrue((self.jobs / "archive" / f"{stored['event_id']}.json").is_file())
        self.assertEqual(self._store_calls(), [])

    def test_restart_does_not_resend_a_confirmed_part_or_rescan(self) -> None:
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        _publish_catalog(self.catalog, fresh)
        _write_russian(self.russian, 250000, "ru-a", fresh)
        self._daily()
        self._wake()
        self.assertEqual(len(self.messages), 1)
        self.messages.clear()
        self._wake()
        self.assertEqual(self.messages, [])
        self.assertEqual(self._store_calls(), [])

    def test_new_russian_snapshot_joins_the_next_comparison(self) -> None:
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        _publish_catalog(self.catalog, fresh)
        _write_russian(self.russian, 250000, "ru-old", fresh)
        self._daily()
        self._wake()
        self.assertIn("Таиланд дешевле", "\n".join(item["text"] for item in self.messages))
        _write_russian(self.russian, 100000, "ru-new", datetime.now(timezone.utc) - timedelta(minutes=10))
        from thailand.market_telegram import enqueue_view_refresh

        enqueue_view_refresh(chat_id=CHAT, view="general_top", trigger="manual")
        self.messages.clear()
        self._wake()
        joined = "\n".join(item["text"] for item in self.messages)
        self.assertIn("Россия дешевле", joined)
        self.assertNotIn("Таиланд дешевле", joined)
        self.assertEqual(self._store_calls(), [])

    def test_refused_refresh_keeps_the_previous_snapshot(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        _publish_catalog(self.catalog, old, discovered=old)
        before = (self.catalog / "latest.json").read_bytes()
        snapshot = next((self.catalog / "snapshots").glob("*.json"))
        stamped = snapshot.read_bytes()
        self._daily()
        self._wake()
        self.assertEqual((self.catalog / "latest.json").read_bytes(), before)
        self.assertEqual(snapshot.read_bytes(), stamped)
        self.assertIn("Обновление не удалось", self.messages[0]["text"])
        self.assertNotIn("показывать нечего", self.messages[0]["text"])

    def test_successful_empty_search_is_not_a_refusal(self) -> None:
        self.store_status = 200
        self._daily()
        self._wake()
        self.assertGreater(len(self._store_calls()), 0)
        self.assertIn("Сейчас нет предложений", self.messages[0]["text"])
        self.assertNotIn("Обновление не удалось", self.messages[0]["text"])

    def test_missing_or_old_fx_does_not_grant_a_current_benefit(self) -> None:
        fresh = datetime.now(timezone.utc) - timedelta(minutes=30)
        _publish_catalog(self.catalog, fresh)
        _write_russian(self.russian, 250000, "ru-a", fresh)
        self.cbr_status = 500
        self._daily()
        self._wake()
        self.assertFalse(self.fx_path.exists())
        self.assertNotIn("Таиланд дешевле", "\n".join(item["text"] for item in self.messages))
        published = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
        old = {
            "fx_id": f"cbr-{published}-1",
            "source": "CBR",
            "currency": "THB",
            "nominal": 1,
            "rub_per_thb": "2.5",
            "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
            "published_date": published,
            "error": None,
        }
        self.fx_path.write_text(json.dumps(old), encoding="utf-8")
        from thailand.market_telegram import enqueue_view_refresh

        enqueue_view_refresh(chat_id=CHAT, view="general_top", trigger="manual")
        self.messages.clear()
        self._wake()
        self.assertEqual(json.loads(self.fx_path.read_text(encoding="utf-8"))["published_date"], old["published_date"])
        self.assertNotIn("Таиланд дешевле", "\n".join(item["text"] for item in self.messages))

    def test_disabled_flag_keeps_the_old_path(self) -> None:
        os.environ["STAGE22_MARKET_TELEGRAM"] = "0"
        from personal_top import enqueue_personal_job
        from thailand.market_schedule import main as schedule_main

        self.assertEqual(schedule_main(["enqueue-daily"]), 0)
        self.assertFalse((self.jobs / "pending").exists() and any((self.jobs / "pending").glob("*.json")))
        queued = enqueue_personal_job(manual=False, products=[], jobs_dir=self.jobs, state={"checked_at": None}, now=datetime.now(timezone.utc))
        self.assertTrue(queued["ok"])
        pending = list((self.jobs / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        body = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertNotEqual(body.get("schema_version"), 2)
        self.assertNotEqual(body.get("trigger_type"), "daily")
        os.environ["STAGE22_MARKET_TELEGRAM"] = "1"
        from personal_top import main as personal_main

        self.calls.clear()
        personal_main(["enqueue"])
        market = [json.loads(path.read_text(encoding="utf-8")) for path in (self.jobs / "pending").glob("*.json") if json.loads(path.read_text(encoding="utf-8")).get("schema_version") == 2]
        self.assertEqual(len(market), 1)
        self.assertEqual(market[0]["trigger_type"], "personal")
        self.assertEqual(market[0]["requested_view"], "personal_top3")
        self.assertEqual(self._store_calls(), [])

    def test_schedule_units_are_prepared_and_do_not_watch_deferred(self) -> None:
        root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        daily = (root / "laptop-monitor-market-daily.timer").read_text(encoding="utf-8")
        retry = (root / "laptop-monitor-market-retry.timer").read_text(encoding="utf-8")
        retry_service = (root / "laptop-monitor-market-retry.service").read_text(encoding="utf-8")
        personal = (root / "laptop-monitor-personal-top.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 08:00:00 Europe/Moscow", daily)
        self.assertNotIn("DirectoryNotEmpty", retry)
        self.assertNotIn("PathExists", retry)
        self.assertIn("OnCalendar=*-*-* *:0/15:00", retry)
        self.assertIn("python -m thailand.worker --once", retry_service)
        self.assertNotIn("Restart=", retry_service)
        self.assertIn("python -m personal_top enqueue", personal)
        self.assertNotIn(b"\r", (root / "laptop-monitor-market-daily.timer").read_bytes())


if __name__ == "__main__":
    unittest.main()
