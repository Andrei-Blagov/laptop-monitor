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

# Global tracking / alerts / TOP / cross-store recommendation cap (inclusive).
# Collection may still persist products above this; user-facing scope is capped.
MAX_TRACKED_PRICE_RUB: int = 300_000

# Минимальная разница между магазинами для CROSS_STORE_SAVING (руб.).
CROSS_STORE_DIFFERENCE_RUB: int = 10_000

# --- Deal ranking v2 (explainable 0..100, single source of truth) ---
# Component maxima (must sum to 100):
SCORE_GPU_MAX: float = 25.0
SCORE_PRICE_MAX: float = 25.0
SCORE_CPU_MAX: float = 15.0
SCORE_RAM_MAX: float = 10.0
SCORE_SSD_MAX: float = 5.0
SCORE_SCREEN_MAX: float = 8.0
SCORE_HISTORY_MAX: float = 8.0
SCORE_SAVING_MAX: float = 4.0

SCORE_GPU_5080: float = 25.0
SCORE_GPU_5070TI: float = 21.0

SCORE_CPU_HIGH_END_HX: float = 15.0
SCORE_CPU_STRONG_H: float = 12.0
SCORE_CPU_MID: float = 8.0
SCORE_CPU_LOWER: float = 3.0

SCORE_RAM_64: float = 10.0
SCORE_RAM_32: float = 9.0
SCORE_RAM_16: float = 4.0

SCORE_SSD_2TB: float = 5.0
SCORE_SSD_1TB: float = 4.0
SCORE_SSD_512: float = 1.0

SCORE_SCREEN_18: float = 8.0
SCORE_SCREEN_17: float = 7.0
SCORE_SCREEN_16: float = 4.0
SCORE_SCREEN_156: float = 2.0
SCORE_SCREEN_QHD_BONUS: float = 2.0  # capped by SCORE_SCREEN_MAX

# Price/value curve vs GPU target (smooth, not binary).
SCORE_PRICE_AT_TARGET: float = 22.0
SCORE_PRICE_UNDER_SPAN: float = 0.12  # 12% under target → full SCORE_PRICE_MAX
SCORE_PRICE_OVER_ZERO_AT: float = 0.32  # ~32% over target → 0

# Historical opportunity bands (current / historical_min).
SCORE_HISTORY_WITHIN_1PCT: float = 8.0
SCORE_HISTORY_WITHIN_3PCT: float = 7.0
SCORE_HISTORY_WITHIN_5PCT: float = 5.0
SCORE_HISTORY_WITHIN_10PCT: float = 2.0

# Cross-store saving tiers (₽).
SCORE_SAVING_TIER_1: int = 5_000
SCORE_SAVING_TIER_2: int = 10_000
SCORE_SAVING_TIER_3: int = 20_000
SCORE_SAVING_TIER_4: int = 30_000

TOP_DEALS_LIMIT: int = 10
TELEGRAM_TOP_SOFT_LIMIT: int = 3500

# --- Buy opportunity signal (Russia) ---
BUY_MIN_CONFIDENCE: int = 80
BUY_RULE_A_MAX_OVER_HIST_PCT: float = 0.05  # <= hist_min * 1.05
BUY_RULE_B_MIN_SCORE: float = 75.0
BUY_RULE_B_MAX_OVER_HIST_PCT: float = 0.03
BUY_RULE_C_MIN_SCORE: float = 65.0
BUY_RULE_C_MAX_OVER_HIST_PCT: float = 0.01
BUY_STRONG_MAX_OVER_HIST_PCT: float = 0.03
BUY_STRONG_MIN_SCORE: float = 75.0
# Automatic BUY repeats only on a meaningful new event (price improvement / drop,
# BUY→STRONG_BUY, new historical low, new #1). Cooldown expiry alone never
# re-notifies; the cooldown only throttles repeated "new #1" flapping.
BUY_COOLDOWN_HOURS: float = 24.0
BUY_BYPASS_PRICE_IMPROVE_PCT: float = 0.03
BUY_BYPASS_PRICE_DROP_RUB: int = 5_000
# Automatic BUY needs this much stored observation history for the model
# cluster (earliest price_history row). Manual / Thailand checks are not gated.
BUY_MIN_HISTORY_DAYS: float = 14.0
# If the automatic Thailand enqueue fails after a BUY was announced, later
# pipeline runs retry only the enqueue (never the Russian BUY message).
# Interval sits just under the 2h pipeline cadence so the next run retries.
THAILAND_ENQUEUE_RETRY_INTERVAL_MINUTES: float = 110.0
THAILAND_ENQUEUE_MAX_RETRIES: int = 3

