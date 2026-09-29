from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
)


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _fmt_price(price: Any) -> str:
    if price is None:
        return "н/д"
    try:
        amount = int(price)
    except (TypeError, ValueError):
        return _esc(price)
    return f"{amount:,}".replace(",", " ") + " ₽"


def _store_label(store: Any) -> str:
    text = str(store or "?")
    if text.lower() == "andpro":
        return "ANDPRO"
    if text.lower() == "regard":
        return "Regard"
    return text


def _meta(event: Mapping[str, Any]) -> dict[str, Any]:
    meta = event.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def _url_line(url: Any) -> str:
    if not url:
        return ""
    # Escape for HTML text; Telegram still linkifies plain URLs.
    return _esc(url)


def format_target_price(event: Mapping[str, Any]) -> str:
    meta = _meta(event)
    lines = [
        "<b>TARGET PRICE</b>",
        "",
        _esc(meta.get("name") or "Товар"),
    ]
    if meta.get("gpu"):
        lines.append(_esc(meta["gpu"]))
    lines.append("")
    lines.append(_esc(_store_label(meta.get("store"))))
    lines.append(_esc(_fmt_price(event.get("new_price"))))
    lines.append("")
    lines.append(f"Порог: {_esc(_fmt_price(meta.get('threshold')))}")
    if meta.get("sku"):
        lines.append(f"SKU: {_esc(meta['sku'])}")
    if meta.get("url"):
        lines.append("")
        lines.append(_url_line(meta["url"]))
    return "\n".join(lines)


def format_price_drop(event: Mapping[str, Any]) -> str:
    meta = _meta(event)
    old_price = event.get("old_price")
    new_price = event.get("new_price")
    drop_amount = meta.get("drop_amount")
    if drop_amount is None and old_price is not None and new_price is not None:
        try:
            drop_amount = int(old_price) - int(new_price)
        except (TypeError, ValueError):
            drop_amount = None
    percent = meta.get("drop_percent")
    percent_part = f" (−{_esc(percent)}%)" if percent is not None else ""

    lines = [
        "<b>PRICE DROP</b>",
        "",
        _esc(meta.get("name") or "Товар"),
        "",
        f"Было: {_esc(_fmt_price(old_price))}",
        f"Стало: {_esc(_fmt_price(new_price))}",
    ]
    if drop_amount is not None:
        lines.append(
            f"Снижение: {_esc(_fmt_price(drop_amount))}{percent_part}"
        )
    lines.append("")
    lines.append(f"Магазин: {_esc(_store_label(meta.get('store')))}")
    if meta.get("is_historical_low"):
        lines.append("")
        lines.append("Также это новый исторический минимум.")
        prev_low = meta.get("previous_historical_low") or meta.get(
            "previous_historical_min"
        )
        if prev_low is not None:
            lines.append(f"Предыдущий минимум: {_esc(_fmt_price(prev_low))}")
    if meta.get("url"):
        lines.append("")
        lines.append(_url_line(meta["url"]))
    return "\n".join(lines)


def format_historical_low(event: Mapping[str, Any]) -> str:
    meta = _meta(event)
    prev_min = meta.get("previous_historical_min")
    if prev_min is None:
        prev_min = event.get("old_price")
    lines = [
        "<b>NEW HISTORICAL LOW</b>",
        "",
        _esc(meta.get("name") or "Товар"),
        "",
        f"Новая цена: {_esc(_fmt_price(event.get('new_price')))}",
        f"Предыдущий минимум: {_esc(_fmt_price(prev_min))}",
        "",
        f"Магазин: {_esc(_store_label(meta.get('store')))}",
    ]
    if meta.get("url"):
        lines.append("")
        lines.append(_url_line(meta["url"]))
    return "\n".join(lines)


def format_cross_store(event: Mapping[str, Any]) -> str:
    meta = _meta(event)
    lines = [
        "<b>CROSS-STORE SAVING</b>",
        "",
        _esc(meta.get("name") or meta.get("sku") or "Модель"),
        "",
        (
            f"{_esc(_store_label(meta.get('cheapest_store')))}: "
            f"{_esc(_fmt_price(meta.get('cheapest_price')))}"
        ),
        (
            f"{_esc(_store_label(meta.get('other_store')))}: "
            f"{_esc(_fmt_price(meta.get('other_price')))}"
        ),
        "",
        f"Экономия: {_esc(_fmt_price(meta.get('difference')))}",
        f"Дешевле: {_esc(_store_label(meta.get('cheapest_store')))}",
    ]
    if meta.get("cheapest_url"):
        lines.append("")
        lines.append(_url_line(meta["cheapest_url"]))
    if meta.get("other_url"):
        lines.append(_url_line(meta["other_url"]))
    return "\n".join(lines)


