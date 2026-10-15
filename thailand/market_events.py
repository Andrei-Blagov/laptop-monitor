"""Schema-2 market events on the existing file queue.

Deferred events stay out of pending/. One heavy Thai pass serves every
compatible subscriber. Delivery retries do not call stores.
"""
from __future__ import annotations

import json
import hashlib
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


_LEASES: dict[str, dict[str, Any]] = {}


def _lease_key(path: Path) -> str:
    return str(path.resolve())


def _forget_inherited_leases() -> None:
    # Closing an inherited descriptor does not unlock the parent's flock.
    # LOCK_UN here would unlock the shared open-file description in both.
    for state in _LEASES.values():
        state["handle"].close()
    _LEASES.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_inherited_leases)


class VolumeLease:
    """Exclusive lock on the shared queue volume.

    The kernel releases it when the holding process dies, including a process
    in another container. A local ``/proc`` lookup cannot see that process.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self._counted = False
        self._owner_pid = None

    def try_acquire(self) -> bool:
        key = _lease_key(self.path)
        state = _LEASES.get(key)
        if self._counted and state and state["pid"] == os.getpid():
            return True
        self._counted = False
        if state:
            state["count"] += 1
            self._counted = True
            self._owner_pid = os.getpid()
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self.path.write_bytes(b"\0")
        self._fh = open(self.path, "r+b")
        self._fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        _LEASES[key] = {"handle": self._fh, "count": 1, "pid": os.getpid()}
        self._counted = True
        self._owner_pid = os.getpid()
        return True

    def release(self) -> None:
        if not self._counted or self._owner_pid != os.getpid():
            return
        key = _lease_key(self.path)
        state = _LEASES.get(key)
        self._counted = False
        if state is None or state["pid"] != os.getpid():
            self._fh = None
            return
        left = state["count"] - 1
        if left > 0:
            state["count"] = left
            self._fh = None
            return
        _LEASES.pop(key, None)
        self._fh = state["handle"]
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def _lease_path(base: Path, name: str) -> Path:
    return base / name


def scan_lease_live(path: Path) -> bool:
    """True while this process or another container still holds the scan lease."""
    if _lease_key(path) in _LEASES:
        return True
    return lease_held_by_other(path)


def lease_held_by_other(path: Path) -> bool:
    """True when a different process, possibly in another container, holds ``path``."""
    if _lease_key(path) in _LEASES:
        return False
    probe = VolumeLease(path)
    if probe.try_acquire():
        probe.release()
        return False
    return True


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
    for name in ("deferred", "results", "delivery", "delivering", "receipts"):
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
        live = scan is not None and scan_lease_live(_lease_path(base, "scan.lease"))
        if live and scan.get("discovery_key") == body.get("discovery_key") and (due is None or due <= clock):
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


def process_started_at(pid: object) -> str | None:
    """Start token of this process image. None when that image is gone.

    The PID number is not the token: the kernel can give it to a later process.
    """
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if os.name == "nt":
        return _windows_started_at(number)
    return _linux_started_at(number)


def _linux_started_at(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    if len(fields) < 20:
        return None
    if fields[0] == "Z":
        return None
    return fields[19]


def _windows_started_at(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.windll.kernel32

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created = _FileTime()
        unused = _FileTime()
        if not kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(unused), ctypes.byref(unused), ctypes.byref(unused)
        ):
            return None
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        return str((int(created.high) << 32) | int(created.low))
    finally:
        kernel.CloseHandle(handle)


def _owner_live(record: Mapping[str, Any] | None, *, pid_field: str = "owner_pid", started_field: str = "owner_started_at") -> bool:
    if not record:
        return False
    started = record.get(started_field)
    if started in (None, ""):
        return False
    return process_started_at(record.get(pid_field)) == str(started)


def _owner_is_self(record: Mapping[str, Any] | None, *, pid_field: str = "owner_pid", started_field: str = "owner_started_at") -> bool:
    if not record:
        return False
    try:
        same_pid = int(record.get(pid_field)) == os.getpid()
    except (TypeError, ValueError):
        return False
    return same_pid and _owner_live(record, pid_field=pid_field, started_field=started_field)


def _stamp_owner(record: dict[str, Any], *, pid_field: str = "owner_pid", started_field: str = "owner_started_at") -> None:
    record[pid_field] = os.getpid()
    record[started_field] = process_started_at(os.getpid())


def _scan_live(base: Path, scan: Mapping[str, Any] | None, now: datetime, stale_seconds: float) -> bool:
    """A scan is live only while its lease is held. ``/proc`` is not shared by containers."""
    del scan, now, stale_seconds
    return lease_held_by_other(_lease_path(base, "scan.lease"))


def _active_scan(root: Path) -> dict[str, Any] | None:
    return _read_json(root / "active_scan.json")


def _save_scan(root: Path, scan: dict[str, Any] | None, *, now: datetime | None = None, stale_seconds: float = 600) -> bool:
    path = root / "active_scan.json"
    current = _active_scan(root)
    if scan is not None and current and current.get("scan_id") != scan.get("scan_id"):
        if _scan_live(root, current, now or datetime.now(timezone.utc), stale_seconds):
            return False
    if scan is None:
        if current and _scan_live(root, current, now or datetime.now(timezone.utc), stale_seconds):
            return False
        if path.is_file():
            path.unlink()
        return True
    _atomic_write(path, scan)
    return True


def recover_market_scan(
    *,
    root: Path | str,
    now: datetime,
    stale_seconds: float = 0,
    catalog_root: Path | str | None = None,
    russian_snapshot_root: Path | str | None = None,
) -> list[str]:
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
                        _protect_linked(stored, catalog_root, russian_snapshot_root)
                    _write_event(base, "delivery", event)
                    _remove_event(base, event["event_id"], keep="delivery")
                    continue
                event["delivery_state"] = "pending"
                event["subscription_scan_id"] = None
                _write_event(base, "pending", event)
                _remove_event(base, event["event_id"], keep="pending")
                recovered.append(event["event_id"])
            return recovered
        if _scan_live(base, scan, now, stale_seconds):
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
                    _protect_linked(stored, catalog_root, russian_snapshot_root)
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
    if _scan_live(base, scan, now, 600):
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
    }
    _stamp_owner(scan)
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


def _view_refs(outcome: Mapping[str, Any]) -> dict[str, list[str]]:
    """Point each view at rows already stored on the D result."""
    comparison = outcome.get("comparison") or {}
    if comparison.get("pairs"):
        from cross_market_comparison import comparison_view

        return {name: [str(pair["pair_id"]) for pair in comparison_view(comparison, name)] for name in ("all", "general_top", "personal_top3")}
    rankings = outcome.get("rankings") or {}
    return {
        "all": [str(row["model_id"]) for row in rankings.get("eligible_models") or [] if row.get("model_id")],
        "general_top": [str(row["model_id"]) for row in rankings.get("thai_top_value") or [] if row.get("model_id")],
        "personal_top3": [],
    }


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


def _observation_fresh(offer: Mapping[str, Any], now: datetime, hours: int) -> bool:
    return age_valid(moment(offer.get("price_checked_at")), now, hours) and age_valid(
        moment(offer.get("availability_checked_at")), now, hours
    )


def _shown_pairs(result: Mapping[str, Any], view: str) -> list[Mapping[str, Any]]:
    wanted = {str(item) for item in (result.get("views") or {}).get(view) or []}
    return [pair for pair in ((result.get("comparison") or {}).get("pairs") or []) if str(pair.get("pair_id")) in wanted]


def _shown_models(result: Mapping[str, Any], view: str) -> list[Mapping[str, Any]]:
    wanted = {str(item) for item in (result.get("views") or {}).get(view) or []}
    rankings = result.get("rankings") or {}
    found = []
    seen: set[str] = set()
    for row in list(rankings.get("eligible_models") or []) + list(rankings.get("thai_top_value") or []):
        model_id = str(row.get("model_id") or "")
        if model_id in wanted and model_id not in seen:
            seen.add(model_id)
            found.append(row)
    return found


def _delivery_flags(result: Mapping[str, Any], view: str, now: datetime) -> tuple[bool, bool]:
    """Informational freshness is separate from permission to show a current RUB saving."""
    policy = ComparisonPolicy()
    pairs = _shown_pairs(result, view)
    if pairs:
        info = all(
            _observation_fresh(pair.get("th_observation") or {}, now, policy.quote_hours)
            and _observation_fresh(pair.get("ru_observation") or {}, now, policy.ru_quote_hours)
            for pair in pairs
        )
        fx = result.get("fx") or {}
        purchase = bool(fx) and _fx_usable(fx, now, policy) and all(
            pair.get("comparison_status") == "confirmed"
            and _observation_fresh(pair.get("th_observation") or {}, now, policy.quote_hours)
            and _observation_fresh(pair.get("ru_observation") or {}, now, policy.ru_quote_hours)
            for pair in pairs
        )
        return purchase, info
    models = _shown_models(result, view)
    info = bool(models) and all(
        all(_observation_fresh(offer, now, policy.quote_hours) for offer in (row.get("offers") or []) if isinstance(offer, dict))
        for row in models
    )
    return False, info


def _deliver_one(sender, event: dict[str, Any], result: Mapping[str, Any], now: datetime) -> str:
    purchase, info = _delivery_flags(result, str(event.get("requested_view") or "general_top"), now)
    if sender is None:
        return "skipped"
    sender.send(event, result, current=purchase, historical=not purchase, info_current=info)
    return "sent" if purchase else "historical"


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


def _protect_linked(result: Mapping[str, Any], catalog_root, russian_snapshot_root) -> None:
    """Write the ids that Thai and Russian retention actually read."""
    if catalog_root is not None and result.get("snapshot_id"):
        _protect(Path(catalog_root), [str(result["snapshot_id"])])
    if russian_snapshot_root is not None and result.get("ru_snapshot_id"):
        _protect(Path(russian_snapshot_root), [str(result["ru_snapshot_id"])])


def _finish_scan(base, catalog_root, scan, events, outcome, now, fx=None, russian_snapshot_root=None) -> dict[str, Any]:
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
        "rankings": outcome.get("rankings"),
        "comparison": comparison or None,
        "coverage": list(outcome.get("coverage") or []),
        "errors": list(outcome.get("errors") or []),
        "views": _view_refs(outcome),
    }
    _atomic_write(base / "results" / f"{scan['scan_id']}.json", result)
    _protect_linked(result, catalog_root, russian_snapshot_root)
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
    russian_snapshot_root: Path | str | None = None,
    transport: Any = None,
    sender: Any = None,
    clock: Any = None,
    deliver: bool = True,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """One worker wake: promote due events, run at most one pass, deliver stored results."""
    base = jobs_root(jobs_dir)
    if not base.exists():
        return None
    _dirs(base)
    clock_now = now or (clock.now() if clock is not None else datetime.now(timezone.utc))
    import config
    scan_lease_path = _lease_path(base, "scan.lease")
    if lease_held_by_other(scan_lease_path):
        with _lock(base):
            scan = _active_scan(base)
            if scan:
                for event in _load_events(base / "pending"):
                    if event.get("discovery_key") == scan.get("discovery_key"):
                        _subscribe(base, scan, event)
        return _retry_delivery(base, sender, clock=clock, fallback=now, deliver=deliver)
    scan_lease = VolumeLease(scan_lease_path)
    if not scan_lease.try_acquire():
        with _lock(base):
            scan = _active_scan(base)
            if scan:
                for event in _load_events(base / "pending"):
                    if event.get("discovery_key") == scan.get("discovery_key"):
                        _subscribe(base, scan, event)
        return _retry_delivery(base, sender, clock=clock, fallback=now, deliver=deliver)
    try:
        return _run_market_queue_locked(
            base, catalog_root=catalog_root, health_path=health_path, fx=fx,
            russian_snapshot=russian_snapshot, russian_snapshot_root=russian_snapshot_root,
            transport=transport, sender=sender, clock=clock, deliver=deliver,
            now=now, clock_now=clock_now,
        )
    finally:
        scan_lease.release()


def _run_market_queue_locked(
    base, *, catalog_root, health_path, fx, russian_snapshot, russian_snapshot_root,
    transport, sender, clock, deliver, now, clock_now,
):
    import config
    recover_market_scan(
        root=base, now=clock_now,
        stale_seconds=float(config.THAILAND_JOB_STALE_PROCESSING_SECONDS),
        catalog_root=catalog_root, russian_snapshot_root=russian_snapshot_root,
    )
    promote_due_events(root=base, now=clock_now)
    delivered = _retry_delivery(base, sender, clock=clock, fallback=now, deliver=deliver)
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
    result = _finish_scan(
        base, Path(catalog_root), scan, events, outcome, clock_now, fx=fx,
        russian_snapshot_root=russian_snapshot_root,
    )
    if deliver and sender is not None:
        delivered = _retry_delivery(base, sender, clock=clock, fallback=now, deliver=True)
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
    _stamp_owner(event, pid_field="delivery_owner", started_field="delivery_owner_started_at")
    _atomic_write(destination, event)
    return event


def _send_moment(clock, fallback: datetime | None) -> datetime:
    if clock is not None:
        return clock.now()
    return fallback if fallback is not None else datetime.now(timezone.utc)


def _retry_delivery(base: Path, sender, *, clock=None, fallback: datetime | None = None, deliver: bool = True) -> dict[str, Any] | None:
    """Deliver stored results. A live delivery lease in any container is not stolen."""
    if not deliver or sender is None:
        return None
    path = _lease_path(base, "delivery.lease")
    if lease_held_by_other(path):
        return None
    lease = VolumeLease(path)
    if not lease.try_acquire():
        return None
    try:
        return _retry_delivery_locked(base, sender, clock=clock, fallback=fallback)
    finally:
        lease.release()


def _retry_delivery_locked(base: Path, sender, *, clock=None, fallback: datetime | None = None) -> dict[str, Any] | None:
    """Claim waiting events. One view is sent once to each recipient, and every event is kept."""
    (base / "delivering").mkdir(parents=True, exist_ok=True)
    with _lock(base):
        for path in sorted((base / "delivering").glob("*.json")):
            event = _read_json(path)
            if not isinstance(event, dict):
                continue
            _write_event(base, "delivery", event)
            path.unlink(missing_ok=True)
    waiting = _load_events(base / "delivery")
    if not waiting:
        return None
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in waiting:
        result_id = str(event.get("subscription_scan_id") or event.get("result_id") or "")
        if _load_result(base, result_id) is None:
            continue
        view = str(event.get("requested_view") or "general_top")
        groups.setdefault((result_id, view), []).append(event)
    sent_ids = []
    for (result_id, view), events in groups.items():
        result = _load_result(base, result_id)
        receipt_id = hashlib.sha256(json.dumps([result_id, view]).encode()).hexdigest()
        receipt = _read_json(base / "receipts" / f"{receipt_id}.json") or {}
        claimed = []
        for event in events:
            with _lock(base):
                one = _claim_delivery(base, str(event["event_id"]))
            if one is not None:
                claimed.append(one)
        if not claimed or result is None:
            continue
        recipients = []
        done = []
        seen_ids: set[str] = set()
        for record in receipt.get("delivered_part_ids") or []:
            if isinstance(record, dict) and record.get("id"):
                seen_ids.add(str(record["id"]))
                done.append(record)
        for event in claimed:
            for recipient in event.get("recipients") or []:
                text = str(recipient).strip()
                if text and text not in recipients:
                    recipients.append(text)
            for record in event.get("delivered_part_ids") or []:
                ident = record.get("id") if isinstance(record, dict) else str(record)
                if ident and ident not in seen_ids:
                    seen_ids.add(str(ident))
                    done.append(record if isinstance(record, dict) else {"id": str(record), "text": ""})
        primary = claimed[0]
        outgoing = dict(primary)
        outgoing["recipients"] = recipients
        outgoing["requested_view"] = view
        outgoing["delivered_part_ids"] = done
        outgoing["_delivery_receipt_id"] = receipt_id
        now = _send_moment(clock, fallback)
        try:
            state = _deliver_one(sender, outgoing, result, now)
        except Exception as exc:  # noqa: BLE001
            for event in claimed:
                event["delivered_part_ids"] = outgoing.get("delivered_part_ids")
                event["delivery_state"] = "deferred"
                event["retry_count"] = int(event.get("retry_count") or 0) + 1
                event["delivery_error"] = type(exc).__name__
                event["not_before"] = (now + timedelta(minutes=30)).isoformat()
                event.pop("delivery_owner", None)
                event.pop("delivery_owner_started_at", None)
                _write_event(base, "deferred", event)
                (base / "delivering" / f"{event['event_id']}.json").unlink(missing_ok=True)
            return {"status": "delivery_failed", "event_id": primary["event_id"], "http_calls": 0}
        _atomic_write(base / "receipts" / f"{receipt_id}.json", {
            "delivered_part_ids": outgoing.get("delivered_part_ids") or [],
        })
        for event in claimed:
            event["delivered_part_ids"] = outgoing.get("delivered_part_ids")
            event["delivery_state"] = "sent"
            event["delivered_as"] = state
            event.pop("delivery_owner", None)
            event.pop("delivery_owner_started_at", None)
            _write_event(base, "archive", event)
            (base / "delivering" / f"{event['event_id']}.json").unlink(missing_ok=True)
            _remove_event(base, event["event_id"], keep="archive")
            sent_ids.append(event["event_id"])
    if not sent_ids:
        return None
    return {"status": "delivered", "event_ids": sent_ids, "http_calls": 0}


def heavy_scan_busy(root: Path | str | None, now: datetime, stale_seconds: float = 600) -> bool:
    if root is None:
        return False
    base = jobs_root(root)
    return _scan_live(base, _active_scan(base), now, stale_seconds)


_OPEN_LEASES: dict[str, VolumeLease] = {}


def begin_heavy_scan(root: Path | str, now: datetime, *, kind: str) -> bool:
    """Take the single heavy-pass lease. A live owner is not replaced."""
    base = jobs_root(root)
    _dirs(base)
    path = _lease_path(base, "scan.lease")
    if lease_held_by_other(path):
        return False
    lease = VolumeLease(path)
    if not lease.try_acquire():
        return False
    with _lock(base):
        scan = {
            "scan_id": f"{kind}-{uuid.uuid4()}",
            "discovery_key": kind,
            "claimed_at": now.isoformat(),
            "event_ids": [],
        }
        _stamp_owner(scan)
        if not _save_scan(base, scan, now=now):
            lease.release()
            return False
    _OPEN_LEASES[str(path)] = lease
    return True


def end_heavy_scan(root: Path | str | None) -> None:
    if root is None:
        return
    base = jobs_root(root)
    with _lock(base):
        current = _active_scan(base)
        if current is None or _owner_is_self(current) or not lease_held_by_other(_lease_path(base, "scan.lease")):
            _save_scan(base, None)
    lease = _OPEN_LEASES.pop(str(_lease_path(base, "scan.lease")), None)
    if lease is not None:
        lease.release()


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
