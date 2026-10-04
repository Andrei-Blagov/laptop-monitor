from __future__ import annotations

"""Telegram model price history (read-only)."""

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import config
from comparison import Offer, ProductMatch, match_products
from deal_ranking import rank_clusters
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
    get_product_by_id_readonly,
    open_db_readonly,
    count_price_history,
    count_products,
)
from store_freshness import get_fresh_store_slugs
from stores.registry import get_adapter

try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover - Windows without tzdata
    # Moscow is permanently UTC+3 (no DST).
    MSK = timezone(timedelta(hours=3))

CALLBACK_MODEL_PREFIX = "hist:model:"
CALLBACK_LIST = "hist:list"
CALLBACK_MENU = "hist:menu"
CALLBACK_REFRESH_PREFIX = "hist:refresh:"
CALLBACK_PERIODS_PREFIX = "hist:periods:"

HISTORY_PICKER_LIMIT = 10
TELEGRAM_MESSAGE_SOFT_LIMIT = 3500
CALLBACK_DATA_LIMIT = 64


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def format_rub(price: int | float | None) -> str:
    if price is None:
        return "н/д"
    try:
        amount = int(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{amount:,}".replace(",", " ") + " ₽"


def store_label(slug: str) -> str:
    adapter = get_adapter(slug)
    if adapter is not None:
        return adapter.display_name
    if slug == "andpro":
        return "ANDPRO"
    return slug.capitalize() if slug else "?"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_msk(value: str | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = parse_iso(str(value))
        if dt is None:
            return str(value)
    local = dt.astimezone(MSK)
    return local.strftime("%Y-%m-%d %H:%M MSK")


def make_model_callback(product_id: int) -> str:
    data = f"{CALLBACK_MODEL_PREFIX}{int(product_id)}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def make_refresh_callback(product_id: int) -> str:
    data = f"{CALLBACK_REFRESH_PREFIX}{int(product_id)}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def parse_model_callback(data: str) -> int | None:
    text = str(data or "")
    if text.startswith(CALLBACK_MODEL_PREFIX):
        raw = text[len(CALLBACK_MODEL_PREFIX) :]
    elif text.startswith(CALLBACK_REFRESH_PREFIX):
        raw = text[len(CALLBACK_REFRESH_PREFIX) :]
    else:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def short_model_button_text(name: str, price: int, *, max_len: int = 56) -> str:
    price_part = f" — {format_rub(price)}"
    budget = max(8, max_len - len(price_part))
    base = " ".join(str(name or "Модель").split())
    if len(base) > budget:
        base = base[: max(0, budget - 1)].rstrip() + "…"
    return base + price_part


def _valid_price(value: Any) -> int | None:
    if value is None:
        return None
    try:
        price = int(value)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def price_at_or_before(
    history: Sequence[Mapping[str, Any]],
    cutoff: datetime,
) -> int | None:
    """Step-function: latest valid price with checked_at <= cutoff."""
    latest: int | None = None
    for row in history:
        ts = parse_iso(row.get("checked_at"))
        if ts is None or ts > cutoff:
            continue
        price = _valid_price(row.get("price"))
        if price is not None:
            latest = price
    return latest


def historical_min_for_history(
    history: Sequence[Mapping[str, Any]],
) -> tuple[int, str | None] | None:
    best: tuple[int, str | None] | None = None
    for row in history:
        price = _valid_price(row.get("price"))
        if price is None:
            continue
        checked = row.get("checked_at")
        if best is None or price < best[0]:
            best = (price, str(checked) if checked else None)
    return best


@dataclass(frozen=True)
class HistoryPickerItem:
    product_id: int
    name: str
    price: int
    store: str
    callback_data: str


@dataclass
class StoreHistorySnapshot:
    store: str
    current_price: int | None
    available: bool
    historical_min: int | None
    historical_min_at: str | None
    product_id: int | None


@dataclass
class ModelHistoryReport:
    product_id: int
    name: str
    display_sku: str
    stale_current: bool
    stores: list[StoreHistorySnapshot]
    best_now: int | None
    best_now_store: str | None
    all_time_min: int | None
    all_time_min_store: str | None
    all_time_min_at: str | None
    best_7d_ago: int | None
    best_30d_ago: int | None
    found: bool = True
    error: str | None = None


def find_cluster_by_product_id(
    matches: Sequence[ProductMatch],
    product_id: int,
) -> ProductMatch | None:
    pid = int(product_id)
    for match in matches:
        for offer in match.offers:
            if offer.product_id is not None and int(offer.product_id) == pid:
                return match
    return None


def _load_matches(
    db_path: Path | str,
) -> tuple[list[dict], list[ProductMatch], set[str]]:
    latest = get_latest_store_runs_readonly(db_path)
    fresh = get_fresh_store_slugs(latest)
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    comparison = match_products(products, identifiers)
    return products, comparison.matches, fresh


def build_history_picker_items(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    limit: int = HISTORY_PICKER_LIMIT,
) -> list[HistoryPickerItem]:
    products, matches, fresh = _load_matches(db_path)
    if not fresh:
        return []
    id_by_key = {
        (str(p["store"]), str(p["external_id"])): int(p["id"])
        for p in products
        if p.get("id") is not None
    }
    deals = rank_clusters(matches, limit=int(limit), fresh_stores=fresh)
    items: list[HistoryPickerItem] = []
    for deal in deals:
        if deal.price is None or not config.is_price_in_tracking_scope(deal.price):
            continue
        external_id = str((deal.offer or {}).get("external_id") or "")
        pid = id_by_key.get((deal.store, external_id))
        if pid is None:
            continue
        items.append(
            HistoryPickerItem(
                product_id=pid,
                name=deal.cluster_name or deal.offer.get("name") or "Модель",
                price=int(deal.price),
                store=deal.store,
                callback_data=make_model_callback(pid),
            )
        )
    return items


def build_history_picker_keyboard(
    items: Sequence[HistoryPickerItem],
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for item in items:
        rows.append(
            [
                {
                    "text": short_model_button_text(item.name, item.price),
                    "callback_data": item.callback_data,
                }
            ]
        )
    rows.append([{"text": "🏠 Меню", "callback_data": CALLBACK_MENU}])
    return {"inline_keyboard": rows}


def make_periods_callback(product_id: int) -> str:
    data = f"{CALLBACK_PERIODS_PREFIX}{int(product_id)}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def history_nav_keyboard(product_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "⬅️ К списку", "callback_data": CALLBACK_LIST},
                {
                    "text": "🔄 Обновить",
                    "callback_data": make_refresh_callback(product_id),
                },
            ],
            [
                {
                    "text": "📊 График",
                    "callback_data": make_periods_callback(product_id),
                },
            ],
            [{"text": "🏠 Меню", "callback_data": CALLBACK_MENU}],
        ]
    }


