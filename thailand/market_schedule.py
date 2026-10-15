"""Standard queue entry points. They do not scan stores or open a second queue."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from thailand.job_queue import jobs_root
from thailand.market_events import (
    MOSCOW,
    build_market_event,
    enqueue_market_event,
    moment,
    next_daily_not_before,
)

_DAILY = uuid.UUID("b7c1e0a2-4d55-4a1e-9f30-6c2b8a1d4e77")
_PERSONAL = uuid.UUID("c8d2f1b3-5e66-4b2f-a041-7d3c9b2e5f88")


def configured_recipients(chat_id: str | int | None = None) -> list[str]:
    if chat_id is not None and str(chat_id).strip():
        return [str(chat_id).strip()]
    return sorted(config.get_telegram_admin_chat_ids())


def market_work_waiting(jobs_dir: Path | str | None = None, *, now: datetime | None = None) -> bool:
    """True when a wake would promote, scan, or deliver. An empty queue is not work."""
    base = jobs_root(jobs_dir)
    clock = now or datetime.now(timezone.utc)
    for name in ("pending", "processing", "delivery", "delivering"):
        folder = base / name
        if folder.is_dir() and any(folder.glob("*.json")):
            return True
    deferred = base / "deferred"
    if not deferred.is_dir():
        return False
    for path in deferred.glob("*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or int(event.get("schema_version") or 0) < 2:
            continue
        due = moment(event.get("not_before"))
        if due is None or due <= clock:
            return True
    return False


def enqueue_daily(*, now: datetime | None = None, jobs_dir: Path | str | None = None) -> dict[str, Any]:
    """One shared daily event. Before 08:00 Europe/Moscow it waits in deferred/."""
    if not config.stage22_market_telegram_enabled():
        return {"ok": True, "status": "flag_off"}
    moment_now = now or datetime.now(timezone.utc)
    local_day = moment_now.astimezone(MOSCOW).date().isoformat()
    event_id = str(uuid.uuid5(_DAILY, f"{config.get_instance_id()}|{local_day}|daily"))
    event = build_market_event(
        instance_id=config.get_instance_id(),
        trigger_type="daily",
        requested_view="general_top",
        recipients=configured_recipients(),
        event_id=event_id,
        now=moment_now,
        not_before=next_daily_not_before(moment_now),
    )
    stored = enqueue_market_event(event, root=jobs_root(jobs_dir), now=moment_now)
    stored["status"] = stored.get("status") or ("duplicate" if stored.get("duplicate") else "queued")
    return stored


def enqueue_personal_market(
    *,
    chat_id: str | int | None = None,
    now: datetime | None = None,
    jobs_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Personal TOP-3 on the shared queue. Does not start a separate Thai scan."""
    moment_now = now or datetime.now(timezone.utc)
    recipients = configured_recipients(chat_id)
    local_day = moment_now.astimezone(MOSCOW).date().isoformat()
    identity = recipients[0] if len(recipients) == 1 else ",".join(recipients)
    event_id = str(uuid.uuid5(_PERSONAL, f"{config.get_instance_id()}|{local_day}|personal|{identity}"))
    event = build_market_event(
        instance_id=config.get_instance_id(),
        trigger_type="personal",
        requested_view="personal_top3",
        recipients=recipients,
        event_id=event_id,
        now=moment_now,
    )
    stored = enqueue_market_event(event, root=jobs_root(jobs_dir), now=moment_now)
    return {
        "ok": True,
        "duplicate": bool(stored.get("duplicate")),
        "job_id": event_id,
        "event_id": event_id,
        "error": None,
        "thailand_scan_status": "queued",
        "trigger_type": "personal",
        "market_event": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue the shared Thailand market schedule")
    parser.add_argument("command", choices=("enqueue-daily",))
    args = parser.parse_args(argv)
    result = enqueue_daily()
    print(result.get("status") or result.get("error") or "queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
