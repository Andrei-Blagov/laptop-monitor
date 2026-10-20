from __future__ import annotations

"""
Telegram control bot (long polling).

Admin-only buttons: run pipeline / top deals / price history / status / version.
Does NOT run schema migrations — require migrate_db first.
"""

import hashlib
import html
import json
import logging
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

import config
from deal_ranking import (
    canonical_gpu,
    format_top_deals_message,
)
from pipeline_lock import DEFAULT_LOCK_PATH, read_lock_info
from run_pipeline import (
    EXIT_LOCKED,
    PipelineResult,
    run_pipeline,
    run_status as pipeline_status,
)
from model_price_history import (
    CALLBACK_LIST,
    CALLBACK_MENU,
    CALLBACK_MODEL_PREFIX,
    CALLBACK_PERIODS_PREFIX,
    CALLBACK_REFRESH_PREFIX,
    build_history_picker_items,
    build_history_picker_keyboard,
    build_model_history_report,
    format_history_picker_message,
    format_model_history_message,
    history_nav_keyboard,
    parse_model_callback,
)
from price_history_chart import (
    CALLBACK_CHART_PREFIX,
    build_price_history_chart,
    chart_period_keyboard,
    chart_result_keyboard,
    parse_chart_callback,
    parse_periods_callback,
)
from store_freshness import freshness_age_minutes, get_fresh_store_slugs
from storage import (
    DEFAULT_DB_PATH,
    count_alert_events,
    count_unsent_alert_events,
    get_delivery_stats,
    get_latest_store_runs_readonly,
    get_store_runs_for_pipeline,
    open_db_readonly,
    require_schema_ready,
)
from stores.registry import all_adapters
from telegram_safe import (
    parse_telegram_response,
    redact_secrets,
    safe_exc_message,
    telegram_http_error_summary,
)
from version import APP_NAME, get_version
from manual_run import (MAX_DELIVERY_ATTEMPTS, claim_fallback, mark_notified,
                        read_request, reconcile, record_delivery_attempt,
                        request_run, set_message_id)

logger = logging.getLogger(__name__)

BTN_RUN = "ctrl:run"
BTN_TOP = "ctrl:top"
BTN_STATUS = "ctrl:status"
BTN_VERSION = "ctrl:version"
BTN_HISTORY = "ctrl:hist"
BTN_MARKETS = "ctrl:markets"
BTN_TH_SCAN = "ctrl:th_scan"
BTN_TH_COMPARE = "ctrl:th_cmp"
BTN_RU_TOP = "ctrl:ru_top"
BTN_TH_FIND = "ctrl:th_find"
BTN_RECOMMEND = "ctrl:recommend"
BTN_PERSONAL = "ctrl:ptop"
BTN_PERSONAL_REFRESH = "ctrl:ptop_go"
CALLBACK_BUY = "buy:"
CALLBACK_MODEL_PICK = "tm:"
CALLBACK_RECOMMEND = "rq:"

# Canonical BotFather list. Names are lowercase, without a slash.
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Открыть главное меню"),
    ("menu", "Главное меню"),
    ("recommend", "Стоит ли покупать модель из TOP-10"),
    ("top", "TOP-10 России"),
    ("history", "История цен"),
    ("markets", "Россия и Таиланд"),
    ("mytop", "Избранное"),
    ("run", "Обновить цены"),
    ("status", "Статус системы"),
    ("version", "Версия бота"),
    ("help", "Помощь"),
)


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _is_admin(chat_id: str | int) -> bool:
    return str(chat_id) in config.get_telegram_admin_chat_ids()