def _current_best(
    offers: Sequence[Offer],
    *,
    fresh_stores: set[str] | None,
) -> tuple[int | None, str | None]:
    candidates: list[Offer] = []
    for offer in offers:
        if not offer.available:
            continue
        if fresh_stores is not None and offer.store not in fresh_stores:
            continue
        if _valid_price(offer.price) is None:
            continue
        candidates.append(offer)
    if not candidates:
        return None, None
    best = min(candidates, key=lambda o: (int(o.price), o.store))  # type: ignore[arg-type]
    return int(best.price), best.store  # type: ignore[arg-type]


def _best_at_cutoff(
    offers: Sequence[Offer],
    histories: Mapping[int, Sequence[Mapping[str, Any]]],
    cutoff: datetime,
) -> int | None:
    prices: list[int] = []
    for offer in offers:
        if offer.product_id is None:
            continue
        price = price_at_or_before(histories.get(int(offer.product_id), []), cutoff)
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return min(prices)


def build_model_history_report(
    db_path: Path | str,
    product_id: int,
    *,
    now: datetime | None = None,
) -> ModelHistoryReport:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    product = get_product_by_id_readonly(db_path, product_id)
    if product is None:
        return ModelHistoryReport(
            product_id=int(product_id),
            name="?",
            display_sku="?",
            stale_current=True,
            stores=[],
            best_now=None,
            best_now_store=None,
            all_time_min=None,
            all_time_min_store=None,
            all_time_min_at=None,
            best_7d_ago=None,
            best_30d_ago=None,
            found=False,
            error="Модель не найдена (product удалён или неизвестен).",
        )

    _products, matches, fresh = _load_matches(db_path)
    match = find_cluster_by_product_id(matches, int(product_id))
    if match is None:
        # Fallback: single-offer synthetic cluster from the product row.
        offer = Offer(
            store=str(product["store"]),
            external_id=str(product["external_id"]),
            name=str(product.get("name") or "?"),
            sku=product.get("sku"),
            price=product.get("price"),
            available=bool(product.get("available")),
            url=product.get("url"),
            product_id=int(product["id"]),
        )
        match = ProductMatch(
            normalized_sku=str(product.get("sku") or product["external_id"]),
            display_sku=str(product.get("sku") or product["external_id"]),
            name=str(product.get("name") or "?"),
            offers=[offer],
            match_method="singleton",
        )

    product_ids = [
        int(o.product_id) for o in match.offers if o.product_id is not None
    ]
    histories = get_price_history_for_products_readonly(db_path, product_ids)

    store_rows: list[StoreHistorySnapshot] = []
    all_time: tuple[int, str, str | None] | None = None  # price, store, at
    for offer in sorted(match.offers, key=lambda o: store_label(o.store).lower()):
        hist = histories.get(int(offer.product_id), []) if offer.product_id else []
        hmin = historical_min_for_history(hist)
        store_rows.append(
            StoreHistorySnapshot(
                store=offer.store,
                current_price=_valid_price(offer.price),
                available=bool(offer.available),
                historical_min=hmin[0] if hmin else None,
                historical_min_at=hmin[1] if hmin else None,
                product_id=int(offer.product_id) if offer.product_id is not None else None,
            )
        )
        if hmin is not None:
            if all_time is None or hmin[0] < all_time[0]:
                all_time = (hmin[0], offer.store, hmin[1])

    best_now, best_store = _current_best(match.offers, fresh_stores=fresh)
    stale = best_now is None
    cutoff_7 = now_utc - timedelta(days=7)
    cutoff_30 = now_utc - timedelta(days=30)
    best_7 = _best_at_cutoff(match.offers, histories, cutoff_7)
    best_30 = _best_at_cutoff(match.offers, histories, cutoff_30)

    return ModelHistoryReport(
        product_id=int(product_id),
        name=match.name,
        display_sku=match.display_sku,
        stale_current=stale,
        stores=store_rows,
        best_now=best_now,
        best_now_store=best_store,
        all_time_min=all_time[0] if all_time else None,
        all_time_min_store=all_time[1] if all_time else None,
        all_time_min_at=all_time[2] if all_time else None,
        best_7d_ago=best_7,
        best_30d_ago=best_30,
        found=True,
    )


