"""Connect Telegram actions to the existing market-event queue. No second queue."""
from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import config
from thailand.job_queue import _atomic_write, _read_json, jobs_root
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stream_ready_view(result: Mapping[str, Any], *, chat_id: str | int, view: str, emit, now_fn=None) -> None:
    """Show a stored result one part at a time. Does not enqueue or touch observation times."""
    from thailand.market_messages import iter_market_sends

    event = {"requested_view": view, "recipients": [str(chat_id)]}
    for part in iter_market_sends(result, event, now_fn or utc_now):
        emit(part["text"], part.get("reply_markup"), part["recipient"])


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
        del current, historical, info_current
        from thailand.market_messages import iter_market_sends

        recipients = [str(item).strip() for item in (event.get("recipients") or []) if str(item).strip()]
        if not recipients:
            raise RuntimeError("no_recipient")
        done = _done_records(event.get("delivered_part_ids"))
        observed_now = None

        def at_send():
            nonlocal observed_now
            observed_now = self.now_fn()
            return observed_now

        for part in iter_market_sends(result, event, at_send):
            # A whole rendered presentation, result and recipient define reuse.
            # Matching a single card inside a different presentation is insufficient.
            plan = format_market_messages(result, event, now=observed_now)
            signature = [{"text": row["text"], "reply_markup": row.get("reply_markup")} for row in plan]
            local_id = part["part_id"][len(part["recipient"])+1:]
            index = next(i for i, row in enumerate(plan) if row["part_id"] == local_id and row["text"] == part["text"])
            content_id = hashlib.sha256(json.dumps([result.get("result_id") or result.get("scan_id") or uuid.uuid4().hex, part["recipient"], signature], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            content_path = jobs_root(self.jobs_dir) / "receipts" / ("content-" + content_id + ".json")
            saved_content = _read_json(content_path)
            if content_path.exists() and not isinstance(saved_content, dict):
                raise RuntimeError("unreadable_content_receipt")
            shared = saved_content or {"result_id": result.get("result_id"), "recipient": part["recipient"], "signature": signature, "acknowledged_indexes": []}
            if shared.get("result_id") != result.get("result_id") or shared.get("recipient") != part["recipient"] or shared.get("signature") != signature:
                raise RuntimeError("content_receipt_mismatch")
            indexes = shared.get("acknowledged_indexes")
            if not isinstance(indexes, list) or any(type(i) is not int or i < 0 or i >= len(plan) for i in indexes):
                raise RuntimeError("invalid_content_receipt")
            if _part_already_sent(part["part_id"], part["text"], done):
                if index not in indexes:
                    indexes.append(index)
                    _atomic_write(content_path, shared)
                continue
            historical = part.get("state") in {"historical", "info"}
            reused = index in indexes
            if not reused:
                self._send_one(
                    event, result, part, recipient=part["recipient"], part_id=part["part_id"],
                    historical=historical,
                )
                # First durable ACK, at the existing HTTP -> receipt boundary.
                # If the view receipt fails, the next attempt recovers from this one.
                indexes.append(index)
                _atomic_write(content_path, shared)
            done.append({"id": part["part_id"], "text": part["text"], "content_receipt_id": content_id, "reused": reused})
            event["delivered_part_ids"] = done
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


def _done_records(raw: Any) -> list[dict[str, str]]:
    records = []
    for item in raw or []:
        if isinstance(item, str):
            records.append({"id": item, "text": ""})
        elif isinstance(item, dict) and item.get("id"):
            records.append({"id": str(item["id"]), "text": str(item.get("text") or ""), **{key:item[key] for key in ("content_receipt_id", "reused") if key in item}})
    return records


def _part_already_sent(part_id: str, text: str, records: list[dict[str, str]]) -> bool:
    """Skip a part whose id or exact text was already confirmed, including a bare id that later grew a #index."""
    by_id = {item["id"]: item["text"] for item in records}
    previous = by_id.get(part_id)
    if previous is not None and (previous == "" or previous == text or text in previous):
        return True
    local = part_id.split(":", 1)[-1]
    base = local.split("#", 1)[0]
    recipient = part_id[: -len(local) - 1] if local != part_id else ""
    bare = f"{recipient}:{base}" if recipient else base
    blob = by_id.get(bare, "")
    if blob and text and text in blob:
        return True
    prefix = f"{recipient}:{base}#" if recipient else f"{base}#"
    pieces = [
        item["text"] for item in records
        if item["id"].startswith(prefix) and "#state:" not in item["id"] and item["text"]
    ]
    return bool(pieces and text and text in "\n".join(pieces))


def _persist_delivery(jobs_dir: Path | str, event: Mapping[str, Any]) -> None:
    """Durable part list before the next send. A crash after HTTP and before this write can repeat that one part."""
    base = jobs_root(jobs_dir)
    receipt_id = event.get("_delivery_receipt_id")
    if receipt_id:
        _atomic_write(base / "receipts" / f"{receipt_id}.json", {
            "delivered_part_ids": event.get("delivered_part_ids") or [],
        })
    event_id = str(event.get("event_id") or "")
    for folder in ("delivering", "delivery"):
        path = base / folder / f"{event_id}.json"
        if path.is_file():
            _atomic_write(path, dict(event))
            return
