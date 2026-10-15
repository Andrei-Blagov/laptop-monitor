"""Schema-2 market events on the existing file queue.

Deferred events stay out of pending/. One heavy Thai pass serves every
compatible subscriber. Delivery retries do not call stores.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from market_contracts import ComparisonPolicy, FxObservation, age_valid, moment
from thai_catalog import REFERENCES_NAME, discovery_scope_hash
from thailand.catalog_refresh import execute_catalog_plan
from thailand.catalog_reuse import evaluate_catalog_reuse
from thailand.job_queue import _atomic_write, _read_json, ensure_queue_dirs, jobs_root

MOSCOW = ZoneInfo("Europe/Moscow")
MARKET = "TH"
SCHEMA = 2
TRIGGERS = frozenset({"daily", "manual", "buy", "personal"})
SEPARATE_REASONS = frozenset({
    "deadline", "budget", "stopped_after_429", "retry_after",
    "source_disabled", "circuit", "policy_disabled", "product_absent",
    "price_and_availability_unconfirmed",
})


class FileLock:
    """Exclusive lock shared by enqueue, subscribe, and claim."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        if self._fh.seek(0, os.SEEK_END) == 0:
            self._fh.write(b"\0")
            self._fh.flush()
        self._fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args) -> None:
        if self._fh is None:
            return
        if os.name == "nt":
            import msvcrt
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


def discovery_key(instance_id: str, scope_hash: str | None = None) -> str:
    return f"{instance_id}|{MARKET}|{scope_hash or discovery_scope_hash()}"


def next_daily_not_before(now: datetime) -> datetime | None:
    """08:00 Europe/Moscow. None when that instant has already arrived today."""
    local = now.astimezone(MOSCOW)
    due = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local >= due:
        return None
    return due.astimezone(timezone.utc)


def build_market_event(
    *,
    instance_id: str,
    trigger_type: str,
    requested_view: str,
    recipients: list[str],
    event_id: str | None = None,
    now: datetime | None = None,
    source_ru_snapshot_id: str | None = None,
    not_before: datetime | None = None,
    scope_hash: str | None = None,
) -> dict[str, Any]:
    if trigger_type not in TRIGGERS:
        raise ValueError(f"unknown trigger_type: {trigger_type}")
    moment_now = now or datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA,
        "event_id": event_id or str(uuid.uuid4()),
        "instance_id": instance_id,
        "created_at": moment_now.isoformat(),
        "trigger_type": trigger_type,
        "requested_view": requested_view,
        "recipients": list(recipients),
        "source_ru_snapshot_id": source_ru_snapshot_id,
        "requested_ru_snapshot_id": None,
        "subscription_scan_id": None,
        "result_comparison_id": None,
        "result_id": None,
        "delivery_state": "pending",
        "not_before": not_before.isoformat() if not_before else None,
        "retry_count": 0,
        "discovery_key": discovery_key(instance_id, scope_hash),
        "market": MARKET,
    }


def _lock(root: Path) -> FileLock:
    return FileLock(root / "queue.lock")


def _dirs(root: Path) -> None:
    ensure_queue_dirs(root)
    for name in ("deferred", "results", "delivery", "delivering"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _event_path(root: Path, event_id: str) -> Path | None:
    for name in ("pending", "processing", "deferred", "delivery", "delivering", "archive", "failed"):
        path = root / name / f"{event_id}.json"
        if path.is_file():
            return path
    return None


def _load_events(directory: Path) -> list[dict[str, Any]]:
    found = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, dict) and int(data.get("schema_version") or 0) >= 2:
            found.append(data)
    return found


def _write_event(root: Path, folder: str, event: dict[str, Any]) -> None:
    _atomic_write(root / folder / f"{event['event_id']}.json", event)


def _remove_event(root: Path, event_id: str, keep: str | None = None) -> None:
    for name in ("pending", "processing", "deferred", "delivery", "delivering", "archive", "failed"):
        if name == keep:
            continue
        path = root / name / f"{event_id}.json"
        if path.is_file():
            path.unlink()


