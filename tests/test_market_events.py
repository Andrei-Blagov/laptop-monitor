"""Offline market-event queue. The worker path is process_one, not a side helper."""
from __future__ import annotations

import json
import multiprocessing
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thai_catalog import REFERENCES_NAME, retain_thai_catalogs
from thailand.catalog_refresh import ManualClock, OfferRefresh
from thailand.job_models import build_job
from thailand.job_queue import enqueue_job
from thailand.market_events import (
    build_market_event,
    discovery_key,
    enqueue_market_event,
    next_daily_not_before,
    promote_due_events,
    recover_market_scan,
)
from thailand.worker import process_one
from tests.test_thai_catalog_reuse import NOW, _fx, _offer, _publish

INSTANCE = "c22-test"


def _pair(pair_id: str, *, th_availability: str, ru_price: str, checked: str | None = None) -> dict:
    fresh = checked or th_availability
    return {
        "pair_id": pair_id,
        "comparison_status": "confirmed",
        "th_observation": {
            "price": "80000", "price_checked_at": fresh, "availability_checked_at": th_availability,
            "name": "ASUS ROG", "url": "https://example.test/th", "specs": {"gpu": {"value": "RTX 5070 Ti"}},
        },
        "ru_observation": {
            "price": "250000", "price_checked_at": ru_price, "availability_checked_at": fresh,
            "name": "ASUS ROG", "url": "https://example.test/ru", "specs": {"gpu": {"value": "RTX 5070 Ti"}},
        },
        "saving_rub": "1000", "conflicting_fields": [], "commercial_differences": [], "confirmed_expenses": [],
    }


class FakeSender:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[dict] = []

    def send(self, event, result, *, current: bool, historical: bool, info_current: bool = False) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("delivery")
        self.sent.append({
            "event_id": event["event_id"],
            "recipients": list(event["recipients"]),
            "current": current,
            "historical": historical,
            "info_current": info_current,
            "view": event["requested_view"],
        })


class CountingTransport:
    def __init__(self, counter: Path | None = None, hold: Path | None = None) -> None:
        self.calls = 0
        self.counter = counter
        self.hold = hold

    def refresh_offer(self, item, *, mode, now, clock=None):
        del mode, clock
        self.calls += 1
        if self.counter is not None:
            with self.counter.open("a", encoding="utf-8") as handle:
                handle.write("1\n")
        if self.hold is not None:
            self.hold.write_text("scanning", encoding="utf-8")
            release = self.hold.with_name("release")
            while not release.exists():
                time.sleep(0.02)
        return OfferRefresh(
            source=str(item.get("source") or "jib"),
            external_id=str(item.get("external_id") or ""),
            sku=item.get("sku"),
            status="updated",
            price_thb=70_000,
            availability_state="in_stock",
            availability_confirmed=True,
            observed_at=now,
            http_calls=1,
        )

    def discover_source(self, source, *, now):
        raise AssertionError(f"discover {source} {now}")


def _hold_child(jobs: str, catalog: str, health: str, counter: str, hold: str) -> None:
    process_one(
        jobs_dir=jobs, catalog_root=catalog, health_path=health, fx=_fx(),
        transport=CountingTransport(counter=Path(counter), hold=Path(hold)),
        sender=FakeSender(), clock=ManualClock(NOW), deliver=False,
        state_path=str(Path(jobs) / "buy-state.json"),
    )


def _delivery_child(jobs: str, catalog: str, health: str, hold: str, release: str, log: str) -> None:
    class _Slow:
        def send(self, event, result, *, current: bool, historical: bool, info_current: bool = False) -> None:
            del result, current, historical
            Path(hold).write_text("sending", encoding="utf-8")
            while not Path(release).exists():
                time.sleep(0.02)
            with Path(log).open("a", encoding="utf-8") as handle:
                handle.write(event["event_id"] + "\n")

    process_one(
        jobs_dir=jobs, catalog_root=catalog, health_path=health, fx=_fx(),
        sender=_Slow(), clock=ManualClock(NOW), deliver=True,
        state_path=str(Path(jobs) / "buy-state.json"),
    )


def _publish_identity(path: str) -> None:
    import os
    from thailand.market_events import process_started_at
    Path(path).write_text(json.dumps({
        "pid": os.getpid(),
        "started": process_started_at(os.getpid()),
    }), encoding="utf-8")
    time.sleep(30)


def _hold_delivering(jobs: str, ready: str, release: str) -> None:
    import os
    from thailand.market_events import process_started_at
    path = Path(jobs) / "delivering" / "held-event.json"
    event = json.loads(path.read_text(encoding="utf-8"))
    event["delivery_owner"] = os.getpid()
    event["delivery_owner_started_at"] = process_started_at(os.getpid())
    path.write_text(json.dumps(event), encoding="utf-8")
    Path(ready).write_text("1", encoding="utf-8")
    while not Path(release).exists():
        time.sleep(0.02)


def _child(jobs: str, catalog: str, health: str, counter: str) -> None:
    process_one(
        jobs_dir=jobs,
        catalog_root=catalog,
        health_path=health,
        fx=_fx(),
        transport=CountingTransport(counter=Path(counter)),
        sender=FakeSender(),
        clock=ManualClock(NOW),
        deliver=True,
        state_path=str(Path(jobs) / "buy-state.json"),
    )


class MarketEventQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs"
        self.catalog = self.root / "catalog"
        self.health = self.root / "health.json"
        self.health.write_text('{"version": 1, "stores": {}}\n', encoding="utf-8")
        self.jobs.mkdir()
        self.sender = FakeSender()
        self.transport = CountingTransport()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _event(self, trigger: str, recipients: list[str], event_id: str, *, when: datetime | None = None, view: str = "general_top") -> dict:
        return build_market_event(
            instance_id=INSTANCE,
            trigger_type=trigger,
            requested_view=view,
            recipients=recipients,
            event_id=event_id,
            now=when or NOW,
            source_ru_snapshot_id="ru-buy" if trigger == "buy" else None,
        )

    def _catalog(self, hours: int) -> None:
        observed = NOW - timedelta(hours=hours)
        _publish(self.catalog, [_offer("th-1", "SKU-AAA017", 80_000, observed)], observed=observed)

    def _run(self) -> dict | None:
        return process_one(
            jobs_dir=self.jobs,
            catalog_root=self.catalog,
            health_path=self.health,
            fx=_fx(),
            transport=self.transport,
            sender=self.sender,
            clock=ManualClock(NOW),
            deliver=True,
            state_path=self.root / "buy-state.json",
        )

    def _stored(self, event_id: str) -> dict:
        for folder in ("archive", "delivery", "deferred", "pending", "processing"):
            path = self.jobs / folder / f"{event_id}.json"
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        self.fail(event_id)

    def test_three_triggers_share_one_reuse_pass(self) -> None:
        self._catalog(2)
        for trigger, people, event_id in (
            ("daily", ["daily-chat"], "11111111-1111-1111-1111-111111111111"),
            ("manual", ["manual-chat"], "22222222-2222-2222-2222-222222222222"),
            ("buy", ["buy-chat"], "33333333-3333-3333-3333-333333333333"),
        ):
            enqueue_market_event(self._event(trigger, people, event_id), root=self.jobs, now=NOW)
        first = self._run()
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(first["http_calls"], 0)
        stored = [self._stored(event_id) for event_id in (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        )]
        self.assertEqual(len({row["result_id"] for row in stored}), 1)
        self.assertEqual([row["recipients"] for row in stored], [["daily-chat"], ["manual-chat"], ["buy-chat"]])
        self.assertEqual(stored[2]["source_ru_snapshot_id"], "ru-buy")
        self.assertEqual({row["event_id"] for row in self.sender.sent}, {row["event_id"] for row in stored})

    def test_same_event_id_is_not_duplicated(self) -> None:
        self._catalog(2)
        event = self._event("manual", ["one"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertFalse(enqueue_market_event(event, root=self.jobs, now=NOW)["duplicate"])
        again = enqueue_market_event(event, root=self.jobs, now=NOW)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(list((self.jobs / "pending").glob("*.json"))), 1)

    def test_daily_before_eight_stays_out_of_pending(self) -> None:
        morning = datetime(2026, 10, 14, 4, 30, tzinfo=timezone.utc)  # 07:30 Moscow
        due = next_daily_not_before(morning)
        self.assertIsNotNone(due)
        event = self._event("daily", ["chat"], "daily-early", when=morning)
        event["not_before"] = due.isoformat()
        enqueue_market_event(event, root=self.jobs, now=morning)
        self.assertEqual(list((self.jobs / "pending").glob("*.json")), [])
        self.assertTrue((self.jobs / "deferred" / "daily-early.json").is_file())
        wakes = 0
        while wakes < 3:
            wakes += 1
            found = process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
                transport=self.transport, sender=self.sender, clock=ManualClock(morning),
                deliver=False, state_path=self.root / "buy-state.json",
            )
            if found is None:
                break
        self.assertEqual(wakes, 1)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(promote_due_events(root=self.jobs, now=morning), [])
        self.assertEqual(promote_due_events(root=self.jobs, now=due), ["daily-early"])
        self.assertEqual(list((self.jobs / "deferred").glob("*.json")), [])

    def test_source_limit_defers_without_a_second_pass(self) -> None:
        self._catalog(9)
        until = (NOW + timedelta(hours=2)).isoformat()
        self.health.write_text(json.dumps({"version": 1, "stores": {"jib": {
            "disabled_until": until, "consecutive_failures": 3,
        }}}), encoding="utf-8")
        enqueue_market_event(self._event("manual", ["chat"], "limited"), root=self.jobs, now=NOW)
        outcome = self._run()
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(list((self.jobs / "pending").glob("*.json")), [])
        self.assertTrue((self.jobs / "deferred" / "limited.json").is_file())
        again = self._run()
        self.assertIsNone(again)
        self.assertEqual(self.transport.calls, 0)

    def test_delivery_retry_does_not_call_stores(self) -> None:
        self._catalog(2)
        self.sender.failures = 1
        enqueue_market_event(self._event("manual", ["chat"], "deliver-1"), root=self.jobs, now=NOW)
        failed = self._run()
        self.assertEqual(failed["status"], "delivery_failed")
        self.assertTrue((self.jobs / "deferred" / "deliver-1.json").is_file())
        self.assertEqual(self.sender.sent, [])
        self.assertIsNone(self._run())
        self.assertEqual(self.transport.calls, 0)
        later = process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
            transport=self.transport, sender=self.sender, clock=ManualClock(NOW + timedelta(minutes=31)),
            deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(later["http_calls"], 0)
        self.assertEqual(self.sender.sent[0]["event_id"], "deliver-1")
        self.assertEqual(self._stored("deliver-1")["delivery_state"], "sent")

    def test_stale_purchase_is_not_sent_as_current(self) -> None:
        self.jobs.mkdir(exist_ok=True)
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        fresh = NOW.isoformat()
        stale = (NOW - timedelta(hours=9)).isoformat()
        result = {
            "result_id": "scan-old", "scan_id": "scan-old", "snapshot_id": "snap-old",
            "fx": {**_fx(), "fetched_at": fresh},
            "comparison": {"pairs": [
                _pair("stale-avail", th_availability=stale, ru_price=fresh),
                _pair("stale-ru", th_availability=fresh, ru_price=stale),
            ]},
            "views": {"general_top": ["stale-avail", "stale-ru"], "all": ["stale-avail", "stale-ru"], "personal_top3": []},
            "http_calls": 0,
        }
        (self.jobs / "results" / "scan-old.json").write_text(json.dumps(result), encoding="utf-8")
        event = self._event("buy", ["chat"], "stale-buy")
        event["subscription_scan_id"] = "scan-old"
        event["result_id"] = "scan-old"
        (self.jobs / "delivery" / "stale-buy.json").write_text(json.dumps(event), encoding="utf-8")
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            transport=self.transport, sender=self.sender, clock=ManualClock(NOW),
            deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.sender.sent[0]["current"], False)
        self.assertEqual(self.sender.sent[0]["historical"], True)
        stored = self._stored("stale-buy")
        self.assertEqual(stored["delivery_state"], "sent")
        self.assertEqual(stored["delivered_as"], "historical")
        self.assertFalse((self.jobs / "deferred" / "stale-buy.json").exists())

    def test_referenced_snapshot_survives_retention(self) -> None:
        self._catalog(2)
        enqueue_market_event(self._event("manual", ["chat"], "keep-snap"), root=self.jobs, now=NOW)
        self._run()
        stored = self._stored("keep-snap")
        snapshot_id = stored["result_snapshot_id"]
        extra = self.catalog / "snapshots" / "unreferenced.json"
        extra.write_text("{}", encoding="utf-8")
        deleted = retain_thai_catalogs(self.catalog)
        self.assertIn("unreferenced", deleted)
        self.assertTrue((self.catalog / "snapshots" / f"{snapshot_id}.json").is_file())
        refs = json.loads((self.catalog / REFERENCES_NAME).read_text(encoding="utf-8"))
        self.assertIn(snapshot_id, refs["snapshot_ids"])

    def test_late_subscriber_receives_the_same_result(self) -> None:
        self._catalog(9)
        hold = self.root / "hold"
        self.transport.hold = hold
        enqueue_market_event(self._event("manual", ["first"], "late-1"), root=self.jobs, now=NOW)

        def _background() -> None:
            process_one(
                jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
                transport=self.transport, sender=FakeSender(), clock=ManualClock(NOW),
                deliver=True, state_path=self.root / "buy-state.json",
            )

        import threading
        thread = threading.Thread(target=_background)
        thread.start()
        deadline = time.monotonic() + 5
        while not hold.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        enqueue_market_event(self._event("buy", ["second"], "late-2"), root=self.jobs, now=NOW)
        hold.with_name("release").write_text("go", encoding="utf-8")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self._stored("late-1")["result_id"], self._stored("late-2")["result_id"])
        self.assertEqual(self._stored("late-2")["recipients"], ["second"])

    def test_crashed_claim_returns_to_pending_without_a_second_buy(self) -> None:
        self._catalog(9)
        event = self._event("buy", ["chat"], "crash-1")
        enqueue_market_event(event, root=self.jobs, now=NOW)
        (self.jobs / "processing").mkdir(parents=True, exist_ok=True)
        (self.jobs / "pending" / "crash-1.json").replace(self.jobs / "processing" / "crash-1.json")
        scan = {
            "scan_id": "crashed",
            "discovery_key": discovery_key(INSTANCE),
            "claimed_at": (NOW - timedelta(hours=2)).isoformat(),
            "event_ids": ["crash-1"],
            "owner_pid": 1,
        }
        (self.jobs / "active_scan.json").write_text(json.dumps(scan), encoding="utf-8")
        recovered = recover_market_scan(root=self.jobs, now=NOW, stale_seconds=60)
        self.assertEqual(recovered, ["crash-1"])
        self.assertFalse((self.jobs / "active_scan.json").exists())
        self.assertTrue((self.jobs / "pending" / "crash-1.json").is_file())
        self._run()
        self.assertEqual(self.transport.calls, 1)

    def test_two_processes_perform_one_scan(self) -> None:
        if multiprocessing.current_process().name != "MainProcess":
            return
        self._catalog(9)
        enqueue_market_event(self._event("manual", ["a"], "proc-1"), root=self.jobs, now=NOW)
        enqueue_market_event(self._event("buy", ["b"], "proc-2"), root=self.jobs, now=NOW)
        counter = self.root / "counter.txt"
        counter.write_text("", encoding="utf-8")
        args = (str(self.jobs), str(self.catalog), str(self.health), str(counter))
        first = multiprocessing.Process(target=_child, args=args)
        second = multiprocessing.Process(target=_child, args=args)
        first.start()
        second.start()
        first.join(8)
        second.join(8)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(counter.read_text(encoding="utf-8").count("1"), 1)
        self.assertEqual(self._stored("proc-1")["result_id"], self._stored("proc-2")["result_id"])

    def test_old_buy_job_is_not_sent_twice(self) -> None:
        from buy_opportunity import BuySignal

        signal = BuySignal(
            level="BUY", model_key="m", cluster_name="Model", price=100, store="regard",
            gpu="RTX 5070 Ti", cpu=None, ram_gb=32, ssd_gb=1024, screen_inch=16,
            score=1, confidence=1, historical_min=None, target_price=None, url=None,
            reasons=[], rule_ids=[], over_hist_pct=None, fingerprint="buy-fp-1",
            cluster_key=None, history_started_at=None,
        )
        job = build_job(trigger_type="buy_signal", signals=[signal], russian_deals=[], dedupe_key="buy-fp-1", instance_id=INSTANCE)
        enqueue_job(job, root=self.jobs)
        from thailand.market_events import remember_buy_delivery
        remember_buy_delivery(job, self.jobs)

        def explode(**_kwargs):
            raise AssertionError("old buy scanned again")

        archived = process_one(
            jobs_dir=self.jobs, deliver=True, scan_fn=explode,
            state_path=self.root / "buy-state.json",
        )
        self.assertEqual(archived["buy_delivery"], "already_sent")
        self.assertFalse((self.jobs / "pending" / f"{job['job_id']}.json").exists())

    def test_missing_sender_keeps_the_waiting_event(self) -> None:
        self._catalog(2)
        enqueue_market_event(self._event("manual", ["chat"], "hold-1"), root=self.jobs, now=NOW)
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
            transport=self.transport, sender=self.sender, clock=ManualClock(NOW),
            deliver=False, state_path=self.root / "buy-state.json",
        )
        enqueue_market_event(self._event("manual", ["chat"], "hold-2"), root=self.jobs, now=NOW)
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
            transport=self.transport, sender=None, clock=ManualClock(NOW),
            deliver=True, state_path=self.root / "buy-state.json",
        )
        for event_id in ("hold-1", "hold-2"):
            self.assertTrue((self.jobs / "delivery" / f"{event_id}.json").is_file())
            self.assertFalse((self.jobs / "archive" / f"{event_id}.json").exists())
            self.assertNotEqual(self._stored(event_id).get("delivery_state"), "sent")
        self.assertEqual(self.sender.sent, [])

    def test_two_processes_do_not_send_one_event(self) -> None:
        if multiprocessing.current_process().name != "MainProcess":
            return
        fresh = NOW.isoformat()
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        result = {"result_id": "scan-send", "scan_id": "scan-send", "fx": {"fetched_at": fresh},
                  "views": {"general_top": [{"model": "A", "price_thb": "1", "price_checked_at": fresh,
                                             "availability_checked_at": fresh}]}}
        (self.jobs / "results" / "scan-send.json").write_text(json.dumps(result), encoding="utf-8")
        event = self._event("manual", ["chat"], "once")
        event["subscription_scan_id"] = "scan-send"
        event["result_id"] = "scan-send"
        (self.jobs / "delivery" / "once.json").write_text(json.dumps(event), encoding="utf-8")
        hold = self.root / "sending"
        release = self.root / "release-send"
        log = self.root / "sent.txt"
        log.write_text("", encoding="utf-8")
        args = (str(self.jobs), str(self.catalog), str(self.health), str(hold), str(release), str(log))
        first = multiprocessing.Process(target=_delivery_child, args=args)
        second = multiprocessing.Process(target=_delivery_child, args=args)
        first.start()
        second.start()
        deadline = time.monotonic() + 5
        while not hold.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        release.write_text("go", encoding="utf-8")
        first.join(8)
        second.join(8)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(log.read_text(encoding="utf-8").count("once"), 1)

    def test_persistent_delivery_failure_does_not_spin(self) -> None:
        from thailand.worker import drain

        self._catalog(2)
        self.sender.failures = 20
        enqueue_market_event(self._event("manual", ["chat"], "spin"), root=self.jobs, now=NOW)
        processed = drain(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
            transport=self.transport, sender=self.sender, clock=ManualClock(NOW),
            deliver=True, max_jobs=8, state_path=self.root / "buy-state.json",
        )
        self.assertLess(processed, 4)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.sender.sent, [])
        self.assertTrue((self.jobs / "deferred" / "spin.json").is_file())
        waiting = json.loads((self.jobs / "deferred" / "spin.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(waiting.get("not_before"))

    def test_different_keys_and_v1_do_not_scan_together(self) -> None:
        if multiprocessing.current_process().name != "MainProcess":
            return
        self._catalog(9)
        hold = self.root / "hold"
        counter = self.root / "counter.txt"
        counter.write_text("", encoding="utf-8")
        enqueue_market_event(self._event("manual", ["a"], "key-a"), root=self.jobs, now=NOW)
        worker = multiprocessing.Process(
            target=_hold_child,
            args=(str(self.jobs), str(self.catalog), str(self.health), str(counter), str(hold)),
        )
        worker.start()
        deadline = time.monotonic() + 5
        while not hold.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        other = build_market_event(
            instance_id="other-instance", trigger_type="manual", requested_view="general_top",
            recipients=["b"], event_id="key-b", now=NOW,
        )
        enqueue_market_event(other, root=self.jobs, now=NOW)
        calls = {"n": 0}

        def scan_fn(**_kwargs):
            calls["n"] += 1
            return {"status": "ok", "stores": [{"store": "jib"}], "messages_sent": 0}

        from buy_opportunity import BuySignal
        signal = BuySignal(
            level="BUY", model_key="m", cluster_name="Model", price=100, store="regard",
            gpu="RTX 5070 Ti", cpu=None, ram_gb=32, ssd_gb=1024, screen_inch=16,
            score=1, confidence=1, historical_min=None, target_price=None, url=None,
            reasons=[], rule_ids=[], over_hist_pct=None, fingerprint="busy-fp",
            cluster_key=None, history_started_at=None,
        )
        job = build_job(trigger_type="buy_signal", signals=[signal], russian_deals=[], dedupe_key="busy-fp", instance_id=INSTANCE)
        enqueue_job(job, root=self.jobs)
        second = process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
            transport=CountingTransport(), sender=FakeSender(), clock=ManualClock(NOW),
            deliver=True, scan_fn=scan_fn, state_path=self.root / "buy-state.json",
        )
        self.assertIsNone(second)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(counter.read_text(encoding="utf-8").count("1"), 1)
        self.assertTrue((self.jobs / "pending" / "key-b.json").is_file())
        self.assertTrue((self.jobs / "pending" / f"{job['job_id']}.json").is_file())
        active = json.loads((self.jobs / "active_scan.json").read_text(encoding="utf-8"))
        self.assertEqual(active["discovery_key"], discovery_key(INSTANCE))
        hold.with_name("release").write_text("go", encoding="utf-8")
        worker.join(8)
        self.assertFalse(worker.is_alive())

    def test_saved_result_contains_message_data(self) -> None:
        self._catalog(2)
        enqueue_market_event(self._event("manual", ["chat"], "render"), root=self.jobs, now=NOW)
        self._run()
        event = self._stored("render")
        result = json.loads((self.jobs / "results" / f"{event['result_id']}.json").read_text(encoding="utf-8"))
        model_id = result["views"]["general_top"][0]
        row = next(item for item in result["rankings"]["thai_top_value"] if item["model_id"] == model_id)
        self.assertTrue(row["name"])
        self.assertTrue(row["price_thb"])
        self.assertTrue(row["offers"][0]["price"])
        self.assertTrue(row["offers"][0]["price_checked_at"])
        refs = json.loads((self.catalog / REFERENCES_NAME).read_text(encoding="utf-8"))
        self.assertIn(result["snapshot_id"], refs["snapshot_ids"])
        self.assertTrue((self.jobs / "results" / f"{result['result_id']}.json").is_file())

    def test_recovery_after_result_does_not_repeat_http(self) -> None:
        self._catalog(9)
        event = self._event("manual", ["chat"], "bound")
        enqueue_market_event(event, root=self.jobs, now=NOW)
        (self.jobs / "processing").mkdir(parents=True, exist_ok=True)
        (self.jobs / "pending" / "bound.json").replace(self.jobs / "processing" / "bound.json")
        (self.jobs / "results").mkdir(parents=True, exist_ok=True)
        (self.jobs / "results" / "bound.json").write_text(json.dumps({
            "result_id": "bound", "scan_id": "bound", "snapshot_id": "kept",
            "views": {"general_top": []},
        }), encoding="utf-8")
        scan = {
            "scan_id": "bound", "discovery_key": discovery_key(INSTANCE),
            "claimed_at": (NOW - timedelta(hours=2)).isoformat(),
            "event_ids": ["bound"], "owner_pid": 1,
        }
        (self.jobs / "active_scan.json").write_text(json.dumps(scan), encoding="utf-8")
        self._run()
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self._stored("bound")["result_id"], "bound")

    def test_live_owner_is_not_recovered(self) -> None:
        import os
        event = self._event("manual", ["chat"], "live")
        enqueue_market_event(event, root=self.jobs, now=NOW)
        (self.jobs / "processing").mkdir(parents=True, exist_ok=True)
        (self.jobs / "pending" / "live.json").replace(self.jobs / "processing" / "live.json")
        from thailand.market_events import process_started_at
        scan = {
            "scan_id": "live-scan", "discovery_key": discovery_key(INSTANCE),
            "claimed_at": (NOW - timedelta(hours=5)).isoformat(),
            "event_ids": ["live"], "owner_pid": os.getpid(),
            "owner_started_at": process_started_at(os.getpid()),
        }
        (self.jobs / "active_scan.json").write_text(json.dumps(scan), encoding="utf-8")
        self.assertEqual(recover_market_scan(root=self.jobs, now=NOW, stale_seconds=0), [])
        self.assertTrue((self.jobs / "active_scan.json").is_file())
        self.assertTrue((self.jobs / "processing" / "live.json").is_file())

    def test_reused_pid_does_not_keep_the_claim(self) -> None:
        import os
        event = self._event("manual", ["chat"], "reused")
        enqueue_market_event(event, root=self.jobs, now=NOW)
        (self.jobs / "processing").mkdir(parents=True, exist_ok=True)
        (self.jobs / "pending" / "reused.json").replace(self.jobs / "processing" / "reused.json")
        (self.jobs / "active_scan.json").write_text(json.dumps({
            "scan_id": "reused-scan", "discovery_key": discovery_key(INSTANCE),
            "claimed_at": NOW.isoformat(), "event_ids": ["reused"],
            "owner_pid": os.getpid(), "owner_started_at": "not-this-process",
        }), encoding="utf-8")
        self.assertEqual(recover_market_scan(root=self.jobs, now=NOW, stale_seconds=600), ["reused"])
        self.assertFalse((self.jobs / "active_scan.json").exists())
        self.assertTrue((self.jobs / "pending" / "reused.json").is_file())

    def test_finished_owner_is_recovered(self) -> None:
        if multiprocessing.current_process().name != "MainProcess":
            return
        marker = self.root / "identity.json"
        worker = multiprocessing.Process(target=_publish_identity, args=(str(marker),))
        worker.start()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        identity = json.loads(marker.read_text(encoding="utf-8"))
        worker.terminate()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        event = self._event("manual", ["chat"], "finished-owner")
        enqueue_market_event(event, root=self.jobs, now=NOW)
        (self.jobs / "processing").mkdir(parents=True, exist_ok=True)
        (self.jobs / "pending" / "finished-owner.json").replace(self.jobs / "processing" / "finished-owner.json")
        (self.jobs / "active_scan.json").write_text(json.dumps({
            "scan_id": "finished-scan", "discovery_key": discovery_key(INSTANCE),
            "claimed_at": NOW.isoformat(), "event_ids": ["finished-owner"],
            "owner_pid": identity["pid"], "owner_started_at": identity["started"],
        }), encoding="utf-8")
        self.assertEqual(recover_market_scan(root=self.jobs, now=NOW, stale_seconds=0), ["finished-owner"])
        self.assertTrue((self.jobs / "pending" / "finished-owner.json").is_file())

    def test_reused_delivery_pid_is_not_left_in_delivering(self) -> None:
        import os
        fresh = NOW.isoformat()
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivering").mkdir(parents=True)
        (self.jobs / "results" / "again.json").write_text(json.dumps({
            "result_id": "again", "scan_id": "again", "fx": {**_fx(), "fetched_at": fresh},
            "comparison": {"pairs": [_pair("again-pair", th_availability=fresh, ru_price=fresh, checked=fresh)]},
            "views": {"general_top": ["again-pair"], "all": ["again-pair"], "personal_top3": []},
        }), encoding="utf-8")
        event = self._event("manual", ["chat"], "again-event")
        event["subscription_scan_id"] = "again"
        event["result_id"] = "again"
        event["delivery_owner"] = os.getpid()
        event["delivery_owner_started_at"] = "not-this-process"
        (self.jobs / "delivering" / "again-event.json").write_text(json.dumps(event), encoding="utf-8")
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            sender=self.sender, clock=ManualClock(NOW), deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(self.sender.sent[0]["event_id"], "again-event")
        self.assertFalse((self.jobs / "delivering" / "again-event.json").exists())

    def test_live_delivery_owner_is_not_stolen(self) -> None:
        if multiprocessing.current_process().name != "MainProcess":
            return
        fresh = NOW.isoformat()
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivering").mkdir(parents=True)
        (self.jobs / "results" / "held.json").write_text(json.dumps({
            "result_id": "held", "scan_id": "held", "fx": {**_fx(), "fetched_at": fresh},
            "comparison": {"pairs": [_pair("held-pair", th_availability=fresh, ru_price=fresh, checked=fresh)]},
            "views": {"general_top": ["held-pair"], "all": ["held-pair"], "personal_top3": []},
        }), encoding="utf-8")
        event = self._event("manual", ["chat"], "held-event")
        event["subscription_scan_id"] = "held"
        event["result_id"] = "held"
        (self.jobs / "delivering" / "held-event.json").write_text(json.dumps(event), encoding="utf-8")
        ready = self.root / "delivery-ready"
        release = self.root / "delivery-release"
        worker = multiprocessing.Process(target=_hold_delivering, args=(str(self.jobs), str(ready), str(release)))
        worker.start()
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            sender=self.sender, clock=ManualClock(NOW), deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(self.sender.sent, [])
        self.assertTrue((self.jobs / "delivering" / "held-event.json").is_file())
        release.write_text("go", encoding="utf-8")
        worker.join(5)
        self.assertFalse(worker.is_alive())
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            sender=self.sender, clock=ManualClock(NOW), deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(self.sender.sent[0]["event_id"], "held-event")

    def test_undelivered_buy_is_not_remembered(self) -> None:
        from buy_opportunity import BuySignal

        signal = BuySignal(
            level="BUY", model_key="m", cluster_name="Model", price=100, store="regard",
            gpu="RTX 5070 Ti", cpu=None, ram_gb=32, ssd_gb=1024, screen_inch=16,
            score=1, confidence=1, historical_min=None, target_price=None, url=None,
            reasons=[], rule_ids=[], over_hist_pct=None, fingerprint="undelivered-fp",
            cluster_key=None, history_started_at=None,
        )
        job = build_job(trigger_type="buy_signal", signals=[signal], russian_deals=[], dedupe_key="undelivered-fp", instance_id=INSTANCE)
        enqueue_job(job, root=self.jobs)

        def scan_fn(**_kwargs):
            return {"status": "ok", "stores": [{"store": "jib"}], "messages_sent": 0, "messages_failed": 0}

        process_one(
            jobs_dir=self.jobs, deliver=False, scan_fn=scan_fn,
            state_path=self.root / "buy-state.json",
        )
        record = {}
        path = self.jobs / "delivered_buy.json"
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("undelivered-fp", record.get("fingerprints") or [])

    def test_worker_stores_the_full_comparison(self) -> None:
        from decimal import Decimal
        from market_snapshot import retain_russian_market_snapshots
        from tests.test_thai_catalog_reuse import _russian

        observed = NOW - timedelta(hours=2)
        _publish(self.catalog, [_offer("th-match", "SKU-MATCH17", 80_000, observed)], observed=observed)
        russian = _russian(["model-ru-17"])
        ru_root = self.root / "ru"
        (ru_root / "snapshots").mkdir(parents=True)
        (ru_root / "snapshots" / f"{russian['snapshot_id']}.json").write_text(json.dumps(russian), encoding="utf-8")
        (ru_root / "latest.json").write_text(json.dumps({"snapshot_id": russian["snapshot_id"]}), encoding="utf-8")
        enqueue_market_event(self._event("manual", ["chat"], "full"), root=self.jobs, now=NOW)
        outcome = process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health, fx=_fx(),
            russian_snapshot=russian, russian_snapshot_root=ru_root, transport=self.transport,
            sender=self.sender, clock=ManualClock(NOW), deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(outcome["http_calls"], 0)
        self.assertEqual(self.transport.calls, 0)
        result = json.loads((self.jobs / "results" / f"{outcome['result_id']}.json").read_text(encoding="utf-8"))
        pair_id = result["views"]["general_top"][0]
        pair = next(item for item in result["comparison"]["pairs"] if item["pair_id"] == pair_id)
        self.assertEqual(Decimal(pair["ru_observation"]["price"]), Decimal(250_000))
        self.assertEqual(Decimal(pair["th_observation"]["price"]), Decimal(80_000))
        self.assertIn("specs", pair["ru_observation"])
        self.assertTrue(pair["th_observation"]["url"])
        self.assertIn("confirmed_expenses", pair)
        self.assertIn("commercial_differences", pair)
        self.assertIn("unmatched_th", result["comparison"])
        self.assertIn("source_coverage", result["comparison"])
        self.assertTrue(result["rankings"]["thai_top_value"])
        self.assertEqual(self.sender.sent[0]["current"], True)
        (self.catalog / "snapshots" / "newer-th.json").write_text("{}", encoding="utf-8")
        (self.catalog / "snapshots" / "unreferenced.json").write_text("{}", encoding="utf-8")
        (self.catalog / "latest.json").write_text(json.dumps({"snapshot_id": "newer-th"}), encoding="utf-8")
        (ru_root / "snapshots" / "newer-ru.json").write_text("{}", encoding="utf-8")
        (ru_root / "snapshots" / "unreferenced-ru.json").write_text("{}", encoding="utf-8")
        (ru_root / "latest.json").write_text(json.dumps({"snapshot_id": "newer-ru"}), encoding="utf-8")
        self.assertIn("unreferenced", retain_thai_catalogs(self.catalog))
        self.assertIn("unreferenced-ru", retain_russian_market_snapshots(ru_root))
        self.assertTrue((self.catalog / "snapshots" / f"{result['snapshot_id']}.json").is_file())
        self.assertTrue((ru_root / "snapshots" / f"{result['ru_snapshot_id']}.json").is_file())
        self.assertTrue((self.jobs / "results" / f"{result['result_id']}.json").is_file())

    def test_recovery_keeps_snapshot_references(self) -> None:
        from market_snapshot import retain_russian_market_snapshots

        self._catalog(9)
        th_id = json.loads((self.catalog / "latest.json").read_text(encoding="utf-8"))["snapshot_id"]
        ru_root = self.root / "ru"
        (ru_root / "snapshots").mkdir(parents=True)
        (ru_root / "snapshots" / "ru-kept.json").write_text("{}", encoding="utf-8")
        (ru_root / "latest.json").write_text(json.dumps({"snapshot_id": "ru-kept"}), encoding="utf-8")
        event = self._event("manual", ["chat"], "bound-refs")
        enqueue_market_event(event, root=self.jobs, now=NOW)
        (self.jobs / "processing").mkdir(parents=True, exist_ok=True)
        (self.jobs / "pending" / "bound-refs.json").replace(self.jobs / "processing" / "bound-refs.json")
        (self.jobs / "results").mkdir(parents=True, exist_ok=True)
        (self.jobs / "results" / "bound-refs.json").write_text(json.dumps({
            "result_id": "bound-refs", "scan_id": "bound-refs", "snapshot_id": th_id, "ru_snapshot_id": "ru-kept",
            "views": {"general_top": []},
        }), encoding="utf-8")
        (self.jobs / "active_scan.json").write_text(json.dumps({
            "scan_id": "bound-refs", "discovery_key": discovery_key(INSTANCE),
            "claimed_at": (NOW - timedelta(hours=2)).isoformat(), "event_ids": ["bound-refs"], "owner_pid": 1,
        }), encoding="utf-8")
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            russian_snapshot_root=ru_root, transport=self.transport, sender=None,
            clock=ManualClock(NOW), deliver=False, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(self.transport.calls, 0)
        th_refs = json.loads((self.catalog / REFERENCES_NAME).read_text(encoding="utf-8"))
        ru_refs = json.loads((ru_root / REFERENCES_NAME).read_text(encoding="utf-8"))
        self.assertIn(th_id, th_refs["snapshot_ids"])
        self.assertIn("ru-kept", ru_refs["snapshot_ids"])
        (self.catalog / "snapshots" / "newer-th.json").write_text("{}", encoding="utf-8")
        (self.catalog / "snapshots" / "drop-th.json").write_text("{}", encoding="utf-8")
        (self.catalog / "latest.json").write_text(json.dumps({"snapshot_id": "newer-th"}), encoding="utf-8")
        (ru_root / "snapshots" / "drop-ru.json").write_text("{}", encoding="utf-8")
        (ru_root / "latest.json").write_text(json.dumps({"snapshot_id": "newer-ru"}), encoding="utf-8")
        (ru_root / "snapshots" / "newer-ru.json").write_text("{}", encoding="utf-8")
        self.assertIn("drop-th", retain_thai_catalogs(self.catalog))
        self.assertIn("drop-ru", retain_russian_market_snapshots(ru_root))
        self.assertTrue((self.catalog / "snapshots" / f"{th_id}.json").is_file())
        self.assertTrue((ru_root / "snapshots" / "ru-kept.json").is_file())
        self.assertTrue((self.jobs / "results" / "bound-refs.json").is_file())

    def test_default_jobs_dir_serves_v2_and_releases_v1(self) -> None:
        import config
        from unittest.mock import patch
        from thailand.job_queue import jobs_root

        self._catalog(2)
        with patch.object(config, "THAILAND_JOBS_DIR", str(self.jobs)):
            enqueue_market_event(self._event("manual", ["chat"], "default-v2"), root=jobs_root(), now=NOW)
            outcome = process_one(
                catalog_root=self.catalog, health_path=self.health, fx=_fx(), transport=self.transport,
                sender=self.sender, clock=ManualClock(NOW), deliver=True, state_path=self.root / "buy-state.json",
            )
            self.assertEqual(outcome["http_calls"], 0)
            self.assertTrue((self.jobs / "archive" / "default-v2.json").is_file())
            calls = {"n": 0}

            def scan_fn(**_kwargs):
                calls["n"] += 1
                return {"status": "ok", "stores": [{"store": "jib"}], "messages_sent": 0}

            for index in (1, 2):
                job = build_job(
                    trigger_type="manual_check", signals=[], russian_deals=[],
                    dedupe_key=f"v1-{index}", instance_id=INSTANCE,
                )
                enqueue_job(job, root=jobs_root())
                finished = process_one(scan_fn=scan_fn, deliver=False, state_path=self.root / "buy-state.json")
                self.assertEqual(finished["status"], "ok")
                self.assertFalse((jobs_root() / "active_scan.json").exists())
            self.assertEqual(calls["n"], 2)

    def test_second_send_uses_the_later_clock(self) -> None:
        fresh = NOW.isoformat()

        class _Clock:
            def __init__(self) -> None:
                self.current = NOW

            def now(self) -> datetime:
                return self.current

        class _Sender:
            def __init__(self, clock: _Clock) -> None:
                self.clock = clock
                self.sent: list[dict] = []

            def send(self, event, result, *, current: bool, historical: bool, info_current: bool = False) -> None:
                del result
                self.sent.append({"event_id": event["event_id"], "current": current, "info_current": info_current})
                self.clock.current = NOW + timedelta(hours=9)

        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        pair = _pair("moving", th_availability=fresh, ru_price=fresh, checked=fresh)
        (self.jobs / "results" / "moving.json").write_text(json.dumps({
            "result_id": "moving", "scan_id": "moving", "fx": {**_fx(), "fetched_at": fresh},
            "comparison": {"pairs": [pair]},
            "views": {"general_top": ["moving"], "all": ["moving"], "personal_top3": []},
        }), encoding="utf-8")
        for event_id in ("send-1", "send-2"):
            event = self._event("manual", ["chat"], event_id)
            event["subscription_scan_id"] = "moving"
            event["result_id"] = "moving"
            (self.jobs / "delivery" / f"{event_id}.json").write_text(json.dumps(event), encoding="utf-8")
        sender = _Sender(_Clock())
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            sender=sender, clock=sender.clock, deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual([row["current"] for row in sender.sent], [True, False])
        self.assertEqual(sender.sent[1]["info_current"], False)

    def test_missing_fx_is_not_a_current_purchase(self) -> None:
        fresh = NOW.isoformat()
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        pair = _pair("no-fx", th_availability=fresh, ru_price=fresh, checked=fresh)
        (self.jobs / "results" / "no-fx.json").write_text(json.dumps({
            "result_id": "no-fx", "scan_id": "no-fx", "fx": {},
            "comparison": {"pairs": [pair]},
            "views": {"general_top": ["no-fx"], "all": ["no-fx"], "personal_top3": []},
        }), encoding="utf-8")
        event = self._event("manual", ["chat"], "no-fx-event")
        event["subscription_scan_id"] = "no-fx"
        event["result_id"] = "no-fx"
        (self.jobs / "delivery" / "no-fx-event.json").write_text(json.dumps(event), encoding="utf-8")
        process_one(
            jobs_dir=self.jobs, catalog_root=self.catalog, health_path=self.health,
            sender=self.sender, clock=ManualClock(NOW), deliver=True, state_path=self.root / "buy-state.json",
        )
        self.assertEqual(self.sender.sent[0]["current"], False)
        self.assertEqual(self.sender.sent[0]["info_current"], True)


if __name__ == "__main__":
    unittest.main()
