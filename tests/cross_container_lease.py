"""Entry point for two containers that share a queue volume. Not a unit test."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path("/queue")


def _event(event_id: str):
    import config
    from thailand.market_events import build_market_event, enqueue_market_event

    event = build_market_event(
        instance_id=config.get_instance_id(),
        trigger_type="manual",
        requested_view="general_top",
        recipients=["42"],
        event_id=event_id,
    )
    return enqueue_market_event(event, root=ROOT)


def hold() -> None:
    from thailand.market_events import VolumeLease

    stored = _event("held")
    lease = VolumeLease(ROOT / "scan.lease")
    delivery = VolumeLease(ROOT / "delivery.lease")
    if not lease.try_acquire() or not delivery.try_acquire():
        raise SystemExit("lease was not free at start")
    scan = {
        "scan_id": "live-scan",
        "discovery_key": json.loads((ROOT / "pending" / "held.json").read_text(encoding="utf-8")).get("discovery_key")
        if (ROOT / "pending" / "held.json").is_file() else None,
    }
    event_path = next(ROOT.rglob("held.json"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    scan["discovery_key"] = event["discovery_key"]
    scan["event_ids"] = ["held"]
    scan["claimed_at"] = event["created_at"]
    (ROOT / "processing").mkdir(parents=True, exist_ok=True)
    (ROOT / "processing" / "held.json").write_text(json.dumps(event), encoding="utf-8")
    if event_path.resolve() != (ROOT / "processing" / "held.json").resolve():
        event_path.unlink(missing_ok=True)
    (ROOT / "active_scan.json").write_text(json.dumps(scan), encoding="utf-8")
    (ROOT / "ready").write_text(stored.get("status") or "ready", encoding="utf-8")
    deadline = time.time() + 90
    while time.time() < deadline and not (ROOT / "release").exists():
        time.sleep(0.1)
    lease.release()
    delivery.release()


def second() -> None:
    from unittest.mock import patch
    from thailand.worker import process_one

    class NeverSend:
        def send(self, *args, **kwargs):
            raise AssertionError("second worker must not deliver")

    _event("joined")
    with patch("thailand.market_events._run_plan", side_effect=AssertionError("second scan")):
        process_one(jobs_dir=ROOT, deliver=True, sender=NeverSend())
    joined = None
    for folder in ("pending", "processing", "deferred", "delivery", "archive"):
        path = ROOT / folder / "joined.json"
        if path.is_file():
            joined = folder
    scan = json.loads((ROOT / "active_scan.json").read_text(encoding="utf-8"))
    report = {
        "joined_folder": joined,
        "scan_id": scan.get("scan_id"),
        "event_ids": scan.get("event_ids"),
        "results": [path.name for path in (ROOT / "results").glob("*.json")] if (ROOT / "results").is_dir() else [],
    }
    (ROOT / "second.json").write_text(json.dumps(report), encoding="utf-8")


def recover() -> None:
    from thailand.market_events import recover_market_scan
    from datetime import datetime, timezone

    recovered = recover_market_scan(root=ROOT, now=datetime.now(timezone.utc), stale_seconds=0)
    (ROOT / "recovered.json").write_text(json.dumps({"recovered": recovered}), encoding="utf-8")


def finish() -> None:
    from unittest.mock import patch
    from thailand.market_events import run_market_queue

    class Sender:
        def __init__(self):
            self.sent = []

        def send(self, event, result, **kwargs):
            self.sent.append({"recipients": event["recipients"], "view": event["requested_view"]})

    sender = Sender()
    plan = ({"action": "REUSE"}, {"status": "completed", "http_calls": 0}, 1)
    with patch("thailand.market_events._run_plan", return_value=plan) as mocked:
        first = run_market_queue(jobs_dir=ROOT, catalog_root=ROOT / "catalog", sender=sender)
        again = run_market_queue(jobs_dir=ROOT, catalog_root=ROOT / "catalog", sender=sender)
    report = {"passes": mocked.call_count, "first": first, "again": again, "sent": sender.sent,
              "archived": sorted(p.stem for p in (ROOT / "archive").glob("*.json"))}
    (ROOT / "finished.json").write_text(json.dumps(report), encoding="utf-8")


def hold_delivery() -> None:
    import config
    from thailand.market_events import VolumeLease, build_market_event

    lease = VolumeLease(ROOT / "delivery.lease")
    if not lease.try_acquire():
        raise SystemExit("delivery lease busy")
    event = build_market_event(instance_id=config.get_instance_id(), trigger_type="manual",
        requested_view="thai_top", recipients=["42"], event_id="sending")
    event.update(subscription_scan_id="sending", result_id="sending")
    (ROOT / "delivering" / "sending.json").write_text(json.dumps(event))
    (ROOT / "results" / "sending.json").write_text(json.dumps({"result_id": "sending"}))
    (ROOT / "delivery-ready").write_text("ready")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(0.1)
    lease.release()


def probe_delivery() -> None:
    from thailand.market_events import _retry_delivery

    class NeverSend:
        def send(self, *args, **kwargs):
            raise AssertionError("live delivery was stolen")

    result = _retry_delivery(ROOT, NeverSend())
    assert result is None
    assert (ROOT / "delivering" / "sending.json").is_file()


def recover_delivery() -> None:
    from thailand.market_events import _retry_delivery

    class Sender:
        def __init__(self):
            self.count = 0

        def send(self, *args, **kwargs):
            self.count += 1

    sender = Sender()
    first = _retry_delivery(ROOT, sender)
    second = _retry_delivery(ROOT, sender)
    assert first["status"] == "delivered"
    assert second is None
    assert sender.count == 1
    assert (ROOT / "archive" / "sending.json").is_file()
    (ROOT / "delivery-recovered.json").write_text(json.dumps({"sent": sender.count, "idle": second}))


if __name__ == "__main__":
    {"hold": hold, "second": second, "recover": recover, "finish": finish,
     "hold-delivery": hold_delivery, "probe-delivery": probe_delivery,
     "recover-delivery": recover_delivery}[sys.argv[1]]()
