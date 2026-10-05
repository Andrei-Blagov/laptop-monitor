from __future__ import annotations

"""Stable Thailand store error codes. Technical detail stays out of Telegram."""

import re

BLOCKED = "BLOCKED"
CHALLENGE = "CHALLENGE"
NO_RESULTS = "NO_RESULTS"
TIMEOUT = "TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
PARSE_ERROR = "PARSE_ERROR"
HTTP_ERROR = "HTTP_ERROR"
UNSUPPORTED = "UNSUPPORTED"

ERROR_CODES = frozenset(
    {
        BLOCKED,
        CHALLENGE,
        NO_RESULTS,
        TIMEOUT,
        NETWORK_ERROR,
        PARSE_ERROR,
        HTTP_ERROR,
        UNSUPPORTED,
    }
)


def classify_store_failure(
    detail: str | None = None,
    *,
    status_code: int | None = None,
) -> tuple[str, str]:
    """Return (error_code, technical_detail). Never raises."""
    text = (detail or "").strip()
    low = text.lower()
    tech = text or (f"HTTP_{status_code}" if status_code else "unknown")
    if any(k in low for k in ("challenge", "just a moment", "cf-browser", "captcha", "sufei")):
        return CHALLENGE, tech
    if status_code in {401, 403} or any(
        k in low for k in ("403", "401", "429", "blocked", "cloudflare", "access denied", "forbidden")
    ):
        return BLOCKED, tech if text else f"HTTP_{status_code or 403}"
    if status_code is not None and status_code >= 400:
        return HTTP_ERROR, f"HTTP_{status_code}"
    if "timeout" in low or "timed out" in low:
        return TIMEOUT, tech
    if any(k in low for k in ("json", "parse", "decode")):
        return PARSE_ERROR, tech
    if any(k in low for k in ("connection", "network", "name or service", "nodename", "unreachable")):
        return NETWORK_ERROR, tech
    if "unsupported" in low:
        return UNSUPPORTED, tech
    return HTTP_ERROR, tech


def html_block_code(html: str | None) -> str | None:
    """CHALLENGE/BLOCKED for an interstitial page, not a word inside a real catalog."""
    if not html:
        return None
    title = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip().lower()
    if any(k in title for k in ("just a moment", "attention required", "access denied", "403", "captcha")):
        return BLOCKED if "access denied" in title or "403" in title else CHALLENGE
    low = html.lower()
    if len(html) < 20_000 and any(
        k in low for k in ("cf-browser-verification", "checking your browser", "just a moment", "sufei")
    ):
        return BLOCKED if "sufei" in low else CHALLENGE
    return None