# --- Thailand on-demand scan ---
THAILAND_PER_STORE_TIMEOUT_SECONDS: float = 120.0
THAILAND_OVERALL_TIMEOUT_SECONDS: float = 240.0
THAILAND_SNAPSHOT_RETENTION: int = 20
THAILAND_TOP_LIMIT: int = 5
THAILAND_FX_STALE_MAX_HOURS: float = 36.0
THAILAND_NEAR_PRICE_THB: int = 1500
THAILAND_NEAR_PRICE_PCT: float = 0.02  # direct vs marketplace tie band
THAILAND_TOP_ALT_CHANNELS_MAX: int = 2
THAILAND_MARKETPLACE_MIN_SELLER_RATING: float = 4.7
THAILAND_MARKETPLACE_MIN_REVIEWS: int = 50
THAILAND_MARKETPLACE_MIN_UNITS_SOLD: int = 20
THAILAND_BROWSER_TIMEOUT_MS: int = 45_000
THAILAND_LAZADA_QUERY_WAIT_MS: int = 4500
# Experimental flags (fail-safe when live collection blocked)
THAILAND_BANANA_BROWSER_ENABLED: bool = True
THAILAND_LAZADA_BROWSER_ENABLED: bool = True
# Inner Lazada adapter gate. Automatic scan uses THAILAND_STORE_LAZADA_ENABLED.
THAILAND_LAZADA_ENABLED: bool = True
THAILAND_LAZADA_BLOCK_HEAVY_RESOURCES: bool = True
# Automatic Thailand worker stores. BaNANA/Lazada stay in the tree but are off
# until a public path exists; manual diagnostics can still call them directly.
THAILAND_STORE_JIB_ENABLED: bool = True
THAILAND_STORE_ADVICE_ENABLED: bool = True
THAILAND_STORE_SPEEDCOM_ENABLED: bool = True
THAILAND_STORE_INVADEIT_ENABLED: bool = True
THAILAND_STORE_ITCITY_ENABLED: bool = True
THAILAND_STORE_BANANA_ENABLED: bool = False
THAILAND_STORE_LAZADA_ENABLED: bool = False
THAILAND_SOURCE_FAILURE_THRESHOLD: int = 3
THAILAND_SOURCE_COOLDOWN_HOURS: float = 24.0
THAILAND_SOURCE_HEALTH_PATH: str = "data/thailand_source_health.json"
# Playwright stays on the main thread; do not run several browsers at once.
THAILAND_BROWSER_MAX_CONCURRENCY: int = 1
# Async Thailand worker queue (v0.5.1 candidate)
THAILAND_JOB_MAX_PENDING: int = 10
THAILAND_JOB_ARCHIVE_RETENTION: int = 50
THAILAND_JOB_FAILED_RETENTION: int = 20
THAILAND_JOB_STALE_PROCESSING_SECONDS: float = 600.0  # 10 minutes
THAILAND_JOB_MAX_ATTEMPTS: int = 2
THAILAND_JOBS_DIR = "data/thailand_jobs"
THAILAND_WORKER_STATE_PATH = "data/thailand_worker_state.json"
# Country verdict bands on equivalent hardware (abs % price delta).
COUNTRY_VERDICT_EQUAL_PCT: float = 0.03
COUNTRY_VERDICT_SLIGHT_PCT: float = 0.08
# International value score raw component maxima (sum 88 → normalize to 0..100).
INTL_SCORE_GPU_MAX: float = 25.0
INTL_SCORE_PRICE_MAX: float = 25.0
INTL_SCORE_CPU_MAX: float = 15.0
INTL_SCORE_RAM_MAX: float = 10.0
INTL_SCORE_SSD_MAX: float = 5.0
INTL_SCORE_SCREEN_MAX: float = 8.0
INTL_SCORE_RAW_MAX: float = 88.0

# Store is fresh for TOP / current deals if latest attempt is ok and within TTL.
# Schedule is every 2 hours → default ~3 hours.
STORE_FRESHNESS_MAX_MINUTES: int = 180

# Monitoring geography. Cross-store only among same-region snapshots.
MONITOR_REGION: str = "moscow"

# n8n webhook (optional OPS layer; empty URL = disabled)
N8N_WEBHOOK_TIMEOUT_SECONDS: float = 8.0

# Telegram delivery
CHANNEL_TELEGRAM = "telegram"
# Стабильный ключ destination в DB (chat_id используется только API-клиентом).
TELEGRAM_DESTINATION = "default"
MAX_DELIVERY_ATTEMPTS: int = 5
# Минимальный интервал между sendMessage в один chat (сек).
TELEGRAM_MIN_SEND_INTERVAL_SECONDS: float = 1.1


def is_price_in_tracking_scope(
    price: int | float | None,
    *,
    max_price: int | None = None,
) -> bool:
    """True if public price is eligible for alerts / TOP / cross-store tracking."""
    if price is None:
        return False
    try:
        value = int(price)
    except (TypeError, ValueError):
        return False
    cap = int(MAX_TRACKED_PRICE_RUB if max_price is None else max_price)
    return value > 0 and value <= cap


def format_price_cap_label(max_price: int | None = None) -> str:
    """Human-readable cap, e.g. '300 000'."""
    cap = int(MAX_TRACKED_PRICE_RUB if max_price is None else max_price)
    return f"{cap:,}".replace(",", " ")


def get_monitor_region() -> str:
    load_dotenv_if_present()
    return (os.environ.get("MONITOR_REGION") or MONITOR_REGION).strip().lower()


def get_n8n_webhook_config() -> tuple[str | None, str | None, float]:
    """Returns (url, secret, timeout). url None => disabled."""
    load_dotenv_if_present()
    url = (os.environ.get("N8N_WEBHOOK_URL") or "").strip() or None
    secret = (os.environ.get("N8N_WEBHOOK_SECRET") or "").strip() or None
    raw_timeout = (os.environ.get("N8N_WEBHOOK_TIMEOUT_SECONDS") or "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else float(N8N_WEBHOOK_TIMEOUT_SECONDS)
    except ValueError:
        timeout = float(N8N_WEBHOOK_TIMEOUT_SECONDS)
    return url, secret, timeout


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