def parse_bot_command(text: str) -> tuple[str, str] | None:
    """Return (command, args) for /name or /name@Bot, else None."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    head, _, rest = raw.partition(" ")
    name = head[1:].split("@", 1)[0].strip().lower()
    if not name or not name.replace("_", "").isalnum():
        return None
    return name, rest.strip()


def format_help_text() -> str:
    lines = ["<b>Laptop Monitor — команды</b>", ""]
    for name, description in BOT_COMMANDS:
        shown = description[:1].lower() + description[1:] if description else description
        lines.append(f"/{name} — {shown}")
    return "\n".join(lines)


def handle_text_message(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    text: str,
) -> None:
    """Slash commands are aliases of the existing inline handlers."""
    parsed = parse_bot_command(text)
    if parsed is None:
        return
    name, _args = parsed
    if name == "start":
        send_message(client, token, chat_id, f"<b>{APP_NAME}</b>\nВыберите действие:")
    elif name == "menu":
        send_message(client, token, chat_id, "Главное меню:")
    elif name == "help":
        send_message(client, token, chat_id, format_help_text())
    elif name == "run":
        handle_run(client, token, chat_id)
    elif name == "top":
        handle_top(client, token, chat_id)
    elif name == "history":
        handle_history_list(client, token, chat_id)
    elif name == "recommend":
        handle_recommendation_menu(client, token, chat_id)
    elif name == "markets":
        handle_markets_menu(client, token, chat_id)
    elif name == "mytop":
        handle_personal_top(client, token, chat_id)
    elif name == "status":
        send_message(client, token, chat_id, build_status_text())
    elif name == "version":
        send_message(client, token, chat_id, f"{APP_NAME} {get_version()}")
    else:
        send_message(
            client,
            token,
            chat_id,
            "Неизвестная команда. Используйте /help.",
        )


def _keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Обновить цены", "callback_data": BTN_RUN},
                {"text": "TOP-10 России", "callback_data": BTN_TOP},
            ],
            [
                {"text": "⭐ Избранное", "callback_data": BTN_PERSONAL},
            ],
            [
                {"text": "📈 История цен", "callback_data": BTN_HISTORY},
            ],
            [
                {"text": "Статус", "callback_data": BTN_STATUS},
                {"text": "Дополнительно", "callback_data": BTN_MARKETS},
            ],
        ]
    }


def _markets_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🇷🇺 Россия", "callback_data": BTN_RU_TOP},
            ],
            [
                {"text": "🇹🇭 Проверить Таиланд", "callback_data": BTN_TH_SCAN},
            ],
            [
                {"text": "🇷🇺🇹🇭 Сравнить сейчас", "callback_data": BTN_TH_COMPARE},
            ],
            [
                {"text": "🔎 Найти модель из ТОП РФ в Таиланде", "callback_data": BTN_TH_FIND},
            ],
            [
                {"text": "🏠 Меню", "callback_data": CALLBACK_MENU},
            ],
        ]
    }


def _post_telegram(
    client: httpx.Client,
    token: str,
    method: str,
    payload: dict[str, Any],
    *,
    timeout: float = 30.0,
    error_out: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    try:
        resp = client.post(_api(token, method), json=payload, timeout=timeout)
    except httpx.TimeoutException:
        if error_out is not None:
            error_out["kind"] = "retry"
        logger.error("Telegram %s timeout", method)
        return None
    except httpx.HTTPError as exc:
        if error_out is not None:
            error_out["kind"] = "retry"
        logger.error("%s", telegram_http_error_summary(method=method, description=safe_exc_message(exc)))
        return None
    try:
        data = resp.json()
    except ValueError:
        if error_out is not None:
            error_out["kind"] = "retry"
        logger.error(
            "%s",
            telegram_http_error_summary(
                method=method, status_code=_http_status(resp), description="invalid JSON"
            ),
        )
        return None
    except Exception:
        # Defensive: mock/test doubles may not provide json()
        data = None
    ok, err = parse_telegram_response(data)
    status = _http_status(resp)
    if (status is not None and status >= 400) or not ok:
        if error_out is not None:
            description = str(err or "").lower()
            if "message is not modified" in description:
                error_out["kind"] = "not_modified"
            elif status in {400, 401, 403, 404}:
                error_out["kind"] = "permanent"
            else:
                error_out["kind"] = "retry"
        logger.error(
            "%s",
            telegram_http_error_summary(
                method=method,
                status_code=status,
                api_ok=ok,
                description=err,
            ),
        )
        return None
    return data if isinstance(data, dict) else None


def _http_status(resp: Any) -> int | None:
    raw = getattr(resp, "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def send_message(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    text: str,
    *,
    with_keyboard: bool = True,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    elif with_keyboard:
        payload["reply_markup"] = _keyboard()
    return _post_telegram(client, token, "sendMessage", payload) is not None


def edit_message(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    error_out: dict[str, str] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _post_telegram(client, token, "editMessageText", payload, error_out=error_out) is not None


def answer_callback(
    client: httpx.Client, token: str, callback_id: str, text: str | None = None
) -> bool:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
    return (
        _post_telegram(client, token, "answerCallbackQuery", payload, timeout=15.0)
        is not None
    )


def send_photo(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    png_bytes: bytes,
    *,
    caption: str = "",
    reply_markup: dict[str, Any] | None = None,
    filename: str = "price_history.png",
    timeout: float = 30.0,
) -> bool:
    """Multipart sendPhoto; never logs token-bearing URLs."""
    url = _api(token, "sendPhoto")
    data: dict[str, str] = {
        "chat_id": str(chat_id),
        "caption": caption or "",
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"photo": (filename, png_bytes, "image/png")}
    try:
        resp = client.post(url, data=data, files=files, timeout=timeout)
    except httpx.TimeoutException:
        logger.error("Telegram sendPhoto timeout")
        return False
    except httpx.HTTPError as exc:
        logger.error(
            "%s",
            telegram_http_error_summary(
                method="sendPhoto", description=safe_exc_message(exc)
            ),
        )
        return False
    try:
        payload = resp.json()
    except ValueError:
        logger.error(
            "%s",
            telegram_http_error_summary(
                method="sendPhoto",
                status_code=_http_status(resp),
                description="invalid JSON",
            ),
        )
        return False
    ok, err = parse_telegram_response(payload)
    status = _http_status(resp)
    if (status is not None and status >= 400) or not ok:
        logger.error(
            "%s",
            telegram_http_error_summary(
                method="sendPhoto",
                status_code=status,
                api_ok=ok,
                description=err,
            ),
        )
        return False
    return True


def _reply_or_edit(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    text: str,
    *,
    message_id: int | None,
    reply_markup: dict[str, Any] | None,
) -> None:
    if message_id is not None:
        ok = edit_message(
            client,
            token,
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
        )
        if ok:
            return
    send_message(
        client,
        token,
        chat_id,
        text,
        with_keyboard=False,
        reply_markup=reply_markup,
    )


def handle_history_list(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    *,
    message_id: int | None = None,
) -> None:
    items = build_history_picker_items(DEFAULT_DB_PATH)
    text = format_history_picker_message(items)
    markup = build_history_picker_keyboard(items)
    _reply_or_edit(
        client,
        token,
        chat_id,
        text,
        message_id=message_id,
        reply_markup=markup,
    )


def handle_history_model(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    product_id: int,
    *,
    message_id: int | None = None,
) -> None:
    report = build_model_history_report(DEFAULT_DB_PATH, product_id)
    text = format_model_history_message(report)
    markup = history_nav_keyboard(product_id) if report.found else build_history_picker_keyboard(
        build_history_picker_items(DEFAULT_DB_PATH)
    )
    _reply_or_edit(
        client,
        token,
        chat_id,
        text,
        message_id=message_id,
        reply_markup=markup,
    )


def handle_chart_periods(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    product_id: int,
    *,
    message_id: int | None = None,
) -> None:
    report = build_model_history_report(DEFAULT_DB_PATH, product_id)
    if not report.found:
        handle_history_model(client, token, chat_id, product_id, message_id=message_id)
        return
    text = (
        f"📊 <b>График цены</b>\n\n"
        f"{report.name}\n\n"
        "Выберите период:"
    )
    _reply_or_edit(
        client,
        token,
        chat_id,
        text,
        message_id=message_id,
        reply_markup=chart_period_keyboard(product_id),
    )


def handle_chart_render(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    product_id: int,
    period: str,
) -> None:
    result = build_price_history_chart(DEFAULT_DB_PATH, product_id, period)  # type: ignore[arg-type]
    nav = chart_result_keyboard(product_id)
    if not result.ok or not result.png:
        send_message(
            client,
            token,
            chat_id,
            result.error or "Не удалось построить график. Текстовая история доступна.",
            with_keyboard=False,
            reply_markup=nav,
        )
        return
    ok = send_photo(
        client,
        token,
        chat_id,
        result.png,
        caption=result.caption,
        reply_markup=nav,
    )
    if not ok:
        send_message(
            client,
            token,
            chat_id,
            "Не удалось построить график. Текстовая история доступна.",
            with_keyboard=False,
            reply_markup=nav,
        )


def _deliver_status_readonly(db_path: Path | str) -> dict[str, int]:
    try:
        conn = open_db_readonly(db_path)
    except FileNotFoundError:
        return {"unsent_alert_events": 0, "alert_events_total": 0}
    try:
        delivery = get_delivery_stats(conn, channel=config.CHANNEL_TELEGRAM)
        unsent = count_unsent_alert_events(
            conn,
            channel=config.CHANNEL_TELEGRAM,
            destination=config.TELEGRAM_DESTINATION,
            max_attempts=int(config.MAX_DELIVERY_ATTEMPTS),
        )
        return {
            **delivery,
            "unsent_alert_events": unsent,
            "alert_events_total": count_alert_events(conn),
        }
    except Exception:
        return {"unsent_alert_events": 0, "alert_events_total": 0}
    finally:
        conn.close()


def format_store_summary_lines(
    *,
    store_rows: list[dict[str, Any]] | None = None,
    store_statuses: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Dynamic N-store summary lines (no Regard/ANDPRO hardcode)."""
    lines: list[str] = []
    by_slug: dict[str, dict[str, Any]] = {}
    if store_rows:
        for row in store_rows:
            by_slug[str(row["store"])] = row
    elif store_statuses:
        by_slug = {k: dict(v) for k, v in store_statuses.items()}

    adapters = {a.slug: a for a in all_adapters()}
    seen: set[str] = set()
    for slug in sorted(by_slug.keys()):
        seen.add(slug)
        row = by_slug[slug]
        name = adapters[slug].display_name if slug in adapters else slug
        status = str(row.get("status") or "?").upper()
        if status == "OK":
            status = "OK"
        count = row.get("products_count")
        if count is None and "products" in row:
            count = row.get("products")
        suffix = f" ({count})" if count is not None else ""
        lines.append(f"{name}: {status}{suffix}")
    for adapter in all_adapters():
        if adapter.slug in seen:
            continue
        flag = "disabled" if not adapter.enabled else "no runs"
        lines.append(f"{adapter.display_name}: {flag}")
    return lines


