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


class FakeSender:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[dict] = []

    def send(self, event, result, *, current: bool, historical: bool) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("delivery")
        self.sent.append({
            "event_id": event["event_id"],
            "recipients": list(event["recipients"]),
            "current": current,
            "historical": historical,
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
        self.assertEqual(self.transport.calls, 0)
        retried = self._run()
        self.assertEqual(retried["http_calls"], 0)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.sender.sent[0]["event_id"], "deliver-1")

    def test_stale_purchase_is_not_sent_as_current(self) -> None:
        self.jobs.mkdir(exist_ok=True)
        (self.jobs / "results").mkdir(parents=True)
        (self.jobs / "delivery").mkdir(parents=True)
        result = {
            "result_id": "scan-old", "scan_id": "scan-old", "snapshot_id": "snap-old",
            "quote_checked_at": (NOW - timedelta(hours=9)).isoformat(),
            "purchase_current": True, "http_calls": 0, "generated_at": (NOW - timedelta(hours=9)).isoformat(),
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


if __name__ == "__main__":
    unittest.main()
