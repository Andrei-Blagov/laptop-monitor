from __future__ import annotations

"""Централизованная конфигурация price monitor / alert engine."""

import os
from pathlib import Path

# Минимальное падение цены для PRICE_DROP (проценты).
PRICE_DROP_PERCENT: float = 5.0

# NEW_HISTORICAL_LOW: событие если drop >= percent ИЛИ drop >= absolute (руб.).
HISTORICAL_LOW_MIN_PERCENT: float = 2.0
HISTORICAL_LOW_MIN_ABSOLUTE: int = 3_000

# Пороги целевой цены по GPU (руб.).
TARGET_PRICES: dict[str, int] = {
    "RTX 5070 Ti": 230_000,
    "RTX 5080": 300_000,
}

# Минимальная разница между магазинами для CROSS_STORE_SAVING (руб.).
CROSS_STORE_DIFFERENCE_RUB: int = 10_000

# --- Deal ranking weights (explainable, config-driven) ---
RANK_GPU_5080_BONUS: float = 25.0
RANK_GPU_5070TI_BONUS: float = 18.0
RANK_RAM_32_BONUS: float = 12.0
RANK_RAM_16_BONUS: float = 4.0
RANK_SSD_1TB_BONUS: float = 8.0
RANK_SCREEN_17_BONUS: float = 10.0
RANK_SCREEN_16_BONUS: float = 4.0
RANK_BUDGET_ANCHOR_RUB: int = 200_000
RANK_BUDGET_UNDER_BONUS: float = 15.0
RANK_BUDGET_NEAR_BONUS: float = 6.0
RANK_HISTORICAL_LOW_BONUS: float = 8.0
TOP_DEALS_LIMIT: int = 10

# Telegram delivery
CHANNEL_TELEGRAM = "telegram"
# Стабильный ключ destination в DB (chat_id используется только API-клиентом).
TELEGRAM_DESTINATION = "default"
MAX_DELIVERY_ATTEMPTS: int = 5
# Минимальный интервал между sendMessage в один chat (сек).
TELEGRAM_MIN_SEND_INTERVAL_SECONDS: float = 1.1


def get_instance_id() -> str:
    load_dotenv_if_present()
    return (os.environ.get("LAPTOP_MONITOR_INSTANCE") or "local-dev").strip()


def get_telegram_admin_chat_ids() -> set[str]:
    """Allowlist of Telegram chat/user ids that may control the bot."""
    load_dotenv_if_present()
    raw = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()
    if not raw:
        # Fallback: primary destination chat is admin if not overridden.
        chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        return {chat} if chat else set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def load_dotenv_if_present() -> None:
    """Загружает .env из корня проекта, если установлен python-dotenv."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=False)


def get_telegram_credentials() -> tuple[str, str]:
    """
    Возвращает (bot_token, chat_id).

    Raises:
        ValueError: если переменные окружения не заданы.
    """
    load_dotenv_if_present()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise ValueError(
            "Telegram не настроен. Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID "
            "(см. .env.example)."
        )
    return token, chat_id
