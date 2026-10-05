from __future__ import annotations

"""File-backed Thailand source health and circuit breaker. No DB migration."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config
from thailand.errors import NO_RESULTS, RATE_LIMITED
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
    state = {"version": 1, "stores": stores}
    _interpret_legacy_rate_limits(state)
    return state


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
        "rate_limit_until": None,
    }


def store_entry(state: dict[str, Any], store: str) -> dict[str, Any]:
    stores = state.setdefault("stores", {})
    entry = stores.get(store)
    if not isinstance(entry, dict):
        entry = _blank_entry()
        stores[store] = entry
    return entry


def _legacy_429_evidence(entry: dict[str, Any]) -> bool:
    """True only when a stored field itself says 429. HTTP_ERROR alone is not proof."""
    blob = " ".join(
        str(entry.get(key) or "")
        for key in ("last_error_code", "technical_detail", "last_error_detail", "error")
    ).upper()
    return "429" in blob or "RATE_LIMITED" in blob


def _interpret_legacy_rate_limits(state: dict[str, Any], *, now: datetime | None = None) -> None:
    """
    Reclassify a legacy failure as RATE_LIMITED only for SpeedCom and only when
    the saved entry explicitly records HTTP 429.

    A bare HTTP_ERROR is left as a generic failure. This read does not write
    the production file.
    """
    now = now or datetime.now(timezone.utc)
    minutes = float(getattr(config, "THAILAND_RATE_LIMIT_DEFAULT_MINUTES", 60.0))
    for store, entry in (state.get("stores") or {}).items():
        if store != "speedcom" or not isinstance(entry, dict) or entry.get("rate_limit_migrated"):
            continue
        if not _legacy_429_evidence(entry):
            continue
        if not int(entry.get("consecutive_failures") or 0) and entry.get("last_error_code") == RATE_LIMITED:
            continue
        entry["consecutive_failures"] = 0
        entry["disabled_until"] = None
        entry["last_error_code"] = RATE_LIMITED
        failed = _parse_dt(entry.get("last_failure_at"))
        if failed is not None:
            until = failed + timedelta(minutes=minutes)
            if until > now:
                entry["rate_limit_until"] = until.isoformat()
        entry["rate_limit_migrated"] = True


def rate_limit_active(
    store: str,
    *,
    now: datetime | None = None,
    path: Path | str | None = None,
    state: dict[str, Any] | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    state = state if state is not None else load_health(path)
    entry = (state.get("stores") or {}).get(store) or {}
    until = _parse_dt(entry.get("rate_limit_until"))
    return until is not None and now < until


def skip_reason(
    store: str,
    *,
    now: datetime | None = None,
    path: Path | str | None = None,
    force: bool = False,
) -> str | None:
    """circuit | rate_limit | None. Manual force bypasses both."""
    if force:
        return None
    if circuit_status(store, now=now, path=path) == "open":
        return "circuit"
    if rate_limit_active(store, now=now, path=path):
        return "rate_limit"
    return None


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
    """True while the circuit is open or a rate-limit window is active."""
    return skip_reason(store, now=now, path=path, force=force) is not None


def record_observation(
    store: str,
    *,
    ok: bool,
    error_code: str | None = None,
    offer_count: int = 0,
    runtime_seconds: float | None = None,
    retry_after_seconds: int | None = None,
    now: datetime | None = None,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Persist one store attempt.

    NO_RESULTS and ok=True reset the breaker. HTTP 429 sets rate_limit_until
    and does not increment consecutive_failures. Other transport failures do.
    """
    now = now or datetime.now(timezone.utc)
    state = load_health(path)
    entry = store_entry(state, store)
    entry["last_offer_count"] = int(offer_count)
    if runtime_seconds is not None:
        entry["last_runtime_seconds"] = round(float(runtime_seconds), 3)
    if error_code == RATE_LIMITED:
        entry["last_failure_at"] = now.isoformat()
        entry["last_error_code"] = RATE_LIMITED
        seconds = retry_after_seconds
        if seconds is None or seconds <= 0:
            seconds = int(float(config.THAILAND_RATE_LIMIT_DEFAULT_MINUTES) * 60)
        entry["rate_limit_until"] = (now + timedelta(seconds=int(seconds))).isoformat()
        entry["rate_limit_migrated"] = True
        atomic_write_json(health_file(path), state)
        return entry
    success = bool(ok) or error_code == NO_RESULTS
    if success:
        entry["consecutive_failures"] = 0
        entry["last_success_at"] = now.isoformat()
        entry["last_error_code"] = None
        entry["disabled_until"] = None
        entry["rate_limit_until"] = None
    else:
        fails = int(entry.get("consecutive_failures") or 0) + 1
        entry["consecutive_failures"] = fails
        entry["last_failure_at"] = now.isoformat()
        entry["last_error_code"] = error_code
        threshold = int(config.THAILAND_SOURCE_FAILURE_THRESHOLD)
        if fails >= threshold:
            hours = float(config.THAILAND_SOURCE_COOLDOWN_HOURS)
            entry["disabled_until"] = (now + timedelta(hours=hours)).isoformat()
    entry["rate_limit_migrated"] = True
    atomic_write_json(health_file(path), state)
    return entry
