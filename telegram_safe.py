from __future__ import annotations

"""Sanitize Telegram / HTTP errors so bot tokens never appear in logs."""

import re
from typing import Any

# api.telegram.org/bot<token>/...
_BOT_URL_RE = re.compile(
    r"https?://api\.telegram\.org/bot[^/\s\"']+",
    re.IGNORECASE,
)
# common token shape 123456:AA...
_TOKEN_SHAPE_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def redact_secrets(text: str | None) -> str:
    if not text:
        return ""
    out = _BOT_URL_RE.sub("https://api.telegram.org/bot[REDACTED]", str(text))
    out = _TOKEN_SHAPE_RE.sub("[REDACTED_TOKEN]", out)
    return out


def safe_exc_message(exc: BaseException) -> str:
    """Format exception without token-bearing URLs."""
    name = exc.__class__.__name__
    raw = redact_secrets(str(exc))
    return f"{name}: {raw}"[:500]


def telegram_http_error_summary(
    *,
    method: str,
    status_code: int | None = None,
    api_ok: bool | None = None,
    description: str | None = None,
) -> str:
    parts = [f"Telegram {method}"]
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    if api_ok is False:
        parts.append("API ok=false")
    if description:
        parts.append(redact_secrets(description)[:200])
    return " ".join(parts)


def parse_telegram_response(data: Any) -> tuple[bool, str | None]:
    if not isinstance(data, dict):
        return False, "invalid JSON payload"
    if data.get("ok"):
        return True, None
    desc = data.get("description") or "unknown Telegram error"
    return False, redact_secrets(str(desc))
