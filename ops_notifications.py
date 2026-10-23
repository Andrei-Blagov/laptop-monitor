"""Durable operational transitions; independent of daily and price delivery."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from pipeline_lock import pipeline_lock
from telegram_sender import TelegramSender

logger = logging.getLogger(__name__)
_STATES = {"success", "partial", "failed"}
_HEALTHY = {"ok", "success", "skipped", "disabled"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _error_kind(value: Any) -> str:
    # Structural category only: never store raw errors/URLs/credentials.
    prefix = str(value or "").split(":", 1)[0].strip()
    known = {"sanity", "timeouterror", "timeoutexception", "httperror", "connecterror", "readtimeout",
             "connecttimeout", "valueerror", "runtimeerror", "permissionerror", "filenotfounderror",
             "operationalerror", "integrityerror", "jsondecodeerror"}
    return prefix.lower() if prefix.lower() in known else "error"


def problem_signature(result: Any) -> tuple[str, bool]:
    status = str(getattr(result, "status", ""))
    stores = getattr(result, "store_statuses", {}) or {}
    issues = sorted((str(slug), str(info.get("status") or "unknown"),
                     _error_kind(info.get("error")))
                    for slug, info in stores.items()
                    if str(info.get("status") or "unknown").lower() not in _HEALTHY)
    problem = status != "success"
    signature = [status, getattr(result, "error_stage", None), issues,
                 _error_kind(getattr(result, "error_message", None)) if problem and not issues else None]
    # Run id, product counts, timings and changing exception details are not incidents.
    return _digest(signature if problem else ["success"]), problem


def format_ops_message(result: Any, *, recovered: bool = False) -> str:
    if recovered:
        return "Laptop Monitor: работа восстановлена. Подробности — /status."
    stores = getattr(result, "store_statuses", {}) or {}
    broken = sorted(str(slug) for slug, info in stores.items()
                    if str(info.get("status") or "unknown").lower() not in _HEALTHY)
    status = html.escape(str(getattr(result, "status", "unknown")).upper())
    lines = [f"Laptop Monitor: проблема ({status})."]
    if broken:
        lines.append("Источники: " + ", ".join(html.escape(slug) for slug in broken))
    lines.append("Подробности и свежесть данных — /status.")
    return "\n".join(lines)


def _save(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def notify_transition(result: Any, *, state_path: Path | str | None = None) -> bool:
    """One attempt per recipient and state change, including recovery.

    Initial healthy state is silent. Claim is fsynced before network I/O. After
    uncertain delivery/crash no periodic retry is made and no ACK is fabricated.
    Corrupt state fails closed without resetting deduplication. The bounded file
    holds only hashes, timestamp and delivery states, never tokens or raw text.
    """
    if str(getattr(result, "status", "")) not in _STATES:
        return False
    try:
        instance = _digest(config.get_instance_id())
        path = Path(state_path) if state_path is not None else Path("data") / f"ops_notifications-{instance[:16]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with pipeline_lock(path.with_suffix(".lock")):
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            if state is not None and (state.get("schema") != 1 or state.get("instance") != instance
                                      or not isinstance(state.get("recipients"), dict)
                                      or not isinstance(state.get("problem"), bool)
                                      or not isinstance(state.get("signature"), str)
                                      or not isinstance(state.get("notify"), bool)):
                raise ValueError("invalid ops state")
            signature, problem = problem_signature(result)
            if state is None or state["signature"] != signature:
                was_problem = bool(state and state["problem"])
                state = {"schema": 1, "instance": instance, "signature": signature,
                         "problem": problem, "notify": problem or was_problem,
                         "recipients": {}, "updated_at": datetime.now(timezone.utc).isoformat()}
                _save(path, state)
            if not state["notify"]:
                return False
            token, default_chat = config.get_telegram_credentials()
            admins = config.get_telegram_admin_chat_ids() or {default_chat}
            text = format_ops_message(result, recovered=not problem)
            ok_any = False
            for chat_id in sorted(admins):
                recipient = _digest(str(chat_id))
                if recipient in state["recipients"]:
                    continue
                state["recipients"][recipient] = "unconfirmed"
                _save(path, state)
                sender = None
                try:
                    sender = TelegramSender(token, chat_id)
                    response = sender.send_message(text)
                    confirmed = bool(response.ok and response.message_id is not None)
                    state["recipients"][recipient] = "sent" if confirmed else "unconfirmed"
                    ok_any = ok_any or confirmed
                    if not confirmed:
                        logger.warning("Ops transition delivery unconfirmed; inspect status")
                except Exception as exc:
                    logger.warning("Ops transition delivery unconfirmed (%s)", type(exc).__name__)
                finally:
                    if sender is not None:
                        sender.close()
                _save(path, state)
            return ok_any
    except Exception as exc:
        logger.error("Ops transition unavailable (%s); no periodic resend", type(exc).__name__)
        return False