def enqueue_market_event(event: Mapping[str, Any], *, root: Path | str, now: datetime | None = None) -> dict[str, Any]:
    """Store one event. The same event_id never creates a second file."""
    base = jobs_root(root)
    _dirs(base)
    clock = now or datetime.now(timezone.utc)
    body = dict(event)
    with _lock(base):
        existing = _event_path(base, str(body["event_id"]))
        if existing is not None:
            return {"ok": True, "duplicate": True, "event_id": body["event_id"], "path": str(existing)}
        scan = _active_scan(base)
        due = moment(body.get("not_before"))
        if scan and scan.get("discovery_key") == body.get("discovery_key") and (due is None or due <= clock):
            _subscribe(base, scan, body)
            return {"ok": True, "duplicate": False, "event_id": body["event_id"], "status": "subscribed", "scan_id": scan["scan_id"]}
        folder = "deferred" if due is not None and due > clock else "pending"
        if folder == "pending":
            body["delivery_state"] = "pending"
        else:
            body["delivery_state"] = "deferred"
        _write_event(base, folder, body)
    return {"ok": True, "duplicate": False, "event_id": body["event_id"], "status": body["delivery_state"]}


def promote_due_events(*, root: Path | str, now: datetime) -> list[str]:
    """Move due deferred events into pending once. No sleep and no store calls."""
    base = jobs_root(root)
    _dirs(base)
    moved = []
    with _lock(base):
        for event in _load_events(base / "deferred"):
            due = moment(event.get("not_before"))
            if due is None or due > now:
                continue
            folder = "delivery" if event.get("result_id") else "pending"
            event["delivery_state"] = "pending"
            _write_event(base, folder, event)
            (base / "deferred" / f"{event['event_id']}.json").unlink(missing_ok=True)
            moved.append(event["event_id"])
    return moved