def format_alert_event(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    if event_type == EVENT_TARGET_PRICE:
        return format_target_price(event)
    if event_type == EVENT_PRICE_DROP:
        return format_price_drop(event)
    if event_type == EVENT_HISTORICAL_LOW:
        return format_historical_low(event)
    if event_type == EVENT_CROSS_STORE:
        return format_cross_store(event)
    meta = _meta(event)
    return (
        f"<b>{_esc(event_type or 'ALERT')}</b>\n\n"
        f"{_esc(meta.get('name') or 'Событие')}"
    )


def format_test_message() -> str:
    return (
        "<b>Laptop Monitor</b>\n\n"
        "Telegram подключён.\n"
        "Доставка работает."
    )


def format_price_update_group(events: Sequence[Mapping[str, Any]]) -> str:
    """Grouped PRICE UPDATE for one matched model (drops + optional cross/target)."""
    drops = [e for e in events if e.get("event_type") == EVENT_PRICE_DROP]
    crosses = [e for e in events if e.get("event_type") == EVENT_CROSS_STORE]
    targets = [e for e in events if e.get("event_type") == EVENT_TARGET_PRICE]

    title_meta = _meta(drops[0] if drops else events[0])
    name = title_meta.get("name") or "Модель"
    # Prefer shorter marketing name from Regard if present.
    for event in drops:
        meta = _meta(event)
        candidate = meta.get("name")
        if candidate and (
            str(meta.get("store") or "").lower() == "regard"
            or len(str(candidate)) < len(str(name))
        ):
            name = candidate

    gpu = None
    for event in list(drops) + list(targets):
        gpu = _meta(event).get("gpu")
        if gpu:
            break

    lines = [
        "<b>PRICE UPDATE</b>",
        "",
        _esc(name),
    ]
    if gpu:
        lines.append(_esc(gpu))
    lines.append("")

    # Store drops: ANDPRO then Regard for readability, else by store name.
    def store_sort(event: Mapping[str, Any]) -> str:
        return str(_meta(event).get("store") or "")

    for event in sorted(drops, key=store_sort):
        meta = _meta(event)
        store = _store_label(meta.get("store"))
        old_price = event.get("old_price")
        new_price = event.get("new_price")
        drop_amount = meta.get("drop_amount")
        if drop_amount is None and old_price is not None and new_price is not None:
            try:
                drop_amount = int(old_price) - int(new_price)
            except (TypeError, ValueError):
                drop_amount = None
        percent = meta.get("drop_percent")
        percent_part = f" (−{_esc(percent)}%)" if percent is not None else ""
        lines.append(_esc(store))
        lines.append(
            f"{_esc(_fmt_price(old_price))} → {_esc(_fmt_price(new_price))}"
        )
        if drop_amount is not None:
            lines.append(
                f"−{_esc(_fmt_price(drop_amount))}{percent_part}"
            )
        if meta.get("is_historical_low"):
            lines.append("Новый исторический минимум")
        lines.append("")

    if crosses:
        cross = crosses[0]
        meta = _meta(cross)
        cheapest_store = meta.get("cheapest_store")
        cheapest_price = meta.get("cheapest_price")
        other_store = meta.get("other_store")
        other_price = meta.get("other_price")
        difference = meta.get("difference")
        lines.append(f"Сейчас дешевле: {_esc(_store_label(cheapest_store))}")
        lines.append(_esc(_fmt_price(cheapest_price)))
        lines.append("")
        lines.append(
            f"Разница с {_esc(_store_label(other_store))}: "
            f"{_esc(_fmt_price(difference))}"
        )
        lines.append("")

    for event in targets:
        meta = _meta(event)
        lines.append("<b>TARGET PRICE</b>")
        lines.append(
            f"{_esc(_store_label(meta.get('store')))}: "
            f"{_esc(_fmt_price(event.get('new_price')))}"
        )
        lines.append(f"Порог: {_esc(_fmt_price(meta.get('threshold')))}")
        lines.append("")

    urls: list[str] = []
    for event in drops:
        url = _meta(event).get("url")
        if url and url not in urls:
            urls.append(str(url))
    for event in crosses:
        meta = _meta(event)
        for key in ("cheapest_url", "other_url"):
            url = meta.get(key)
            if url and url not in urls:
                urls.append(str(url))
    for url in urls:
        lines.append(_url_line(url))

    # Trim trailing blank lines.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def format_notification_group(group: Any) -> str:
    """Format NotificationGroup or fall back to single-event formatters."""
    kind = getattr(group, "kind", None) or (group.get("kind") if isinstance(group, dict) else None)
    events = getattr(group, "events", None) or (
        group.get("events") if isinstance(group, dict) else None
    )
    if not events:
        return "<b>ALERT</b>"
    if kind == "price_update" or (
        len(events) > 1 and any(e.get("event_type") == EVENT_PRICE_DROP for e in events)
    ):
        return format_price_update_group(events)
    return format_alert_event(events[0])
