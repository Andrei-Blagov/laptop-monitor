from __future__ import annotations

"""File-backed Thailand source health and circuit breaker. No DB migration."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from thailand.errors import NO_RESULTS
from thailand.storage import atomic_write_json

_EMPTY = {"version": 1, "stores": {}}


def health_file(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(config.THAILAND_SOURCE_HEALTH_PATH)


def load_health(path: Path | str | None = None) -> dict[str, Any]:
    """Corrupt or missing state is an empty closed breaker, not an exception."""
    p = health_file(path)
    if not p.exists():
        return {"version": 1, "stores": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"version": 1, "stores": {}}
    if not isinstance(data, dict):
        return {"version": 1, "stores": {}}
    stores = data.get("stores")
    if not isinstance(stores, dict):
        stores = {}
    return {"version": 1, "stores": stores}


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _blank_entry() -> dict[str, Any]:
    return {
        "last_success_at": None,
        "last_failure_at": None,
        "last_error_code": None,
        "consecutive_failures": 0,
        "last_offer_count": 0,
        "last_runtime_seconds": None,
        "disabled_until": None,
    }


def store_entry(state: dict[str, Any], store: str) -> dict[str, Any]:
    stores = state.setdefault("stores", {})
    entry = stores.get(store)
    if not isinstance(entry, dict):
        entry = _blank_entry()
        stores[store] = entry
    return entry


def circuit_status(
    store: str,
    *,
    now: datetime | None = None,
    path: Path | str | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """closed | open | half_open."""
    now = now or datetime.now(timezone.utc)
    state = state if state is not None else load_health(path)
    entry = (state.get("stores") or {}).get(store) or {}
    until = _parse_dt(entry.get("disabled_until"))
    if until is None:
        return "closed"
    if now < until:
        return "open"
    return "half_open"


def should_skip(
    store: str,
    *,
    now: datetime | None = None,
    path: Path | str | None = None,
    force: bool = False,
) -> bool:
    """True only while the cooldown window is still open. Manual force bypasses."""
    if force:
        return False
    return circuit_status(store, now=now, path=path) == "open"


def record_observation(
    store: str,
    *,
    ok: bool,
    error_code: str | None = None,
    offer_count: int = 0,
    runtime_seconds: float | None = None,
    now: datetime | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Persist one store attempt.

    NO_RESULTS and ok=True reset the breaker. Transport failures count.
    """
    now = now or datetime.now(timezone.utc)
    state = load_health(path)
    entry = store_entry(state, store)
    entry["last_offer_count"] = int(offer_count)
    if runtime_seconds is not None:
        entry["last_runtime_seconds"] = round(float(runtime_seconds), 3)
    success = bool(ok) or error_code == NO_RESULTS
    if success:
        entry["consecutive_failures"] = 0
        entry["last_success_at"] = now.isoformat()
        entry["last_error_code"] = None
        entry["disabled_until"] = None
    else:
        fails = int(entry.get("consecutive_failures") or 0) + 1
        entry["consecutive_failures"] = fails
        entry["last_failure_at"] = now.isoformat()
        entry["last_error_code"] = error_code
        threshold = int(config.THAILAND_SOURCE_FAILURE_THRESHOLD)
        if fails >= threshold:
            hours = float(config.THAILAND_SOURCE_COOLDOWN_HOURS)
            entry["disabled_until"] = (now + timedelta(hours=hours)).isoformat()
    atomic_write_json(health_file(path), state)
    return entry