def _pid_alive(pid: object) -> bool:
    """True when the process exists. Does not signal or terminate it."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, number)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(number, 0)
    except OSError:
        return False
    return True


def _scan_live(scan: Mapping[str, Any] | None, now: datetime, stale_seconds: float) -> bool:
    if not scan:
        return False
    if _pid_alive(scan.get("owner_pid")):
        return True
    claimed = moment(scan.get("claimed_at"))
    if claimed is None:
        return True
    return (now - claimed).total_seconds() < stale_seconds


def _active_scan(root: Path) -> dict[str, Any] | None:
    return _read_json(root / "active_scan.json")


def _save_scan(root: Path, scan: dict[str, Any] | None, *, now: datetime | None = None, stale_seconds: float = 600) -> bool:
    path = root / "active_scan.json"
    current = _active_scan(root)
    if scan is not None and current and current.get("scan_id") != scan.get("scan_id"):
        if _scan_live(current, now or datetime.now(timezone.utc), stale_seconds):
            return False
    if scan is None:
        if current and _pid_alive(current.get("owner_pid")) and current.get("owner_pid") != os.getpid():
            return False
        if path.is_file():
            path.unlink()
        return True
    _atomic_write(path, scan)
    return True


def recover_market_scan(*, root: Path | str, now: datetime, stale_seconds: float = 0) -> list[str]:
    """Return a crashed claim to pending when it has no stored result."""
    base = jobs_root(root)
    _dirs(base)
    recovered = []
    with _lock(base):
        scan = _active_scan(base)
        if not scan:
            for event in _load_events(base / "processing"):
                stored = _load_result(base, str(event.get("subscription_scan_id") or event.get("result_id") or ""))
                if event.get("result_id") or stored:
                    if stored:
                        _bind_event(event, stored, str(event.get("subscription_scan_id") or stored.get("scan_id") or ""))
                    _write_event(base, "delivery", event)
                    _remove_event(base, event["event_id"], keep="delivery")
                    continue
                event["delivery_state"] = "pending"
                event["subscription_scan_id"] = None
                _write_event(base, "pending", event)
                _remove_event(base, event["event_id"], keep="pending")
                recovered.append(event["event_id"])
            return recovered
        if _scan_live(scan, now, stale_seconds):
            return recovered
        result = base / "results" / f"{scan['scan_id']}.json"
        for event_id in list(scan.get("event_ids") or []):
            path = _event_path(base, event_id)
            event = _read_json(path) if path else None
            if not isinstance(event, dict):
                continue
            stored = _read_json(result) if result.is_file() else None
            if result.is_file() or event.get("result_id"):
                if isinstance(stored, dict):
                    _bind_event(event, stored, str(scan["scan_id"]))
                _write_event(base, "delivery", event)
                _remove_event(base, event_id, keep="delivery")
            else:
                event["delivery_state"] = "pending"
                event["subscription_scan_id"] = None
                _write_event(base, "pending", event)
                _remove_event(base, event_id, keep="pending")
                recovered.append(event_id)
        _save_scan(base, None)
    return recovered


def _subscribe(base: Path, scan: dict[str, Any], event: dict[str, Any]) -> None:
    event["subscription_scan_id"] = scan["scan_id"]
    event["delivery_state"] = "pending"
    ids = list(scan.get("event_ids") or [])
    if event["event_id"] not in ids:
        ids.append(event["event_id"])
    scan["event_ids"] = ids
    _write_event(base, "processing", event)
    _remove_event(base, event["event_id"], keep="processing")
    _save_scan(base, scan)


def _claim_batch(base: Path, now: datetime) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    pending = _load_events(base / "pending")
    pending.sort(key=lambda item: item.get("created_at") or "")
    if not pending:
        return None, []
    key = pending[0]["discovery_key"]
    scan = _active_scan(base)
    if _scan_live(scan, now, 600):
        if scan and scan.get("discovery_key") == key:
            for event in pending:
                if event.get("discovery_key") == key:
                    _subscribe(base, scan, event)
        return None, []
    chosen = [event for event in pending if event.get("discovery_key") == key]
    scan = {
        "scan_id": str(uuid.uuid4()),
        "discovery_key": key,
        "claimed_at": now.isoformat(),
        "event_ids": [event["event_id"] for event in chosen],
        "owner_pid": os.getpid(),
    }
    if not _save_scan(base, scan, now=now):
        return None, []
    for event in chosen:
        event["subscription_scan_id"] = scan["scan_id"]
        _write_event(base, "processing", event)
        _remove_event(base, event["event_id"], keep="processing")
    return scan, chosen


def _defer_events(base: Path, events: list[dict[str, Any]], when: datetime, reason: str) -> None:
    for event in events:
        event["delivery_state"] = "deferred"
        event["not_before"] = when.isoformat()
        event["defer_reason"] = reason
        _write_event(base, "deferred", event)
        _remove_event(base, event["event_id"], keep="deferred")


def _protect(catalog_root: Path, snapshot_ids: list[str]) -> None:
    path = catalog_root / REFERENCES_NAME
    current = _read_json(path) or {}
    found = [str(item) for item in current.get("snapshot_ids") or [] if item]
    for snapshot_id in snapshot_ids:
        if snapshot_id and snapshot_id not in found:
            found.append(snapshot_id)
    _atomic_write(path, {"snapshot_ids": found})


def _offer_row(offer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": offer.get("product_name") or offer.get("name"),
        "source": offer.get("source"),
        "external_id": offer.get("external_id"),
        "price_thb": offer.get("price_thb") if offer.get("price_thb") is not None else offer.get("price"),
        "price_checked_at": offer.get("price_checked_at"),
        "availability_checked_at": offer.get("availability_checked_at"),
        "availability_state": offer.get("availability_state"),
    }


def _message_views(outcome: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Rows a message can render without another store call."""
    comparison = outcome.get("comparison") or {}
    rankings = outcome.get("rankings") or {}
    views: dict[str, list[dict[str, Any]]] = {}
    pairs = list(comparison.get("pairs") or [])
    for name in ("all", "general_top", "personal_top3"):
        if name == "all":
            chosen = pairs
        elif name == "general_top":
            chosen = [pair for pair in pairs if (pair.get("ru_membership") or {}).get("in_general_top")]
        else:
            chosen = [pair for pair in pairs if (pair.get("ru_membership") or {}).get("in_personal_top3")]
        rows = []
        for pair in chosen:
            th = pair.get("th_observation") or {}
            ru = pair.get("ru_observation") or {}
            rows.append({
                **_offer_row(th),
                "ru_price_rub": ru.get("price_rub"),
                "ru_price_checked_at": ru.get("price_checked_at"),
                "ru_availability_checked_at": ru.get("availability_checked_at"),
                "saving_rub": pair.get("saving_rub"),
                "differences": list(pair.get("conflicting_fields") or pair.get("commercial_differences") or []),
                "comparison_status": pair.get("comparison_status"),
            })
        views[name] = rows
    if not views["general_top"]:
        for model in rankings.get("thai_top_value") or []:
            for offer in model.get("offers") or [model]:
                if isinstance(offer, dict):
                    views["general_top"].append(_offer_row(offer))
    return views


