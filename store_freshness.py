from __future__ import annotations

"""Fresh-store calculation for TOP / current-deals display."""

from datetime import datetime, timezone
from typing import Mapping, Sequence

import config


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_fresh_store_slugs(
    latest_runs: Mapping[str, Mapping],
    *,
    now: datetime | None = None,
    max_age_minutes: int | None = None,
) -> set[str]:
    """
    Store is fresh iff its LATEST attempt has status=ok AND finished_at within TTL.

    A failed latest attempt makes the store stale even if an older success exists.
    """
    ttl = (
        int(config.STORE_FRESHNESS_MAX_MINUTES)
        if max_age_minutes is None
        else int(max_age_minutes)
    )
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    fresh: set[str] = set()
    for store, row in latest_runs.items():
        if str(row.get("status") or "").lower() != "ok":
            continue
        finished = _parse_iso(row.get("finished_at") or row.get("attempted_at"))
        if finished is None:
            continue
        age_min = (now_utc - finished).total_seconds() / 60.0
        if age_min <= ttl:
            fresh.add(str(store))
    return fresh


def freshness_age_minutes(
    latest_runs: Mapping[str, Mapping],
    fresh_stores: Sequence[str] | set[str],
    *,
    now: datetime | None = None,
) -> int | None:
    """Max age in minutes among fresh stores (for TOP caption)."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    ages: list[float] = []
    for store in fresh_stores:
        row = latest_runs.get(store)
        if not row:
            continue
        finished = _parse_iso(row.get("finished_at") or row.get("attempted_at"))
        if finished is None:
            continue
        ages.append((now_utc - finished).total_seconds() / 60.0)
    if not ages:
        return None
    return int(max(0, round(max(ages))))
