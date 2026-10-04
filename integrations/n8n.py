from __future__ import annotations

"""n8n webhook integration (best-effort OPS layer)."""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Mapping, Sequence

import httpx

import config
from version import get_version

logger = logging.getLogger(__name__)

def safe_error_summary(text: str | None, *, limit: int = 160) -> str | None:
    """Short, safe error text for OPS webhooks / Telegram (no secrets/trace floods)."""
    if not text:
        return None
    cleaned = " ".join(str(text).split())
    lower = cleaned.lower()
    looks_secret = any(
        marker in lower
        for marker in (
            "api.telegram.org/bot",
            "bot_token",
            "webhook_secret",
            "authorization: ",
        )
    )
    if looks_secret:
        head = str(text).split(":", 1)[0].strip()
        cleaned = (
            head
            if head and " " not in head and len(head) < 80
            else "error redacted"
        )
    if len(cleaned) > limit:
        cleaned = cleaned[: max(0, limit - 1)].rstrip() + "…"
    return cleaned or None


def build_pipeline_completed_payload(
    *,
    run_id: int | None,
    status: str,
    started_at: str | None,
    finished_at: str | None,
    duration_seconds: float | None,
    store_statuses: Mapping[str, Mapping[str, Any]] | None,
    alerts_created: int | None,
    messages_sent: int | None,
    messages_failed: int | None,
    top_deals: Sequence[Mapping[str, Any]] | None = None,
    adapters_meta: Sequence[Mapping[str, Any]] | None = None,
    error_summary: str | None = None,
) -> dict[str, Any]:
    stores_out: list[dict[str, Any]] = []
    meta_by_slug = {str(m.get("slug")): m for m in (adapters_meta or [])}
    for slug, info in sorted((store_statuses or {}).items()):
        meta = meta_by_slug.get(slug) or {}
        store_err = safe_error_summary(info.get("error"))
        entry: dict[str, Any] = {
            "slug": slug,
            "display_name": meta.get("display_name") or slug,
            "status": info.get("status"),
            "products": info.get("products_count"),
            "region": meta.get("region"),
            "collection_mode": meta.get("collection_mode"),
            "reliability": meta.get("reliability"),
        }
        if store_err:
            entry["error"] = store_err
        stores_out.append(entry)
    tops: list[dict[str, Any]] = []
    for i, deal in enumerate(top_deals or [], start=1):
        tops.append(
            {
                "rank": i,
                "name": deal.get("name") or deal.get("cluster_name"),
                "store": deal.get("store"),
                "price": deal.get("price"),
                "score": deal.get("score"),
                "url": deal.get("url"),
            }
        )
    payload: dict[str, Any] = {
        "event": "pipeline.completed",
        "app_version": get_version(),
        "instance_id": config.get_instance_id(),
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "region": config.get_monitor_region(),
        "stores": stores_out,
        "alerts_created": alerts_created,
        "messages_sent": messages_sent,
        "messages_failed": messages_failed,
        "top_deals": tops,
    }
    summary = safe_error_summary(error_summary)
    if summary:
        payload["error_summary"] = summary
    return payload