def _fx_usable(fx: Mapping[str, Any], now: datetime, policy: ComparisonPolicy) -> bool:
    published = fx.get("published_date")
    try:
        published_date = date.fromisoformat(str(published)) if published else None
    except ValueError:
        published_date = None
    rate = fx.get("rub_per_thb")
    nominal = fx.get("nominal", 1)
    observation = FxObservation(
        fx_id=str(fx.get("fx_id") or ""),
        rub_per_thb=Decimal(str(rate)) if rate not in (None, "") else None,
        fetched_at=moment(fx.get("fetched_at")),
        published_date=published_date,
        source=str(fx.get("source") or "CBR"),
        currency=str(fx.get("currency") or "THB"),
        nominal=nominal if isinstance(nominal, int) and not isinstance(nominal, bool) else 0,
        error=fx.get("error"),
    )
    return observation.usable(now, policy)


def _view_is_current(result: Mapping[str, Any], view: str, now: datetime) -> bool:
    """Current only when every shown offer, its RU side, and FX pass package D at delivery."""
    rows = (result.get("views") or {}).get(view) or []
    if not rows:
        return False
    policy = ComparisonPolicy()
    fx = result.get("fx") or {}
    if fx and not _fx_usable(fx, now, policy):
        return False
    for row in rows:
        if not age_valid(moment(row.get("price_checked_at")), now, policy.quote_hours):
            return False
        if not age_valid(moment(row.get("availability_checked_at")), now, policy.quote_hours):
            return False
        if row.get("ru_price_checked_at") is not None and not age_valid(
            moment(row.get("ru_price_checked_at")), now, policy.ru_quote_hours
        ):
            return False
        if row.get("ru_availability_checked_at") is not None and not age_valid(
            moment(row.get("ru_availability_checked_at")), now, policy.ru_quote_hours
        ):
            return False
    return True


def _deliver_one(sender, event: dict[str, Any], result: Mapping[str, Any], now: datetime) -> str:
    current = _view_is_current(result, str(event.get("requested_view") or "general_top"), now)
    if sender is None:
        return "skipped"
    sender.send(event, result, current=current, historical=not current)
    return "sent" if current else "historical"


def _load_result(base: Path, scan_id: str) -> dict[str, Any] | None:
    data = _read_json(base / "results" / f"{scan_id}.json")
    return data if isinstance(data, dict) else None


def _bind_event(event: dict[str, Any], result: Mapping[str, Any], scan_id: str) -> None:
    event["subscription_scan_id"] = scan_id
    event["result_id"] = result.get("result_id")
    event["result_comparison_id"] = result.get("result_comparison_id")
    event["requested_ru_snapshot_id"] = result.get("ru_snapshot_id")
    event["result_snapshot_id"] = result.get("snapshot_id")
    event["fx_id"] = (result.get("fx") or {}).get("fx_id")


def _stamp(base: Path, events: list[dict[str, Any]], result: Mapping[str, Any], scan_id: str) -> None:
    for event in events:
        _bind_event(event, result, scan_id)
        _write_event(base, "delivery", event)
        _remove_event(base, event["event_id"], keep="delivery")