def format_pipeline_result_message(result: PipelineResult) -> str:
    lines = [
        f"<b>{APP_NAME}</b> {get_version()}",
        f"Instance: {config.get_instance_id()}",
        f"Pipeline: {result.status.upper()}",
        f"Duration: {result.duration_seconds}s",
        "",
        "Stores:",
    ]
    store_lines = format_store_summary_lines(store_statuses=result.store_statuses)
    lines.extend(f"  {line}" for line in store_lines)
    lines.append("")
    lines.append(f"New alerts: {result.alerts_created}")
    lines.append(
        f"Telegram: {result.messages_sent or 0}/{result.messages_failed or 0}"
    )
    if result.error_message:
        lines.append(f"Error: {redact_secrets(result.error_message)}")
    return "\n".join(lines)


def build_status_text(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    runs = pipeline_status(db_path, limit=1)
    last = runs[0] if runs else None
    latest = get_latest_store_runs_readonly(db_path)
    operation = read_request() if Path(db_path) == Path(DEFAULT_DB_PATH) else {}
    fresh_count = len(unique_russian_top(db_path, limit=0))
    sent_at = None
    try:
        conn = open_db_readonly(db_path)
        try:
            row = conn.execute("SELECT MAX(sent_at) AS sent_at FROM notification_deliveries "
                               "WHERE channel = ? AND status = 'sent'", (config.CHANNEL_TELEGRAM,)).fetchone()
            sent_at = row["sent_at"] if row else None
        finally:
            conn.close()
    except Exception:
        pass
    lines = [
        f"<b>{APP_NAME} {get_version()}</b>",
        f"Подходящих моделей сейчас: {fresh_count}",
        f"Последний российский сбор: {last.get('started_at') if last else 'нет'}",
        f"Итог сбора: {last.get('status') if last else 'нет'}",
        f"Ручная проверка: {operation.get('status') or 'нет'}"
        + (f" (№ {operation.get('id', '')[:8]})" if operation.get('id') else ""),
        f"Последняя успешная отправка: {sent_at or 'нет'}",
    ]
    return "\n".join(lines)


def build_top_parts(db_path: Path | str = DEFAULT_DB_PATH, *, deals=None) -> list[str]:
    """Render the same fresh ranked list used for model selection and BUY."""
    from deal_ranking import effective_deal_score

    if deals is None:
        deals = unique_russian_top(db_path, limit=10)
    if not deals:
        return ["<b>TOP-10 России</b>\nПодходящих свежих предложений сейчас нет."]
    header = f"<b>TOP-10 России — {len(deals)} моделей</b>"
    parts: list[str] = []
    current = header
    for index, deal in enumerate(deals, 1):
        raw_time = (deal.offer or {}).get("price_checked_at")
        try:
            checked = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            clock = checked.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M МСК")
        except (ValueError, TypeError):
            clock = "время неизвестно"
        name = html.escape(html.unescape(deal.cluster_name or "Модель"), quote=False)
        specs = f"{deal.gpu} · {deal.cpu or 'CPU не подтверждён'} · {deal.ram_gb} ГБ RAM · {deal.ssd_gb} ГБ SSD · {deal.screen_inch:g}″ {deal.screen_resolution}"
        price = f"{int(deal.price):,}".replace(",", " ")
        card = (f"<b>{index}. {name}</b>\n{html.escape(specs)}\n"
                f"{price} ₽ · {html.escape(deal.store)} · оценка {effective_deal_score(deal):g}/100\n"
                f"Проверено: {clock}\n{html.escape(deal.url or '')}")
        if len(current) + len(card) + 2 > 3500:
            parts.append(current)
            current = header
        current += "\n\n" + card
    parts.append(current)
    return parts


def handle_top(client: httpx.Client, token: str, chat_id: str | int,
               *, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    deals = unique_russian_top(db_path, limit=10)
    parts = build_top_parts(db_path, deals=deals)
    for index, part in enumerate(parts):
        markup = buy_picker_keyboard(deals) if deals and index == len(parts) - 1 else None
        send_message(client, token, chat_id, part, with_keyboard=False, reply_markup=markup)


def build_top_text(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    return "\n\n".join(build_top_parts(db_path))


def _personal_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🏠 Меню", "callback_data": CALLBACK_MENU}],
        ]
    }


def handle_personal_top(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    *,
    message_id: int | None = None,
) -> None:
    from personal_top import exact_skus, product_codes, series_product_candidates
    from product_identity import normalize_identifier

    deals = unique_russian_top(DEFAULT_DB_PATH, limit=0)
    lines = ["<b>⭐ Избранное — Россия</b>"]
    for model in config.PERSONAL_TOP3:
        codes = exact_skus(model)
        deal = next((item for item in deals if codes & (
            product_codes({"name": item.cluster_name, "sku": item.offer.get("sku")})
            | {normalize_identifier(str(item.offer.get("mpn") or ""))}
        )), None)
        series_variant = None
        if deal is None and model.get("series_codes"):
            for item in deals:
                row = {"name": item.offer.get("name") or item.cluster_name,
                       "sku": item.offer.get("sku"), "store": item.store,
                       "external_id": item.offer.get("external_id"),
                       "available": True, "price": item.price, "url": item.url}
                key = (str(row["store"]), str(row["external_id"] or ""))
                specs = {key: {"cpu": item.cpu, "gpu": item.gpu, "ram_gb": item.ram_gb,
                               "ssd_gb": item.ssd_gb, "screen_inch": item.screen_inch,
                               "screen_resolution": item.screen_resolution,
                               "gpu_vram_gb": item.gpu_vram_gb}}
                if series_product_candidates(model, [row], specs_by_key=specs):
                    deal = item
                    series_variant = str(row["sku"] or row["name"])
                    break
        lines.append("")
        lines.append(f"<b>{html.escape(str(model['model']))}</b>")
        if deal is None:
            lines.append("Свежей подтверждённой цены и наличия в России нет. Старые данные не считаются текущими.")
            continue
        if series_variant:
            lines.append(f"Вариант серии: {html.escape(series_variant)}")
        lines.append(f"В наличии · {int(deal.price):,} ₽ · {html.escape(deal.store)}".replace(",", " "))
        lines.append(html.escape(deal.url or ""))
    text = "\n".join(lines)
    _reply_or_edit(
        client,
        token,
        chat_id,
        text,
        message_id=message_id,
        reply_markup=_personal_keyboard(),
    )


def handle_personal_refresh(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
) -> None:
    if config.stage22_market_telegram_enabled():
        from thailand.market_telegram import enqueue_view_refresh

        enqueue_view_refresh(chat_id=chat_id, view="personal_top3", trigger="personal")
        send_message(
            client, token, chat_id,
            "Обновление Таиланда поставлено в очередь. Сбор идёт общим проходом, без отдельного collect.",
            with_keyboard=False,
        )
        return
    from personal_top import enqueue_personal_job, specs_for_products
    from storage import DEFAULT_DB_PATH, get_all_products_readonly, get_latest_store_runs_readonly
    from store_freshness import get_fresh_store_slugs

    products = get_all_products_readonly()
    result = enqueue_personal_job(
        manual=True,
        chat_id=chat_id,
        products=products,
        fresh_stores=get_fresh_store_slugs(get_latest_store_runs_readonly(DEFAULT_DB_PATH)),
        specs_by_key=specs_for_products(products),
    )
    if result.get("ok"):
        text = "Проверка Таиланда для TOP-3 поставлена в очередь. Магазины опрашиваются одним проходом."
    elif result.get("error") == "too_soon":
        text = "Свежая проверка уже есть. Повторный опрос магазинов пока не запускается."
    elif result.get("error") == "already_queued":
        text = "Проверка TOP-3 уже стоит в очереди."
    else:
        text = "Не удалось поставить проверку TOP-3 в очередь."
    send_message(client, token, chat_id, text)
    handle_personal_top(client, token, chat_id)


def handle_markets_menu(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    *,
    message_id: int | None = None,
) -> None:
    _reply_or_edit(
        client,
        token,
        chat_id,
        "<b>🌍 Рынки</b>\nВыберите действие:",
        message_id=message_id,
        reply_markup=_markets_keyboard(),
    )


def model_callback_token(model, *, length: int = 16) -> str:
    if isinstance(model, str):
        identity = model
    else:
        offer = model.offer or {}
        code = str(model.cluster_key or offer.get("sku") or offer.get("mpn") or "")
        if not code:
            code = f"{model.store}:{offer.get('external_id') or model.url or ''}"
        elif code.startswith("id:"):
            code = f"{model.store}:{code}"
        identity = json.dumps(tuple(str(value or "") for value in (
            code, model.gpu, model.cpu, model.ram_gb, model.ssd_gb,
            model.screen_inch, model.screen_resolution,
        )))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:length]


