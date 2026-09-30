from __future__ import annotations

"""
Telegram control bot (long polling).

Admin-only buttons: run pipeline / top deals / status / version.
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

import config
from comparison import match_products
from deal_ranking import format_top_deals_message, rank_clusters
from deliver import run_status as deliver_status
from identity_sync import identity_from_cache, load_specs_cache
from pipeline_lock import DEFAULT_LOCK_PATH, read_lock_info
from run_pipeline import (
    EXIT_LOCKED,
    run_pipeline,
    run_status as pipeline_status,
)
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers,
    get_all_products,
    get_latest_store_runs,
    init_db,
    open_db,
)
from stores.registry import all_adapters
from version import APP_NAME, get_version

logger = logging.getLogger(__name__)

BTN_RUN = "ctrl:run"
BTN_TOP = "ctrl:top"
BTN_STATUS = "ctrl:status"
BTN_VERSION = "ctrl:version"


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
                {"text": "Статус", "callback_data": BTN_STATUS},
                {"text": "Версия", "callback_data": BTN_VERSION},
            ],
        ]
    }


def send_message(
    client: httpx.Client,
    token: str,
    chat_id: str | int,
    text: str,
    *,
    with_keyboard: bool = True,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if with_keyboard:
        payload["reply_markup"] = _keyboard()
    client.post(_api(token, "sendMessage"), json=payload, timeout=30.0)


def answer_callback(
    client: httpx.Client, token: str, callback_id: str, text: str | None = None
) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]
    client.post(_api(token, "answerCallbackQuery"), json=payload, timeout=15.0)


def build_status_text(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    runs = pipeline_status(db_path, limit=1)
    last = runs[0] if runs else None
    tg = deliver_status(db_path)
    lock = read_lock_info(DEFAULT_LOCK_PATH)
    lines = [
        f"<b>{APP_NAME}</b>",
        f"Version: {get_version()}",
        f"Instance: {config.get_instance_id()}",
    ]
    if last:
        lines.append(f"Last pipeline: {last.get('started_at')}")
        lines.append(f"Status: {str(last.get('status') or '').upper()}")
        if last.get("duration_seconds") is not None:
            lines.append(f"Duration: {last.get('duration_seconds')}s")
        lines.append("Stores:")
        lines.extend(_store_status_lines(db_path, last))
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


def _store_status_lines(db_path: Path | str, last: dict[str, Any]) -> list[str]:
    """Prefer store_runs; fall back to legacy regard/andpro columns."""
    out: list[str] = []
    try:
        with open_db(db_path) as conn:
            init_db(conn)
            latest = get_latest_store_runs(conn)
    except Exception:
        latest = {}
    if latest:
        by_slug = {a.slug: a for a in all_adapters()}
        for slug, row in sorted(latest.items()):
            name = by_slug[slug].display_name if slug in by_slug else slug
            status = row.get("status") or "?"
            count = row.get("products_count")
            suffix = f" ({count})" if count is not None else ""
            out.append(f"  {name}: {status}{suffix}")
        for adapter in all_adapters():
            if adapter.slug not in latest:
                flag = "disabled" if not adapter.enabled else "no runs"
                out.append(f"  {adapter.display_name}: {flag}")
        return out
    out.append(
        f"  Regard: {last.get('regard_status')} ({last.get('regard_products')})"
    )
    out.append(
        f"  ANDPRO: {last.get('andpro_status')} ({last.get('andpro_products')})"
    )
    for adapter in all_adapters():
        if adapter.slug in {"regard", "andpro"}:
            continue
        flag = "disabled" if not adapter.enabled else "no runs"
        out.append(f"  {adapter.display_name}: {flag}")
    return out


def build_top_text(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    products = get_all_products(db_path)
    identifiers = get_all_identifiers(db_path)
    comparison = match_products(products, identifiers)
    cache = load_specs_cache()
    specs_by_key = {}
    for p in products:
        identity = identity_from_cache(cache, str(p["store"]), str(p["external_id"]))
        if identity is not None:
            specs_by_key[(str(p["store"]), str(p["external_id"]))] = identity
    deals = rank_clusters(
        comparison.matches,
        specs_by_key=specs_by_key,
        limit=int(config.TOP_DEALS_LIMIT),
    )
    return format_top_deals_message(deals, limit=int(config.TOP_DEALS_LIMIT))


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
    text = (
        f"<b>{APP_NAME}</b> {get_version()}\n"
        f"Instance: {config.get_instance_id()}\n"
        f"Pipeline: {result.status.upper()}\n"
        f"Duration: {result.duration_seconds}s\n"
        f"Regard: {result.regard_status} ({result.regard_products})\n"
        f"ANDPRO: {result.andpro_status} ({result.andpro_products})\n"
        f"New alerts: {result.alerts_created}\n"
        f"Telegram sent/failed: {result.messages_sent}/{result.messages_failed}"
    )
    if result.error_message:
        text += f"\nError: {result.error_message}"
    send_message(client, token, chat_id, text)


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
                data = resp.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                for update in data.get("result") or []:
                    offset = max(offset, int(update["update_id"]) + 1)
                    try:
                        process_update(client, token, update)
                    except Exception:
                        logger.exception("Failed to process update")
            except Exception:
                logger.exception("Polling error")
                time.sleep(3)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Ensure schema exists for status queries.
    with open_db(DEFAULT_DB_PATH) as conn:
        init_db(conn)
    run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
