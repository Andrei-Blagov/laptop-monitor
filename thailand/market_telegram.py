"""Connect Telegram actions to the existing market-event queue. No second queue."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import config
from thailand.job_queue import _atomic_write, jobs_root
from thailand.market_events import build_market_event, enqueue_market_event
from thailand.market_messages import format_market_messages

_BUY = uuid.UUID("6f0d3c1e-7b2a-4f4e-9c1a-2a6e5d8b9c10")


def apply_configured_inputs(
    *,
    catalog_root,
    health_path,
    fx,
    russian_snapshot,
    russian_snapshot_root,
):
    """Fill only the inputs the caller did not pass. Does not fetch stores or CBR."""
    if catalog_root is None:
        catalog_root = config.THAI_CATALOG_ROOT
    if russian_snapshot_root is None:
        russian_snapshot_root = config.RUSSIAN_MARKET_ROOT
    if russian_snapshot is None:
        russian_snapshot = load_russian_snapshot(russian_snapshot_root)
    if fx is None:
        fx = load_fx_observation(config.FX_OBSERVATION_PATH)
    return catalog_root, health_path, fx, russian_snapshot, russian_snapshot_root


def load_russian_snapshot(root: Path | str | None) -> dict[str, Any] | None:
    if root is None:
        return None
    latest = Path(root) / "latest.json"
    if not latest.is_file():
        return None
    from market_snapshot import load_russian_market_snapshot

    return load_russian_market_snapshot(root)


def load_fx_observation(path: Path | str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    data = json.loads(file.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def sender_from_config():
    """Build the configured Telegram sender. It is not used while the flag is off."""
    token, chat = config.get_telegram_credentials()
    if not token or not chat:
        return None
    from telegram_sender import TelegramSender

    return TelegramSender(token, str(chat))


def latest_result(jobs_dir: Path | str | None) -> dict[str, Any] | None:
    base = jobs_root(jobs_dir)
    folder = base / "results"
    if not folder.is_dir():
        return None
    files = sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def enqueue_view_refresh(
    *,
    chat_id: str | int,
    view: str,
    trigger: str = "manual",
    jobs_dir: Path | str | None = None,
    now: datetime | None = None,
    ru_snapshot_id: str | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    event = build_market_event(
        instance_id=config.get_instance_id(),
        trigger_type=trigger,
        requested_view=view,
        recipients=[str(chat_id)],
        now=moment,
        source_ru_snapshot_id=ru_snapshot_id,
    )
    stored = enqueue_market_event(event, root=jobs_root(jobs_dir), now=moment)
    return stored


def enqueue_buy_candidate(
    signal: Any,
    *,
    instance_id: str,
    jobs_dir: Path | str | None,
    ru_snapshot_id: str | None = None,
    now: datetime | None = None,
    recipients: list[str] | None = None,
) -> dict[str, Any]:
    """One Thai summary event for a Russian BUY. The Russian text is not sent here."""
    fingerprint = str(getattr(signal, "fingerprint", "") or getattr(signal, "cluster_name", "") or "buy")
    event_id = str(uuid.uuid5(_BUY, fingerprint))
    moment = now or datetime.now(timezone.utc)
    if recipients is None:
        _token, chat = config.get_telegram_credentials()
        recipients = [str(chat)] if chat else []
    event = build_market_event(
        instance_id=instance_id,
        trigger_type="buy",
        requested_view="general_top",
        recipients=list(recipients),
        event_id=event_id,
        now=moment,
        source_ru_snapshot_id=ru_snapshot_id,
    )
    stored = enqueue_market_event(event, root=jobs_root(jobs_dir), now=moment)
    return {
        "ok": True,
        "duplicate": bool(stored.get("duplicate")),
        "job_id": event_id,
        "event_id": event_id,
    }


class MarketTextSender:
    """Format the stored result. Each confirmed part is stored before the next send."""

    def __init__(self, inner: Any, *, jobs_dir: Path | str, now_fn) -> None:
        self.inner = inner
        self.jobs_dir = jobs_dir
        self.now_fn = now_fn

    def send(self, event: dict[str, Any], result: Mapping[str, Any], *, current: bool, historical: bool, info_current: bool = False) -> None:
        del current, info_current
        parts = format_market_messages(result, event, now=self.now_fn())
        done = set(event.get("delivered_part_ids") or [])
        recipients = [str(item) for item in (event.get("recipients") or []) if str(item)]
        for part in parts:
            for recipient in recipients:
                part_id = f"{recipient}:{part['part_id']}"
                if part_id in done:
                    continue
                self._send_one(
                    event, result, part, recipient=recipient, part_id=part_id, historical=historical,
                )
                done.add(part_id)
                event["delivered_part_ids"] = sorted(done)
                _persist_delivery(self.jobs_dir, event)

    def _send_one(self, event, result, part, *, recipient: str, part_id: str, historical: bool) -> None:
        if hasattr(self.inner, "send_market_message"):
            outcome = self.inner.send_market_message(
                part["text"], chat_id=recipient, reply_markup=part.get("reply_markup"),
            )
            if not getattr(outcome, "ok", False):
                raise RuntimeError(getattr(outcome, "error", None) or "telegram_send_failed")
            return
        if hasattr(self.inner, "send_text"):
            self.inner.send_text(
                event, part["text"], part_id=part_id, current=not historical, historical=historical,
                info_current=True, reply_markup=part.get("reply_markup"), recipient=recipient,
            )
            return
        outcome = self.inner.send_message(part["text"])
        if not getattr(outcome, "ok", False):
            raise RuntimeError(getattr(outcome, "error", None) or "telegram_send_failed")


def _persist_delivery(jobs_dir: Path | str, event: Mapping[str, Any]) -> None:
    """Durable part list before the next send. A crash after HTTP and before this write can repeat that one part."""
    base = jobs_root(jobs_dir)
    event_id = str(event.get("event_id") or "")
    for folder in ("delivering", "delivery"):
        path = base / folder / f"{event_id}.json"
        if path.is_file():
            _atomic_write(path, dict(event))
            return
