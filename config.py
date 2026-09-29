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

# Telegram delivery
CHANNEL_TELEGRAM = "telegram"
# Стабильный ключ destination в DB (chat_id используется только API-клиентом).
TELEGRAM_DESTINATION = "default"
MAX_DELIVERY_ATTEMPTS: int = 5
# Минимальный интервал между sendMessage в один chat (сек).
TELEGRAM_MIN_SEND_INTERVAL_SECONDS: float = 1.1


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
