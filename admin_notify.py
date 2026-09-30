from __future__ import annotations

"""Operational Telegram notifications for pipeline PARTIAL/FAILED (not price alerts)."""

import logging
from typing import Any

import config
from storage import PIPELINE_STATUS_FAILED, PIPELINE_STATUS_PARTIAL
from telegram_sender import TelegramSender
from version import APP_NAME, get_version

logger = logging.getLogger(__name__)


def format_ops_message(result: Any) -> str:
    lines = [
        f"{APP_NAME} {get_version()}",
        f"Pipeline {str(getattr(result, 'status', '?')).upper()}",
        f"Instance: {config.get_instance_id()}",
    ]
    regard = getattr(result, "regard_status", None)
    andpro = getattr(result, "andpro_status", None)
    if regard:
        lines.append(f"Regard: {regard}")
    if andpro:
        lines.append(f"ANDPRO: {andpro}")
    if getattr(result, "error_stage", None):
        lines.append(f"Stage: {result.error_stage}")
    if getattr(result, "error_message", None):
        lines.append(f"Error: {str(result.error_message)[:400]}")
    if getattr(result, "duration_seconds", None) is not None:
        lines.append(f"Duration: {result.duration_seconds}s")
    return "\n".join(lines)


def maybe_notify_ops_failure(result: Any) -> bool:
    """
    Send short admin ops alert for PARTIAL/FAILED.

    Returns True if a message was sent successfully.
    Never raises; failures stay in logs only.
    """
    status = getattr(result, "status", None)
    if status not in {PIPELINE_STATUS_PARTIAL, PIPELINE_STATUS_FAILED}:
        return False
    try:
        token, default_chat = config.get_telegram_credentials()
        admins = config.get_telegram_admin_chat_ids() or {default_chat}
        text = format_ops_message(result)
        ok_any = False
        for chat_id in admins:
            sender = TelegramSender(token, chat_id)
            try:
                res = sender.send_message(text)
                ok_any = ok_any or bool(res.ok)
                if not res.ok:
                    logger.error(
                        "Ops admin notify failed chat=%s error=%s",
                        chat_id,
                        res.error,
                    )
            finally:
                sender.close()
        return ok_any
    except Exception:
        logger.exception("Ops admin notify unavailable")
        return False
