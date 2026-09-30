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
) -> dict[str, Any]:
    stores_out: list[dict[str, Any]] = []
    meta_by_slug = {str(m.get("slug")): m for m in (adapters_meta or [])}
    for slug, info in sorted((store_statuses or {}).items()):
        meta = meta_by_slug.get(slug) or {}
        stores_out.append(
            {
                "slug": slug,
                "display_name": meta.get("display_name") or slug,
                "status": info.get("status"),
                "products": info.get("products_count"),
                "region": meta.get("region"),
                "collection_mode": meta.get("collection_mode"),
                "reliability": meta.get("reliability"),
            }
        )
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
    return {
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