def _run_plan(events, *, catalog_root, health_path, fx, russian_snapshot, transport, clock, now):
    trigger = "daily" if any(event.get("trigger_type") == "daily" for event in events) else "manual"
    decision = evaluate_catalog_reuse(
        catalog_root, now=now, instance_id=events[0]["instance_id"], trigger=trigger,
        russian_snapshot=russian_snapshot, fx=fx, health_path=health_path,
    )
    passes = 0
    outcome = None
    while passes < 2:
        outcome = execute_catalog_plan(
            decision, snapshot_root=catalog_root, now=now, instance_id=events[0]["instance_id"],
            health_path=health_path, russian_snapshot=russian_snapshot, fx=fx,
            transport=transport, clock=clock,
        )
        passes += 1
        if "snapshot_changed" not in (outcome.get("errors") or []) or not outcome.get("replanned_decision"):
            break
        decision = outcome["replanned_decision"]
    return decision, outcome, passes


def _protect_refs(base: Path, result: Mapping[str, Any]) -> None:
    path = base / "protected_refs.json"
    current = _read_json(path) or {}
    refs = {
        "th_snapshot_ids": list(current.get("th_snapshot_ids") or []),
        "ru_snapshot_ids": list(current.get("ru_snapshot_ids") or []),
        "fx_ids": list(current.get("fx_ids") or []),
        "result_ids": list(current.get("result_ids") or []),
    }
    additions = {
        "th_snapshot_ids": result.get("snapshot_id"),
        "ru_snapshot_ids": result.get("ru_snapshot_id"),
        "fx_ids": (result.get("fx") or {}).get("fx_id"),
        "result_ids": result.get("result_id"),
    }
    for key, value in additions.items():
        if value and value not in refs[key]:
            refs[key].append(value)
    _atomic_write(path, refs)


def _finish_scan(base, catalog_root, scan, events, outcome, now, fx=None) -> dict[str, Any]:
    comparison = outcome.get("comparison") or {}
    supplied = fx if isinstance(fx, dict) else {}
    result = {
        "result_id": scan["scan_id"],
        "scan_id": scan["scan_id"],
        "snapshot_id": outcome.get("snapshot_id"),
        "result_comparison_id": comparison.get("comparison_id"),
        "ru_snapshot_id": comparison.get("ru_snapshot_id"),
        "fx": {
            "fx_id": supplied.get("fx_id") or comparison.get("fx_id"),
            "fetched_at": supplied.get("fetched_at"),
            "published_date": supplied.get("published_date"),
            "rub_per_thb": supplied.get("rub_per_thb"),
            "source": supplied.get("source"),
            "currency": supplied.get("currency"),
            "nominal": supplied.get("nominal"),
            "error": supplied.get("error"),
        } if supplied or comparison.get("fx_id") else {},
        "http_calls": int(outcome.get("http_calls") or 0),
        "action": outcome.get("action"),
        "status": outcome.get("status"),
        "generated_at": now.isoformat(),
        "passes": outcome.get("passes"),
        "views": _message_views(outcome),
    }
    _atomic_write(base / "results" / f"{scan['scan_id']}.json", result)
    _protect_refs(base, result)
    if catalog_root is not None and result.get("snapshot_id"):
        _protect(Path(catalog_root), [str(result["snapshot_id"])])
    with _lock(base):
        current = _active_scan(base) or scan
        ids = list(dict.fromkeys([*scan.get("event_ids", []), *current.get("event_ids", [])]))
        attached = []
        for event_id in ids:
            path = _event_path(base, event_id)
            event = _read_json(path) if path else None
            if isinstance(event, dict):
                attached.append(event)
        _stamp(base, attached, result, scan["scan_id"])
        _save_scan(base, None)
    return result


