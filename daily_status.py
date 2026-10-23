"""Read-only, content-bound daily ACK aggregates. Never logs message text/recipients."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))


def _read(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid receipt")
    return value


def _proof(event, root):
    recipients = event.get("recipients")
    records = event.get("delivered_part_ids")
    if (not isinstance(recipients, list) or not recipients or not isinstance(records, list) or not records
            or len(set(map(str, recipients))) != len(recipients)):
        raise ValueError("missing daily ACK")
    seen, expected = set(), set()
    for record in records:
        if not isinstance(record, dict) or type(record.get("reused")) is not bool:
            raise ValueError("invalid daily part")
        ident, text, key = (record.get(name) for name in ("id", "text", "content_receipt_id"))
        if not all(isinstance(value, str) for value in (ident, text, key)) or not re.fullmatch("[a-f0-9]{64}", key):
            raise ValueError("invalid binding")
        recipient, part = ident.rsplit(":", 1)
        if recipient not in set(map(str, recipients)) or not re.fullmatch(r"daily#\d+", part) or ident in seen:
            raise ValueError("invalid recipient part")
        saved = _read(root / "receipts" / ("content-" + key + ".json"))
        signature, indexes = saved.get("signature"), saved.get("acknowledged_indexes")
        if not isinstance(signature, list) or not signature or not isinstance(indexes, list):
            raise ValueError("missing durable ACK")
        if any(type(i) is not int or not 0 <= i < len(signature) for i in indexes):
            raise ValueError("invalid durable ACK")
        digest = hashlib.sha256(json.dumps([saved.get("result_id"), recipient, signature],
                                          sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        index = int(part.split("#")[1])
        if (key != digest or saved.get("recipient") != recipient or saved.get("result_id") != event.get("result_id")
                or index not in indexes or index >= len(signature) or signature[index].get("text") != text):
            raise ValueError("ACK mismatch")
        expected.update(f"{recipient}:daily#{i}" for i in range(len(signature)))
        seen.add(ident)
    complete = seen == expected and all(any(item.startswith(str(r) + ":") for item in seen) for r in recipients)
    return {"parts": len(seen), "expected": len(expected), "complete": complete}


def daily_status(root, *, now=None, expected_recipients=None):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(MOSCOW).date()
    out = {"today_ack": 0, "today_parts": 0, "today_parts_expected": 0, "invalid": 0, "pending": 0, "failed": 0,
           "last_day": None, "last_ack_persisted_at": None, "unknown": False}
    for folder in ("pending", "processing", "deferred", "delivery", "delivering", "archive", "failed"):
        paths = list((root / folder).glob("*.json"))
        if len(paths) > 5000:
            out["unknown"] = True
            return out
        for path in paths:
            try:
                event = _read(path)
                if event.get("trigger_type") != "daily":
                    continue
                created = datetime.fromisoformat(str(event["created_at"]).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    raise ValueError("unknown event timezone")
                event_day = created.astimezone(MOSCOW).date()
                if event_day == day and folder not in {"archive", "failed"}:
                    out["pending"] += 1
                if event_day == day and folder == "failed":
                    out["failed"] += 1
                if folder == "archive" and event.get("delivery_state") == "sent":
                    if expected_recipients is not None and set(map(str, event.get("recipients") or [])) != set(map(str, expected_recipients)):
                        raise ValueError("daily recipient configuration mismatch")
                    proof = _proof(event, root)
                    # A saved ACK for yesterday's presentation cannot prove today's daily.
                    if not any(event_day.strftime("%d.%m.%Y") in record["text"] for record in event["delivered_part_ids"]):
                        raise ValueError("daily presentation date mismatch")
                    if event_day == day:
                        out["today_parts"] += proof["parts"]
                        out["today_parts_expected"] += proof["expected"]
                    if not proof["complete"]:
                        raise ValueError("incomplete ACK")
                    if event_day == day:
                        out["today_ack"] += 1
                    if out["last_day"] is None or event_day.isoformat() > out["last_day"]:
                        out["last_day"] = event_day.isoformat()
                        out["last_ack_persisted_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                out["invalid"] += 1
    return out


def format_daily_status(status):
    if status["unknown"]:
        return "Ежедневная сводка: статус неизвестен (проверка очереди не завершена)."
    label = ("подтверждена" if status["today_ack"] == 1 and not status["invalid"] else
             "аномалия подтверждений" if status["today_ack"] > 1 or status["invalid"] else "ACK пока нет")
    return (f"Ежедневная сводка сегодня: {label}; частей ACK: {status['today_parts']}/{status['today_parts_expected']}.\n"
            f"Последняя полная daily: {status['last_day'] or 'нет подтверждения'}.\n"
            f"ACK сохранён: {status['last_ack_persisted_at'] or 'неизвестно'}.\n"
            f"Daily в очереди: {status['pending']}; failed: {status['failed']}; невалидных: {status['invalid']}.\n"
            "ACK означает принятие Telegram API, не прочтение сообщения.")