def _confirmed_russian_quotes(db_path: Path | str, now: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the exact per-offer evidence gate used by the Russian daily digest."""
    from market_contracts import russian_snapshot
    from market_snapshot import load_russian_market_snapshot
    from thai_catalog import russian_offer_views
    from thai_market_ranking import normalized_field
    from product_identity import normalize_identifier

    try:
        raw = load_russian_market_snapshot(Path(db_path).resolve().parent / "russian_market")
        eligible, _information = russian_offer_views(raw, now=now)
        parsed = russian_snapshot(raw)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("Russian snapshot unavailable for Telegram TOP: %s", type(exc).__name__)
        return {}
    accepted = {row["best_offer_id"] for row in eligible}
    records = {row["offer_id"]: row for row in raw.get("offers", ())}
    quotes = {}
    ambiguous: set[tuple[str, str]] = set()
    for offer in parsed.offers:
        if offer.offer_id not in accepted:
            continue
        record = records.get(offer.offer_id, {})
        sku_ids = {normalize_identifier(value) for value in record.get("sku_candidates") or ()}
        mpn_ids = {normalize_identifier(value) for value in record.get("mpn_candidates") or ()}
        key = (offer.source, offer.external_id)
        if key in quotes:
            # One store listing cannot establish two simultaneous configurations.
            quotes.pop(key)
            ambiguous.add(key)
            continue
        if key in ambiguous:
            continue
        quotes[key] = {
            "price": int(offer.price), "url": offer.url,
            "name": offer.name, "sku_ids": sku_ids - {None}, "mpn_ids": mpn_ids - {None},
            "nominal_ssd_gb": offer.nominal_ssd_gb,
            "price_checked_at": offer.price_checked_at.isoformat(),
            "availability_checked_at": offer.availability_checked_at.isoformat(),
            **{field: normalized_field(offer, field, now) for field in
               ("gpu", "cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")},
        }
    return quotes


def _same_confirmed_configuration(deal, quote: dict[str, Any]) -> bool:
    """Reject a reused listing ID when its current variant differs from the snapshot."""
    from product_identity import normalize_cpu, normalize_identifier, normalize_resolution

    sku = normalize_identifier((deal.offer or {}).get("sku"))
    mpn = normalize_identifier((deal.offer or {}).get("mpn"))
    sku_ids = quote["sku_ids"]
    if sku_ids:
        if len(sku_ids) != 1 or sku not in sku_ids:
            return False
    elif not mpn or mpn not in quote["mpn_ids"]:
        return False
    if (re.search(r"DESKTOP|НАСТОЛЬН", str(deal.gpu), re.I) or
            canonical_gpu(deal.gpu) != quote["gpu"] or
            normalize_cpu(deal.cpu) != normalize_cpu(quote["cpu"]) or
            deal.ram_gb != quote["ram_gb"] or
            normalize_resolution(deal.screen_resolution) != quote["screen_resolution"]):
        return False
    if deal.ssd_gb != quote["ssd_gb"] and not (
            quote["nominal_ssd_gb"] == 1000 and {deal.ssd_gb, quote["ssd_gb"]} == {1000, 1024}):
        return False
    return (deal.screen_inch is not None and quote["screen_inch"] is not None and
            abs(float(deal.screen_inch) - float(quote["screen_inch"])) < 0.05)


def unique_russian_top(db_path: Path | str = DEFAULT_DB_PATH, *, limit: int = 10):
    from russian_deals import load_russian_ranked_deals
    from laptop_eligibility import deal_is_eligible
    from deal_ranking import score_offer

    now = datetime.now(timezone.utc)
    confirmed = _confirmed_russian_quotes(db_path, now)
    if not confirmed:
        return []
    deals = load_russian_ranked_deals(db_path, limit=0)
    out = []
    seen: set[str] = set()
    for deal in deals:
        quote = confirmed.get((deal.store, str((deal.offer or {}).get("external_id") or "")))
        if (not quote or not quote["name"] or not deal.cluster_name or not deal.url or
                quote["price"] != deal.price or quote["url"] != deal.url or
                not _same_confirmed_configuration(deal, quote)):
            continue
        score = score_offer(
            price=deal.price, gpu=quote["gpu"], cpu=quote["cpu"],
            ram_gb=quote["ram_gb"], ssd_gb=quote["ssd_gb"],
            screen_inch=float(quote["screen_inch"]),
            screen_resolution=quote["screen_resolution"],
            historical_min=deal.historical_min,
        )
        deal = replace(
            deal, cluster_name=quote["name"], gpu=quote["gpu"], cpu=quote["cpu"],
            ram_gb=int(quote["ram_gb"]), ssd_gb=int(quote["ssd_gb"]),
            screen_inch=float(quote["screen_inch"]),
            screen_resolution=quote["screen_resolution"],
            score=score.score, reasons=score.reasons, confidence=score.confidence,
            score_breakdown=score.breakdown,
            saving_vs_next=None, saving_verified=False,
            next_store=None, next_price=None,
            offer={**deal.offer, "price_checked_at": quote["price_checked_at"],
                   "availability_checked_at": quote["availability_checked_at"]},
        )
        identity = model_callback_token(deal, length=64)
        if identity in seen or not deal_is_eligible(deal):
            continue
        seen.add(identity)
        out.append(deal)
    out.sort(key=lambda item: (-item.score, item.price or 10**12, item.cluster_name))
    if limit > 0:
        return out[:limit]
    return out


_SKUISH = re.compile(r"^[A-Z0-9][A-Z0-9-]{9,}$", re.I)


def _short_gpu_label(gpu: str | None) -> str | None:
    key = canonical_gpu(gpu)
    if not key:
        return None
    return key.replace("RTX ", "")


def friendly_model_label(deal) -> str | None:
    """Readable model for a button. Long SKU tails are dropped; a bare SKU returns None."""
    raw = re.sub(r"^(ноутбук|notebook|laptop)\s+", "", html.unescape((deal.cluster_name or "").strip()), flags=re.I)
    parts: list[str] = []
    for token in raw.split():
        piece = token.strip(" ,.;")
        if not piece:
            continue
        if "-" in piece and len(piece) >= 12:
            head = piece.split("-", 1)[0]
            if head[:1].isalpha() and 2 <= len(head) <= 8 and any(ch.isalpha() for ch in head):
                parts.append(head)
            continue
        if _SKUISH.match(piece) and any(ch.isdigit() for ch in piece):
            continue
        parts.append(piece)
    text = " ".join(parts).strip(" -")
    if len(text) < 3 or (_SKUISH.match(text) and any(ch.isdigit() for ch in text)):
        return None
    return text


def picker_button_label(deal, index: int) -> str:
    from thailand.target_model import build_target_model

    gpu = _short_gpu_label(deal.gpu)
    price = f"{int(deal.price):,}".replace(",", " ") if deal.price is not None else "н/д"
    name = friendly_model_label(deal)
    if not name:
        name = str(build_target_model(deal).get("canonical_model_code") or deal.cluster_name or "Модель")
    bits = [f"{index}. {name}"]
    if gpu:
        bits.append(gpu)
    bits.append(f"{price} ₽")
    text = " · ".join(bits)
    if len(text) <= 64:
        return text
    tail = " · ".join(bit for bit in bits[1:])
    room = 64 - len(tail) - 3
    short = name[: max(8, room - len(f"{index}. "))].rstrip()
    return f"{index}. {short} · {tail}"[:64]


def model_picker_keyboard(deals) -> dict[str, Any]:
    rows = []
    for index, deal in enumerate(deals[:10], start=1):
        token = CALLBACK_MODEL_PICK + model_callback_token(deal)
        rows.append([{"text": picker_button_label(deal, index), "callback_data": token}])
    rows.append([{"text": "🏠 Меню", "callback_data": CALLBACK_MENU}])
    return {"inline_keyboard": rows}


def recommendation_keyboard(deals) -> dict[str, Any]:
    rows = []
    for index, deal in enumerate(deals[:10], start=1):
        token = CALLBACK_RECOMMEND + model_callback_token(deal)
        rows.append([{"text": picker_button_label(deal, index), "callback_data": token}])
    rows.append([{"text": "🏠 Меню", "callback_data": CALLBACK_MENU}])
    return {"inline_keyboard": rows}


def buy_picker_keyboard(deals) -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": f"{index}. Стоит ли покупать?", "callback_data": CALLBACK_BUY + model_callback_token(deal)}]
        for index, deal in enumerate(deals[:10], 1)
    ]}


def handle_recommendation_menu(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    message_id: int | None = None,
) -> None:
    deals = unique_russian_top(db_path, limit=10)
    if not deals:
        send_message(
            client,
            token,
            chat_id,
            "Нет свежих данных. Сначала запустите проверку.",
            with_keyboard=False,
        )
        return
    _reply_or_edit(
        client,
        token,
        chat_id,
        "Выберите модель из TOP-10 России:",
        message_id=message_id,
        reply_markup=recommendation_keyboard(deals),
    )


def handle_recommendation_pick(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    token_id: str,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Explain a current Russian offer without enqueueing a Thailand scan."""
    deals = unique_russian_top(db_path, limit=10)
    if not deals:
        send_message(client, token, chat_id, "Нет свежих данных. Сначала запустите проверку.", with_keyboard=False)
        return
    if token_id == "best":
        deal = deals[0]
    else:
        deal = next((item for item in deals if model_callback_token(item) == token_id), None)
    if deal is None:
        send_message(client, token, chat_id, "Эта модель уже не в текущем ТОП. Откройте список ещё раз.", with_keyboard=False)
        return
    from purchase_recommendation import format_russian_recommendation

    send_message(client, token, chat_id, format_russian_recommendation(deal), with_keyboard=False)


def handle_thailand_model_picker(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    message_id: int | None = None,
) -> None:
    deals = unique_russian_top(db_path, limit=10)
    if not deals:
        send_message(client, token, chat_id, "Нет свежего российского ТОП для поиска.", with_keyboard=False)
        return
    _reply_or_edit(
        client,
        token,
        chat_id,
        "<b>Найти модель из ТОП РФ в Таиланде</b>\nВыберите модель:",
        message_id=message_id,
        reply_markup=model_picker_keyboard(deals),
    )


def handle_thailand_model_pick(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    token_id: str,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Resolve the clicked model now and enqueue. Does not wait for the scan."""
    deals = unique_russian_top(db_path, limit=10)
    deal = next((d for d in deals if model_callback_token(d) == token_id), None)
    if deal is None:
        send_message(client, token, chat_id, "Эта модель уже не в текущем ТОП. Откройте список ещё раз.", with_keyboard=False)
        return
    from buy_thailand_flow import enqueue_model_thailand_job

    result = enqueue_model_thailand_job(deal, chat_id=chat_id)
    if result.get("ok"):
        name = friendly_model_label(deal) or deal.cluster_name
        gpu = canonical_gpu(deal.gpu) or ""
        price = f"{int(deal.price):,}".replace(",", " ") if deal.price is not None else "н/д"
        lines = ["Ищу в Таиланде:", str(name)]
        if gpu:
            lines.append(gpu)
        lines.append(f"Россия: {price} ₽")
        send_message(client, token, chat_id, "\n".join(lines), with_keyboard=False)
    else:
        send_message(client, token, chat_id, "Не удалось поставить поиск в очередь.", with_keyboard=False)


def handle_thailand_manual(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    *,
    compare: bool,
) -> None:
    """Admin-only: enqueue on-demand Thailand job (no network scan in bot process)."""
    if config.stage22_market_telegram_enabled():
        _stage22_thailand_action(client, token, chat_id, compare=compare)
        return
    try:
        from buy_thailand_flow import enqueue_manual_thailand_job

        result = enqueue_manual_thailand_job(
            compare=compare,
            chat_id=chat_id,
            db_path=DEFAULT_DB_PATH,
        )
        if result.get("ok"):
            send_message(
                client,
                token,
                chat_id,
                "🇹🇭 Проверка Таиланда запущена. Результат придёт отдельным сообщением.",
                with_keyboard=False,
            )
        else:
            err = result.get("error") or result.get("thailand_scan_status") or "unknown"
            send_message(
                client,
                token,
                chat_id,
                f"Не удалось запустить проверку Таиланда ({err}).",
                with_keyboard=False,
            )
    except Exception as exc:
        send_message(
            client,
            token,
            chat_id,
            f"Ошибка проверки Таиланда: {type(exc).__name__}",
            with_keyboard=False,
        )


def _stage22_thailand_action(client, token, chat_id, *, compare: bool) -> None:
    """Explicit refresh always enters the existing planner; no store calls here."""
    from thailand.market_telegram import enqueue_view_refresh

    view = "general_top" if compare else "thai_top"
    enqueue_view_refresh(chat_id=chat_id, view=view, trigger="manual")
    send_message(
        client, token, chat_id,
        "Проверка Таиланда поставлена в очередь. Магазины из бота не вызываются.",
        with_keyboard=False,
    )


def handle_run(client: httpx.Client, token: str, chat_id: str | int) -> None:
    notify_manual_result(client, token)
    state, accepted = request_run(chat_id)
    if not accepted:
        message = ("Результат предыдущей проверки ещё отправляется."
                   if state.get("status") not in {"pending", "running", "busy"}
                   else "Проверка уже выполняется.")
        if state.get("id"):
            message += f" № {state['id'][:8]}"
        send_message(client, token, chat_id, message, with_keyboard=False)
        return
    notice = _post_telegram(client, token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"<b>Обновление цен принято</b> · № {state['id'][:8]}\nПроверка идёт. Статус доступен через /status.",
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })
    message_id = ((notice or {}).get("result") or {}).get("message_id")
    if message_id is not None:
        set_message_id(state["id"], int(message_id))


def notify_manual_result(client: httpx.Client, token: str) -> None:
    state = reconcile()
    if state.get("status") in {"pending", "running"} or state.get("notified") or not state.get("id"):
        return
    label = "успешно" if state.get("status") == "success" else "с ошибкой"
    detail = html.escape(str(state.get("error") or ""))[:900]
    text = (f"<b>Обновление цен завершилось {label}</b> · № {state['id'][:8]}\n"
            f"Состояние: {html.escape(str(state.get('status')))}"
            + (f"\n{detail}" if detail else ""))
    if state.get("delivery_status") == "fallback_claimed":
        mark_notified(state["id"], delivery="fallback_unconfirmed")
        return
    if state.get("message_id") and int(state.get("delivery_attempts") or 0) < MAX_DELIVERY_ATTEMPTS:
        error: dict[str, str] = {}
        if edit_message(client, token, state["chat_id"], int(state["message_id"]), text,
                        error_out=error):
            mark_notified(state["id"], delivery="edited")
            return
        if error.get("kind") == "not_modified":
            mark_notified(state["id"], delivery="already_visible")
            return
        state = record_delivery_attempt(state["id"], error.get("kind", "retry"))
        if error.get("kind") != "permanent" and int(state.get("delivery_attempts") or 0) < MAX_DELIVERY_ATTEMPTS:
            return
        if error.get("kind") != "permanent":
            # A timed-out edit may already be visible. Do not send a duplicate.
            mark_notified(state["id"], delivery="edit_unconfirmed")
            return
    if claim_fallback(state["id"]):
        sent = send_message(client, token, state["chat_id"], text, with_keyboard=False)
        mark_notified(state["id"], delivery="fallback_sent" if sent else "fallback_unconfirmed")


def process_update(client: httpx.Client, token: str, update: dict[str, Any]) -> None:
    callback = update.get("callback_query")
    message = update.get("message")

    if callback:
        chat = (callback.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        data = str(callback.get("data") or "")
        cb_id = str(callback.get("id") or "")
        if chat_id is None:
            return
        if not _is_admin(chat_id):
            answer_callback(client, token, cb_id, "Недостаточно прав")
            return
        answer_callback(client, token, cb_id)
        msg = callback.get("message") or {}
        message_id_raw = msg.get("message_id")
        try:
            message_id = int(message_id_raw) if message_id_raw is not None else None
        except (TypeError, ValueError):
            message_id = None
        if data == BTN_RUN:
            handle_run(client, token, chat_id)
        elif data == BTN_TOP:
            handle_top(client, token, chat_id)
        elif data == BTN_MARKETS:
            handle_markets_menu(
                client, token, chat_id, message_id=message_id
            )
        elif data == BTN_RU_TOP:
            handle_top(client, token, chat_id)
        elif data == BTN_TH_SCAN:
            handle_thailand_manual(client, token, chat_id, compare=False)
        elif data == BTN_TH_COMPARE:
            handle_thailand_manual(client, token, chat_id, compare=True)
        elif data == BTN_PERSONAL:
            handle_personal_top(client, token, chat_id, message_id=message_id)
        elif data == BTN_PERSONAL_REFRESH:
            handle_personal_refresh(client, token, chat_id)
        elif data == BTN_RECOMMEND:
            handle_recommendation_menu(client, token, chat_id, message_id=message_id)
        elif data.startswith(CALLBACK_RECOMMEND):
            handle_recommendation_pick(
                client, token, chat_id, data[len(CALLBACK_RECOMMEND):]
            )
        elif data.startswith(CALLBACK_BUY):
            handle_recommendation_pick(client, token, chat_id, data[len(CALLBACK_BUY):])
        elif data == BTN_TH_FIND:
            handle_thailand_model_picker(client, token, chat_id, message_id=message_id)
        elif data.startswith(CALLBACK_MODEL_PICK):
            handle_thailand_model_pick(
                client, token, chat_id, data[len(CALLBACK_MODEL_PICK):]
            )
        elif data == BTN_STATUS:
            send_message(client, token, chat_id, build_status_text())
        elif data == BTN_VERSION:
            send_message(
                client,
                token,
                chat_id,
                f"{APP_NAME} {get_version()}\nInstance: {config.get_instance_id()}",
            )
        elif data in {BTN_HISTORY, CALLBACK_LIST}:
            handle_history_list(
                client, token, chat_id, message_id=message_id
            )
        elif data == CALLBACK_MENU:
            _reply_or_edit(
                client,
                token,
                chat_id,
                f"<b>{APP_NAME}</b> {get_version()}\nВыберите действие:",
                message_id=message_id,
                reply_markup=_keyboard(),
            )
        elif data.startswith(CALLBACK_MODEL_PREFIX) or data.startswith(
            CALLBACK_REFRESH_PREFIX
        ):
            product_id = parse_model_callback(data)
            if product_id is None:
                send_message(
                    client,
                    token,
                    chat_id,
                    "Не удалось разобрать модель.",
                )
            else:
                handle_history_model(
                    client,
                    token,
                    chat_id,
                    product_id,
                    message_id=message_id,
                )
        elif data.startswith(CALLBACK_PERIODS_PREFIX):
            product_id = parse_periods_callback(data)
            if product_id is None:
                send_message(client, token, chat_id, "Не удалось разобрать модель.")
            else:
                handle_chart_periods(
                    client,
                    token,
                    chat_id,
                    product_id,
                    message_id=message_id,
                )
        elif data.startswith(CALLBACK_CHART_PREFIX):
            parsed = parse_chart_callback(data)
            if parsed is None:
                send_message(client, token, chat_id, "Не удалось разобрать период.")
            else:
                period, product_id = parsed
                handle_chart_render(client, token, chat_id, product_id, period)
        return

    if message:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = str(message.get("text") or "").strip()
        if chat_id is None:
            return
        if not _is_admin(chat_id):
            return
        handle_text_message(client, token, chat_id, text)


def run_polling(*, timeout: int = 25) -> None:
    token, _ = config.get_telegram_credentials()
    if not config.get_telegram_admin_chat_ids():
        raise SystemExit(
            "TELEGRAM_ADMIN_CHAT_ID (or TELEGRAM_CHAT_ID) required for control bot"
        )
    offset = 0
    with httpx.Client(timeout=timeout + 10) as client:
        logger.info(
            "Control bot started version=%s instance=%s",
            get_version(),
            config.get_instance_id(),
        )
        while True:
            notify_manual_result(client, token)
            try:
                resp = client.get(
                    _api(token, "getUpdates"),
                    params={"timeout": min(timeout, 5), "offset": offset},
                )
            except httpx.TimeoutException:
                logger.error("Telegram getUpdates timeout")
                time.sleep(2)
                continue
            except httpx.HTTPError as exc:
                logger.error(
                    "%s",
                    telegram_http_error_summary(
                        method="getUpdates", description=safe_exc_message(exc)
                    ),
                )
                time.sleep(3)
                continue
            try:
                data = resp.json()
            except ValueError:
                logger.error(
                    "%s",
                    telegram_http_error_summary(
                        method="getUpdates",
                        status_code=resp.status_code,
                        description="invalid JSON",
                    ),
                )
                time.sleep(2)
                continue
            ok, err = parse_telegram_response(data)
            if resp.status_code >= 400 or not ok:
                logger.error(
                    "%s",
                    telegram_http_error_summary(
                        method="getUpdates",
                        status_code=resp.status_code,
                        api_ok=ok,
                        description=err,
                    ),
                )
                time.sleep(2)
                continue
            for update in data.get("result") or []:
                offset = max(offset, int(update["update_id"]) + 1)
                try:
                    process_update(client, token, update)
                except Exception as exc:
                    logger.error(
                        "Failed to process update: %s",
                        safe_exc_message(exc),
                    )
            notify_manual_result(client, token)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [v=%(version)s i=%(instance)s] %(name)s %(message)s",
    )

    class _ContextFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.version = get_version()  # type: ignore[attr-defined]
            record.instance = config.get_instance_id()  # type: ignore[attr-defined]
            return True

    for handler in logging.getLogger().handlers:
        handler.addFilter(_ContextFilter())

    # httpx logs full request URLs at INFO; those include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    try:
        require_schema_ready(DEFAULT_DB_PATH)
    except RuntimeError as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
