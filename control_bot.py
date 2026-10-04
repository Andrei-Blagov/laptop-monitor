from __future__ import annotations

"""
Telegram control bot (long polling).

Admin-only buttons: run pipeline / top deals / price history / status / version.
Does NOT run schema migrations — require migrate_db first.
"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

import config
from comparison import match_products
from deal_ranking import (
    collect_match_product_ids,
    format_top_deals_message,
    historical_mins_from_rows,
    rank_clusters,
)
from identity_sync import identity_from_cache, load_specs_cache
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
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_delivery_stats,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
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

logger = logging.getLogger(__name__)

BTN_RUN = "ctrl:run"
BTN_TOP = "ctrl:top"
BTN_STATUS = "ctrl:status"
BTN_VERSION = "ctrl:version"
BTN_HISTORY = "ctrl:hist"


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _is_admin(chat_id: str | int) -> bool:
    return str(chat_id) in config.get_telegram_admin_chat_ids()


def _keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Запустить проверку", "callback_data": BTN_RUN},
                {"text": "Топ предложений", "callback_data": BTN_TOP},
            ],
            [
                {"text": "📈 История цены", "callback_data": BTN_HISTORY},
            ],
            [
                {"text": "Статус", "callback_data": BTN_STATUS},
                {"text": "Версия", "callback_data": BTN_VERSION},
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
) -> dict[str, Any] | None:
    try:
        resp = client.post(_api(token, method), json=payload, timeout=timeout)
    except httpx.TimeoutException:
        logger.error("Telegram %s timeout", method)
        return None
    except httpx.HTTPError as exc:
        logger.error("%s", telegram_http_error_summary(method=method, description=safe_exc_message(exc)))
        return None
    try:
        data = resp.json()
    except ValueError:
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
    return _post_telegram(client, token, "editMessageText", payload) is not None


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
    # Read-only: never mutate schema.
    runs = pipeline_status(db_path, limit=1)
    last = runs[0] if runs else None
    tg = _deliver_status_readonly(db_path)
    lock = read_lock_info(DEFAULT_LOCK_PATH)
    latest = get_latest_store_runs_readonly(db_path)
    lines = [
        f"<b>{APP_NAME}</b>",
        f"Version: {get_version()}",
        f"Instance: {config.get_instance_id()}",
        f"Price cap: ≤ {config.format_price_cap_label()} ₽",
    ]
    if last:
        lines.append(f"Last pipeline: {last.get('started_at')}")
        lines.append(f"Status: {str(last.get('status') or '').upper()}")
        if last.get("duration_seconds") is not None:
            lines.append(f"Duration: {last.get('duration_seconds')}s")
        lines.append("Stores:")
        run_id = last.get("id")
        store_rows: list[dict[str, Any]] | None = None
        if run_id is not None:
            try:
                conn = open_db_readonly(db_path)
                try:
                    store_rows = get_store_runs_for_pipeline(conn, int(run_id))
                finally:
                    conn.close()
            except Exception:
                store_rows = None
        if store_rows:
            for line in format_store_summary_lines(store_rows=store_rows):
                lines.append(f"  {line}")
        elif latest:
            for line in format_store_summary_lines(
                store_statuses={
                    s: {"status": r.get("status"), "products_count": r.get("products_count")}
                    for s, r in latest.items()
                }
            ):
                lines.append(f"  {line}")
        else:
            for adapter in all_adapters():
                flag = "enabled" if adapter.enabled else "disabled"
                lines.append(f"  {adapter.display_name}: {flag}")
        lines.append(f"New alerts: {last.get('alerts_created')}")
        lines.append(
            f"Telegram sent/failed: {last.get('messages_sent')}/"
            f"{last.get('messages_failed')}"
        )
        if last.get("app_version"):
            lines.append(f"Run version: {last.get('app_version')}")
    else:
        lines.append("Last pipeline: none")
        lines.append("Stores:")
        for adapter in all_adapters():
            flag = "enabled" if adapter.enabled else "disabled"
            lines.append(f"  {adapter.display_name}: {flag}")
    lines.append(f"Telegram queue unsent: {tg.get('unsent_alert_events', 0)}")
    lines.append(f"Lock active: {'yes' if lock else 'no'}")
    return "\n".join(lines)


def build_top_text(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    latest = get_latest_store_runs_readonly(db_path)
    fresh = get_fresh_store_slugs(latest)
    if not fresh:
        return "Нет свежих данных. Запустите проверку."
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    comparison = match_products(products, identifiers)
    cache = load_specs_cache()
    specs_by_key = {}
    for p in products:
        identity = identity_from_cache(cache, str(p["store"]), str(p["external_id"]))
        if identity is not None:
            specs_by_key[(str(p["store"]), str(p["external_id"]))] = identity
    pids = collect_match_product_ids(comparison.matches)
    hist_mins = historical_mins_from_rows(
        get_price_history_for_products_readonly(db_path, pids)
    )
    deals = rank_clusters(
        comparison.matches,
        specs_by_key=specs_by_key,
        limit=int(config.TOP_DEALS_LIMIT),
        fresh_stores=fresh,
        historical_mins=hist_mins,
    )
    age = freshness_age_minutes(latest, fresh)
    return format_top_deals_message(
        deals,
        limit=int(config.TOP_DEALS_LIMIT),
        max_age_minutes=age,
        empty_message=(
            f"Нет свежих предложений до {config.format_price_cap_label()} ₽. "
            "Запустите проверку."
        ),
    )


def handle_run(client: httpx.Client, token: str, chat_id: str | int) -> None:
    send_message(
        client,
        token,
        chat_id,
        f"<b>{APP_NAME}</b> {get_version()}\nЗапускаю проверку…",
        with_keyboard=False,
    )
    result = run_pipeline(
        DEFAULT_DB_PATH,
        enrich_identities=True,
        write_comparison_artifacts=True,
        last_run_path=Path("data") / "last_run.json",
        deliver=True,
    )
    if result.exit_code == EXIT_LOCKED:
        send_message(client, token, chat_id, "Проверка уже выполняется.")
        return
    send_message(client, token, chat_id, format_pipeline_result_message(result))


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
            send_message(client, token, chat_id, build_top_text())
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
        if text in {"/start", "/menu", "/help"}:
            send_message(
                client,
                token,
                chat_id,
                f"<b>{APP_NAME}</b> {get_version()}\nВыберите действие:",
            )
        elif text == "/status":
            send_message(client, token, chat_id, build_status_text())
        elif text == "/version":
            send_message(
                client,
                token,
                chat_id,
                f"{APP_NAME} {get_version()}",
            )


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
            try:
                resp = client.get(
                    _api(token, "getUpdates"),
                    params={"timeout": timeout, "offset": offset},
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