def format_failure_alert_message(payload: Mapping[str, Any]) -> str | None:
    """
    Build Telegram text for PARTIAL/FAILED pipeline.completed events.
    Returns None for success / unknown (no failure notification).
    """
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"partial", "failed"}:
        return None

    finished = payload.get("finished_at") or payload.get("started_at")
    time_txt = _format_moscow(finished)
    instance = str(payload.get("instance_id") or "unknown")
    run_id = payload.get("run_id")
    stores = list(payload.get("stores") or [])
    sent = payload.get("messages_sent")
    failed = payload.get("messages_failed")

    problem: list[str] = []
    working: list[str] = []
    for store in stores:
        name = str(store.get("display_name") or store.get("slug") or "store")
        st = str(store.get("status") or "").lower()
        if st in {"ok", "success"}:
            working.append(f"• {name} — OK")
        else:
            reason = safe_error_summary(store.get("error")) or st or "failed"
            problem.append(f"• {name} — FAILED: {reason}")

    if status == "partial":
        lines = [
            "⚠️ Laptop Monitor — PARTIAL",
            "",
            f"Время: {time_txt}",
            f"Instance: {instance}",
            f"Run: {run_id}",
            "",
            "Проблемные магазины:",
        ]
        lines.extend(problem or ["• (не указаны)"])
        lines.extend(["", "Работают:"])
        lines.extend(working or ["• (нет)"])
        lines.extend(
            [
                "",
                "Telegram:",
                f"sent: {sent if sent is not None else '—'}",
                f"failed: {failed if failed is not None else '—'}",
            ]
        )
        return "\n".join(lines)

    summary = safe_error_summary(payload.get("error_summary")) or "pipeline failed"
    lines = [
        "🔴 Laptop Monitor — FAILED",
        "",
        f"Время: {time_txt}",
        f"Instance: {instance}",
        f"Run: {run_id}",
        "",
        "Причина:",
        summary,
        "",
        "Stores:",
    ]
    if stores:
        for store in stores:
            name = str(store.get("display_name") or store.get("slug") or "store")
            st = str(store.get("status") or "unknown").upper()
            err = safe_error_summary(store.get("error"))
            lines.append(f"• {name} — {st}" + (f": {err}" if err else ""))
    else:
        lines.append("• (нет данных)")
    return "\n".join(lines)


def _format_moscow(iso_value: Any) -> str:
    from datetime import datetime, timedelta, timezone

    if not iso_value:
        return "—"
    try:
        text = str(iso_value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        msk = timezone(timedelta(hours=3))
        return dt.astimezone(msk).strftime("%Y-%m-%d %H:%M:%S MSK")
    except ValueError:
        return str(iso_value)


def sign_webhook(body: bytes, secret: str, *, timestamp: str | None = None) -> tuple[str, str]:
    ts = timestamp or str(int(time.time()))
    message = ts.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return ts, digest


def verify_webhook_signature(
    body: bytes,
    secret: str,
    timestamp: str,
    signature: str,
    *,
    max_skew_seconds: int = 300,
    now: int | None = None,
) -> bool:
    """Example verifier for n8n / docs (not used by sender)."""
    try:
        ts_i = int(timestamp)
    except ValueError:
        return False
    now_i = int(time.time() if now is None else now)
    if abs(now_i - ts_i) > max_skew_seconds:
        return False
    _, expected = sign_webhook(body, secret, timestamp=timestamp)
    return hmac.compare_digest(expected, signature)


def post_pipeline_webhook(
    payload: Mapping[str, Any],
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """
    Best-effort POST. Never raises for transport/API failures.
    Returns diagnostic dict.
    """
    url, secret, timeout = config.get_n8n_webhook_config()
    if not url:
        return {"enabled": False, "ok": True, "skipped": True}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"laptop-monitor/{get_version()}",
    }
    if secret:
        ts, sig = sign_webhook(body, secret)
        headers["X-Laptop-Monitor-Timestamp"] = ts
        headers["X-Laptop-Monitor-Signature"] = sig
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(url, content=body, headers=headers)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning(
                "n8n webhook HTTP %s (best-effort, pipeline unchanged)",
                resp.status_code,
            )
        return {
            "enabled": True,
            "ok": ok,
            "status_code": resp.status_code,
            "skipped": False,
        }
    except httpx.TimeoutException:
        logger.warning("n8n webhook timeout (best-effort, pipeline unchanged)")
        return {"enabled": True, "ok": False, "error": "timeout", "skipped": False}
    except httpx.HTTPError as exc:
        logger.warning(
            "n8n webhook network error %s (best-effort, pipeline unchanged)",
            type(exc).__name__,
        )
        return {
            "enabled": True,
            "ok": False,
            "error": type(exc).__name__,
            "skipped": False,
        }
    finally:
        if owns:
            client.close()