def run_market_queue(
    *,
    jobs_dir: Path | str | None,
    catalog_root: Path | str | None = None,
    health_path: Path | str | None = None,
    fx: Mapping[str, Any] | None = None,
    russian_snapshot: Mapping[str, Any] | None = None,
    transport: Any = None,
    sender: Any = None,
    clock: Any = None,
    deliver: bool = True,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """One worker wake: promote due events, run at most one pass, deliver stored results."""
    if jobs_dir is None:
        return None
    base = jobs_root(jobs_dir)
    if not base.exists():
        return None
    _dirs(base)
    clock_now = now or (clock.now() if clock is not None else datetime.now(timezone.utc))
    import config
    recover_market_scan(
        root=base, now=clock_now,
        stale_seconds=float(config.THAILAND_JOB_STALE_PROCESSING_SECONDS),
    )
    promote_due_events(root=base, now=clock_now)
    delivery_now = clock.now() if clock is not None else (datetime.now(timezone.utc) if now is None else clock_now)
    delivered = _retry_delivery(base, sender, delivery_now, deliver=deliver)
    if delivered is not None:
        return delivered
    with _lock(base):
        scan, events = _claim_batch(base, clock_now)
    if scan is None:
        return None
    if catalog_root is None:
        _defer_events(base, events, clock_now + timedelta(hours=1), "catalog_root_missing")
        with _lock(base):
            _save_scan(base, None)
        return {"status": "deferred", "event_ids": [event["event_id"] for event in events], "http_calls": 0}
    decision, outcome, passes = _run_plan(
        events, catalog_root=catalog_root, health_path=health_path, fx=fx,
        russian_snapshot=russian_snapshot, transport=transport, clock=clock, now=clock_now,
    )
    outcome = dict(outcome)
    outcome["passes"] = passes
    if decision.get("action") == "DEFERRED" or outcome.get("status") == "deferred":
        when = moment(outcome.get("next_allowed_at")) or (clock_now + timedelta(minutes=30))
        with _lock(base):
            current = _active_scan(base) or scan
            attached = []
            for event_id in current.get("event_ids") or []:
                path = _event_path(base, event_id)
                event = _read_json(path) if path else None
                if isinstance(event, dict):
                    attached.append(event)
            _defer_events(base, attached or events, when, decision.get("action") or "deferred")
            _save_scan(base, None)
        return {"status": "deferred", "event_ids": [event["event_id"] for event in events], "http_calls": int(outcome.get("http_calls") or 0)}
    result = _finish_scan(base, Path(catalog_root), scan, events, outcome, clock_now, fx=fx)
    if deliver and sender is not None:
        delivery_now = clock.now() if clock is not None else (datetime.now(timezone.utc) if now is None else clock_now)
        delivered = _retry_delivery(base, sender, delivery_now, deliver=True)
        if delivered is not None and delivered.get("status") == "delivery_failed":
            delivered["http_calls"] = result["http_calls"]
            delivered["result_id"] = result["result_id"]
            return delivered
    return {"status": result.get("status") or "completed", "event_ids": scan["event_ids"], "http_calls": result["http_calls"], "result_id": result["result_id"]}


def _claim_delivery(base: Path, event_id: str) -> dict[str, Any] | None:
    source = base / "delivery" / f"{event_id}.json"
    destination = base / "delivering" / f"{event_id}.json"
    try:
        os.replace(source, destination)
    except OSError:
        return None
    event = _read_json(destination)
    if not isinstance(event, dict):
        return None
    event["delivery_owner"] = os.getpid()
    _atomic_write(destination, event)
    return event


def _retry_delivery(base: Path, sender, now: datetime, *, deliver: bool = True) -> dict[str, Any] | None:
    """Claim one waiting event. A failed send waits until not_before; it is not retried in this drain."""
    if not deliver or sender is None:
        return None
    (base / "delivering").mkdir(parents=True, exist_ok=True)
    with _lock(base):
        for path in sorted((base / "delivering").glob("*.json")):
            event = _read_json(path)
            if not isinstance(event, dict):
                continue
            owner = event.get("delivery_owner")
            if _pid_alive(owner) and owner != os.getpid():
                continue
            _write_event(base, "delivery", event)
            path.unlink(missing_ok=True)
    waiting = _load_events(base / "delivery")
    if not waiting:
        return None
    sent_ids = []
    for event in waiting:
        result = _load_result(base, str(event.get("subscription_scan_id") or event.get("result_id") or ""))
        if result is None:
            continue
        with _lock(base):
            claimed = _claim_delivery(base, str(event["event_id"]))
        if claimed is None:
            continue
        try:
            state = _deliver_one(sender, claimed, result, now)
        except Exception as exc:  # noqa: BLE001
            claimed["delivery_state"] = "deferred"
            claimed["retry_count"] = int(claimed.get("retry_count") or 0) + 1
            claimed["delivery_error"] = type(exc).__name__
            claimed["not_before"] = (now + timedelta(minutes=30)).isoformat()
            claimed.pop("delivery_owner", None)
            _write_event(base, "deferred", claimed)
            (base / "delivering" / f"{claimed['event_id']}.json").unlink(missing_ok=True)
            return {"status": "delivery_failed", "event_id": claimed["event_id"], "http_calls": 0}
        claimed["delivery_state"] = "sent"
        claimed["delivered_as"] = state
        claimed.pop("delivery_owner", None)
        _write_event(base, "archive", claimed)
        (base / "delivering" / f"{claimed['event_id']}.json").unlink(missing_ok=True)
        _remove_event(base, claimed["event_id"], keep="archive")
        sent_ids.append(claimed["event_id"])
    if not sent_ids:
        return None
    return {"status": "delivered", "event_ids": sent_ids, "http_calls": 0}


def heavy_scan_busy(root: Path | str | None, now: datetime, stale_seconds: float = 600) -> bool:
    if root is None:
        return False
    base = jobs_root(root)
    return _scan_live(_active_scan(base), now, stale_seconds)


def begin_heavy_scan(root: Path | str, now: datetime, *, kind: str) -> bool:
    """Take the single heavy-pass lease. A live owner is not replaced."""
    base = jobs_root(root)
    _dirs(base)
    with _lock(base):
        scan = {
            "scan_id": f"{kind}-{uuid.uuid4()}",
            "discovery_key": kind,
            "claimed_at": now.isoformat(),
            "event_ids": [],
            "owner_pid": os.getpid(),
        }
        return _save_scan(base, scan, now=now)


def end_heavy_scan(root: Path | str | None) -> None:
    if root is None:
        return
    base = jobs_root(root)
    with _lock(base):
        current = _active_scan(base)
        if current and current.get("owner_pid") == os.getpid():
            _save_scan(base, None)


def release_claimed_job(root: Path | str, job: Mapping[str, Any]) -> None:
    base = jobs_root(root)
    job_id = str(job.get("job_id") or "")
    source = base / "processing" / f"{job_id}.json"
    if source.is_file():
        os.replace(source, base / "pending" / f"{job_id}.json")


def v1_buy_already_sent(job: Mapping[str, Any], root: Path | str | None) -> bool:
    if int(job.get("schema_version") or 1) >= 2 or job.get("trigger_type") != "buy_signal":
        return False
    base = jobs_root(root) if root is not None else None
    if base is None:
        return False
    record = _read_json(base / "delivered_buy.json") or {}
    fingerprint = _buy_fingerprint(job)
    return bool(fingerprint) and fingerprint in set(record.get("fingerprints") or [])


def remember_buy_delivery(job: Mapping[str, Any], root: Path | str | None) -> None:
    if root is None or job.get("trigger_type") != "buy_signal":
        return
    base = jobs_root(root)
    fingerprint = _buy_fingerprint(job)
    if not fingerprint:
        return
    record = _read_json(base / "delivered_buy.json") or {}
    found = [str(item) for item in record.get("fingerprints") or []]
    if fingerprint not in found:
        found.append(fingerprint)
    _atomic_write(base / "delivered_buy.json", {"fingerprints": found})


def _buy_fingerprint(job: Mapping[str, Any]) -> str:
    parts = []
    for signal in job.get("signals") or []:
        if isinstance(signal, dict) and signal.get("fingerprint"):
            parts.append(str(signal["fingerprint"]))
    return "|".join(parts)


def wait_for_result(root: Path | str, scan_id: str, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """Bounded wait for another process. This is not a pending-directory loop."""
    base = jobs_root(root)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _load_result(base, scan_id)
        if found is not None:
            return found
        time.sleep(0.02)
    return _load_result(base, scan_id)
