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


def maybe_notify_ops_failure(result: Any, *, state_path=None) -> bool:
    """
    Send one operational transition alert, including recovery.

    Retained entry point; durable claims live in ops_notifications.
    Returns True only for a confirmed message ID, never a synthetic ACK.
    Never raises; failures stay in logs only.
    """
    from ops_notifications import notify_transition

    return notify_transition(result, state_path=state_path)
