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
        entry = {
            "rank": i,
            "name": deal.get("name") or deal.get("cluster_name"),
            "store": deal.get("store"),
            "price": deal.get("price"),
            "score": deal.get("score"),
            "url": deal.get("url"),
        }
        if deal.get("confidence") is not None:
            entry["confidence"] = deal.get("confidence")
        tops.append(entry)
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


# --- Daily Digest v1 (pipeline.completed summaries only) ---

DIGEST_INSTANCE_ID = "vps-prod"
DIGEST_HISTORY_MAX_AGE_HOURS = 48
DIGEST_HISTORY_MAX_RECORDS = 100
DIGEST_WINDOW_HOURS = 24
DIGEST_TOP_LIMIT = 5
DIGEST_TOP_URL_LIMIT = 3


def _parse_iso(value: Any) -> Any:
    from datetime import datetime, timezone

    if value is None:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _moscow_date_key(now: Any = None) -> str:
    from datetime import datetime, timedelta, timezone

    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    msk = timezone(timedelta(hours=3))
    return when.astimezone(msk).strftime("%Y-%m-%d")


def make_run_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compact safe summary for rolling digest history (no secrets/raw body)."""
    stores_out: list[dict[str, Any]] = []
    for store in list(payload.get("stores") or []):
        if not isinstance(store, Mapping):
            continue
        entry = {
            "slug": store.get("slug"),
            "display_name": store.get("display_name") or store.get("slug"),
            "status": store.get("status"),
        }
        # Digest must not carry long errors.
        stores_out.append(entry)
    tops_out: list[dict[str, Any]] = []
    for deal in list(payload.get("top_deals") or []):
        if not isinstance(deal, Mapping):
            continue
        entry = {
            "rank": deal.get("rank"),
            "name": deal.get("name"),
            "store": deal.get("store"),
            "price": deal.get("price"),
            "score": deal.get("score"),
            "url": deal.get("url"),
            "saving": deal.get("saving") or deal.get("cross_store_saving"),
        }
        if deal.get("confidence") is not None:
            entry["confidence"] = deal.get("confidence")
        tops_out.append(entry)
    return {
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "instance_id": payload.get("instance_id"),
        "alerts_created": payload.get("alerts_created"),
        "messages_sent": payload.get("messages_sent"),
        "messages_failed": payload.get("messages_failed"),
        "stores": stores_out,
        "top_deals": tops_out,
    }


def prune_run_history(
    history: Sequence[Mapping[str, Any]],
    *,
    now: Any = None,
    max_age_hours: int = DIGEST_HISTORY_MAX_AGE_HOURS,
    max_records: int = DIGEST_HISTORY_MAX_RECORDS,
) -> list[dict[str, Any]]:
    """Keep last max_age_hours and at most max_records (newest first)."""
    from datetime import datetime, timedelta, timezone

    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    cutoff = when - timedelta(hours=max_age_hours)
    kept: list[tuple[Any, dict[str, Any]]] = []
    for raw in history:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        ts = _parse_iso(item.get("finished_at") or item.get("started_at"))
        if ts is None:
            continue
        if ts < cutoff:
            continue
        kept.append((ts, item))
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in kept[:max_records]]


def archive_run_summary(
    history: Sequence[Mapping[str, Any]] | None,
    payload: Mapping[str, Any],
    *,
    now: Any = None,
) -> list[dict[str, Any]]:
    """Upsert summary by run_id, then prune retention window."""
    summary = make_run_summary(payload)
    run_key = str(summary.get("run_id") if summary.get("run_id") is not None else "")
    out: list[dict[str, Any]] = []
    replaced = False
    for raw in history or []:
        if not isinstance(raw, Mapping):
            continue
        existing = dict(raw)
        if run_key and str(existing.get("run_id")) == run_key:
            out.append(summary)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(summary)
    return prune_run_history(out, now=now)


def filter_history_window(
    history: Sequence[Mapping[str, Any]],
    *,
    now: Any = None,
    window_hours: int = DIGEST_WINDOW_HOURS,
    instance_id: str = DIGEST_INSTANCE_ID,
) -> list[dict[str, Any]]:
    from datetime import datetime, timedelta, timezone

    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    cutoff = when - timedelta(hours=window_hours)
    out: list[dict[str, Any]] = []
    for raw in history:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("instance_id") or "") != instance_id:
            continue
        ts = _parse_iso(raw.get("finished_at") or raw.get("started_at"))
        if ts is None or ts < cutoff:
            continue
        out.append(dict(raw))
    out.sort(
        key=lambda item: _parse_iso(item.get("finished_at") or item.get("started_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out


def _price_in_cap(price: Any, *, max_price: int | None = None) -> bool:
    cap = config.MAX_TRACKED_PRICE_RUB if max_price is None else max_price
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return value <= float(cap)


def _format_price_rub(price: Any) -> str:
    try:
        value = int(round(float(price)))
    except (TypeError, ValueError):
        return str(price)
    return f"{value:,}".replace(",", " ") + " ₽"


def select_digest_top_deals(
    history_24h: Sequence[Mapping[str, Any]],
    *,
    limit: int = DIGEST_TOP_LIMIT,
) -> list[dict[str, Any]]:
    """TOP from latest success/partial run in window; fail-safe price cap."""
    source = None
    for item in history_24h:
        st = str(item.get("status") or "").lower()
        if st in {"success", "partial"}:
            source = item
            break
    if source is None:
        return []
    deals: list[dict[str, Any]] = []
    for deal in list(source.get("top_deals") or []):
        if not isinstance(deal, Mapping):
            continue
        if not _price_in_cap(deal.get("price")):
            continue
        deals.append(dict(deal))
        if len(deals) >= limit:
            break
    return deals


def compute_digest_metrics(history_24h: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    success = partial = failed = 0
    alerts = sent = msg_failed = 0
    for item in history_24h:
        st = str(item.get("status") or "").lower()
        if st == "success":
            success += 1
        elif st == "partial":
            partial += 1
        elif st == "failed":
            failed += 1
        try:
            alerts += int(item.get("alerts_created") or 0)
        except (TypeError, ValueError):
            pass
        try:
            sent += int(item.get("messages_sent") or 0)
        except (TypeError, ValueError):
            pass
        try:
            msg_failed += int(item.get("messages_failed") or 0)
        except (TypeError, ValueError):
            pass
    latest = history_24h[0] if history_24h else None
    return {
        "total_runs": len(history_24h),
        "success": success,
        "partial": partial,
        "failed": failed,
        "alerts_created": alerts,
        "messages_sent": sent,
        "messages_failed": msg_failed,
        "latest": latest,
    }


def format_daily_digest_message(
    history: Sequence[Mapping[str, Any]],
    *,
    now: Any = None,
    test_mode: bool = False,
    top_limit: int = DIGEST_TOP_LIMIT,
) -> str:
    """Build Daily Digest Telegram text from archived pipeline summaries."""
    window = filter_history_window(history, now=now)
    title = (
        "🧪 Laptop Monitor — Daily Digest TEST"
        if test_mode
        else "📊 Laptop Monitor — Daily Digest"
    )
    if not window:
        return (
            f"{title}\n"
            "\n"
            "За последние 24 часа запусков не было."
        )

    metrics = compute_digest_metrics(window)
    latest = metrics["latest"] or {}
    latest_status = str(latest.get("status") or "unknown").upper()
    latest_time = _format_moscow(latest.get("finished_at") or latest.get("started_at"))

    store_lines: list[str] = []
    for store in list(latest.get("stores") or []):
        if not isinstance(store, Mapping):
            continue
        name = str(store.get("display_name") or store.get("slug") or "store")
        st = str(store.get("status") or "").lower()
        if st in {"ok", "success"}:
            store_lines.append(f"✅ {name}")
        else:
            store_lines.append(f"⚠️ {name} — FAILED")

    tops = select_digest_top_deals(window, limit=top_limit)
    # Telegram hard limit ~4096; shrink TOP if needed.
    top_block = _format_top_block(tops, url_limit=DIGEST_TOP_URL_LIMIT)
    lines = [
        title,
        "Период: последние 24 часа",
        "",
        f"Запуски: {metrics['total_runs']}",
        f"✅ SUCCESS: {metrics['success']}",
        f"⚠️ PARTIAL: {metrics['partial']}",
        f"🔴 FAILED: {metrics['failed']}",
        "",
        f"Ценовых событий: {metrics['alerts_created']}",
        f"Telegram: {metrics['messages_sent']} sent / {metrics['messages_failed']} failed",
        "",
        "Магазины:",
    ]
    lines.extend(store_lines or ["• (нет данных)"])
    lines.extend(["", f"ТОП ДО {config.MAX_TRACKED_PRICE_RUB:,}".replace(",", " ") + " ₽:"])
    if top_block:
        lines.extend(top_block)
    else:
        lines.append("• (нет предложений в окне)")
    lines.extend(["", "Последний запуск:", f"{latest_time} — {latest_status}"])
    text = "\n".join(lines)
    if len(text) <= 3900:
        return text
    # Retry with fewer TOP rows / no URLs.
    tops3 = select_digest_top_deals(window, limit=3)
    top_block = _format_top_block(tops3, url_limit=0)
    lines = [
        title,
        "Период: последние 24 часа",
        "",
        f"Запуски: {metrics['total_runs']}",
        f"✅ SUCCESS: {metrics['success']}",
        f"⚠️ PARTIAL: {metrics['partial']}",
        f"🔴 FAILED: {metrics['failed']}",
        "",
        f"Ценовых событий: {metrics['alerts_created']}",
        f"Telegram: {metrics['messages_sent']} sent / {metrics['messages_failed']} failed",
        "",
        "Магазины:",
    ]
    lines.extend(store_lines or ["• (нет данных)"])
    lines.extend(["", "ТОП ДО 300 000 ₽:"])
    lines.extend(top_block or ["• (нет предложений в окне)"])
    lines.extend(["", "Последний запуск:", f"{latest_time} — {latest_status}"])
    return "\n".join(lines)


def _format_top_block(
    deals: Sequence[Mapping[str, Any]],
    *,
    url_limit: int,
) -> list[str]:
    lines: list[str] = []
    for i, deal in enumerate(deals, start=1):
        name = str(deal.get("name") or "—")
        store = str(deal.get("store") or "—")
        price = _format_price_rub(deal.get("price"))
        line = f"{i}. {name} — {price} — {store}"
        saving = deal.get("saving")
        try:
            if saving is not None and float(saving) > 0:
                line += f" (экономия {_format_price_rub(saving)})"
        except (TypeError, ValueError):
            pass
        lines.append(line)
        if i <= url_limit:
            url = deal.get("url")
            if url:
                lines.append(f"   {url}")
    return lines


def should_send_digest(
    *,
    last_sent_date: str | None,
    now: Any = None,
    test_mode: bool = False,
) -> tuple[bool, str]:
    """
    Production dedupe by MSK calendar date.
    Test mode never blocks and does not use production key.
    Returns (should_send, date_key).
    """
    date_key = _moscow_date_key(now)
    if test_mode:
        return True, date_key
    if last_sent_date and str(last_sent_date) == date_key:
        return False, date_key
    return True, date_key


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