def format_delta_line(now_price: int | None, past_price: int | None) -> str:
    if past_price is None or now_price is None:
        return "недостаточно данных"
    delta = int(now_price) - int(past_price)
    if past_price == 0:
        pct_part = ""
    else:
        pct = (delta / float(past_price)) * 100.0
        pct_part = f" ({pct:+.1f}%)".replace(".", ",")
    if delta == 0:
        return f"0 ₽{pct_part}"
    sign = "−" if delta < 0 else "+"
    return f"{sign}{format_rub(abs(delta))}{pct_part}"


def format_model_history_message(report: ModelHistoryReport) -> str:
    if not report.found:
        return (
            "📈 <b>ИСТОРИЯ ЦЕНЫ</b>\n\n"
            f"{_esc(report.error or 'Модель не найдена.')}"
        )

    lines: list[str] = [
        "📈 <b>ИСТОРИЯ ЦЕНЫ</b>",
        "",
        f"<b>{_esc(report.name)}</b>",
        f"SKU: {_esc(report.display_sku)}",
        "",
        "Сейчас:",
    ]
    if report.stores:
        for row in report.stores:
            label = store_label(row.store)
            if row.current_price is None:
                lines.append(f"• {label} — н/д")
            elif row.available:
                lines.append(f"• {label} — {format_rub(row.current_price)}")
            else:
                lines.append(
                    f"• {label} — {format_rub(row.current_price)} (нет в наличии)"
                )
    else:
        lines.append("• (нет offers)")

    lines.append("")
    if report.best_now is not None and report.best_now_store:
        lines.append(
            f"Лучшая цена:\n{format_rub(report.best_now)} — "
            f"{store_label(report.best_now_store)}"
        )
    else:
        lines.append("Лучшая цена:\nн/д")

    if report.stale_current:
        lines.append("")
        lines.append("⚠️ Текущие данные устарели")

    lines.append("")
    if report.all_time_min is not None and report.all_time_min_store:
        lines.append("Минимум за всё время:")
        lines.append(
            f"{format_rub(report.all_time_min)} — "
            f"{store_label(report.all_time_min_store)}"
        )
        lines.append(format_msk(report.all_time_min_at))
    else:
        lines.append("Минимум за всё время:\nн/д")

    lines.append("")
    lines.append("По магазинам:")
    for row in report.stores:
        label = store_label(row.store)
        lines.append(f"{label}:")
        lines.append(
            f"min {row.historical_min if row.historical_min is not None else 'н/д'}"
        )
        lines.append(
            f"current {row.current_price if row.current_price is not None else 'н/д'}"
        )

    lines.append("")
    lines.append("Изменение:")
    lines.append(
        f"7 дней: {format_delta_line(report.best_now, report.best_7d_ago)}"
    )
    lines.append(
        f"30 дней: {format_delta_line(report.best_now, report.best_30d_ago)}"
    )

    text = "\n".join(lines)
    if len(text) > TELEGRAM_MESSAGE_SOFT_LIMIT:
        # Drop per-store min block first.
        compact: list[str] = [
            "📈 <b>ИСТОРИЯ ЦЕНЫ</b>",
            "",
            f"<b>{_esc(report.name)}</b>",
            f"SKU: {_esc(report.display_sku)}",
            "",
            "Сейчас:",
        ]
        for row in report.stores:
            label = store_label(row.store)
            price = (
                format_rub(row.current_price)
                if row.current_price is not None
                else "н/д"
            )
            if row.available:
                compact.append(f"• {label} — {price}")
            else:
                compact.append(f"• {label} — {price} (нет в наличии)")
        compact.append("")
        if report.best_now is not None and report.best_now_store:
            compact.append(
                f"Лучшая цена: {format_rub(report.best_now)} — "
                f"{store_label(report.best_now_store)}"
            )
        if report.stale_current:
            compact.append("⚠️ Текущие данные устарели")
        if report.all_time_min is not None and report.all_time_min_store:
            compact.append(
                f"Минимум: {format_rub(report.all_time_min)} — "
                f"{store_label(report.all_time_min_store)}"
            )
            compact.append(format_msk(report.all_time_min_at))
        compact.append(
            f"7 дней: {format_delta_line(report.best_now, report.best_7d_ago)}"
        )
        compact.append(
            f"30 дней: {format_delta_line(report.best_now, report.best_30d_ago)}"
        )
        text = "\n".join(compact)
    return text


def format_history_picker_message(items: Sequence[HistoryPickerItem]) -> str:
    if not items:
        return (
            "📈 <b>ИСТОРИЯ ЦЕНЫ</b>\n\n"
            f"Нет свежих моделей до {config.format_price_cap_label()} ₽.\n"
            "Запустите проверку."
        )
    return (
        "📈 <b>ИСТОРИЯ ЦЕНЫ</b>\n\n"
        f"Выберите модель (TOP до {config.format_price_cap_label()} ₽):"
    )


def assert_history_readonly(db_path: Path | str) -> tuple[int, int]:
    """Return (products, history) counts; caller compares before/after."""
    try:
        conn = open_db_readonly(db_path)
    except FileNotFoundError:
        return 0, 0
    try:
        return count_products(conn), count_price_history(conn)
    finally:
        conn.close()
