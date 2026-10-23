"""Read-only navigation of saved configuration-bound history, including delisted offers."""
from __future__ import annotations

import hashlib
import html
from pathlib import Path

from confirmed_history import record_key
from model_price_history import format_msk, format_rub, historical_min_for_history
from storage import open_db_readonly

PAGE_SIZE = 8
PAGE = "ha:page:"
PICK = "ha:pick:"
CHART = "ha:chart:"


def _token(key):
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def entries(db_path):
    """Group only by the full persisted key; legacy stays per listing, unbound.

    Historical metadata is used only when it reproduces the persisted key.
    A reused current product row cannot rename an older configuration.
    """
    try:
        conn = open_db_readonly(db_path)
    except FileNotFoundError:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT h.*,p.store,p.external_id FROM price_history h JOIN products p ON p.id=h.product_id "
            "ORDER BY h.checked_at,h.id")]
    finally:
        conn.close()
    grouped = {}
    for row in rows:
        key = row.get("configuration_key")
        identity = str(key) if key else "legacy:" + str(row["product_id"])
        entry = grouped.setdefault(identity, {"identity": identity, "key": key, "rows": [], "metadata": None})
        entry["rows"].append(row)
    # Existing immutable snapshots supply labels, not fresh quote eligibility.
    from market_snapshot import load_russian_market_snapshot
    root = Path(db_path).resolve().parent / "russian_market"
    for path in sorted((root / "snapshots").glob("*.json"), reverse=True)[:100]:
        try:
            document = load_russian_market_snapshot(root, snapshot_id=path.stem)
            for record in document.get("offers", []):
                key = record_key(record)
                item = grouped.get(key)
                if item and item["metadata"] is None and any(
                    r["store"] == record.get("store") and r["external_id"] == record.get("external_id")
                    for r in item["rows"]
                ):
                    item["metadata"] = record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    output = []
    for item in grouped.values():
        item["token"] = _token(item["identity"])
        meta = item["metadata"] or {}
        item["label"] = (str(meta.get("product_name") or "")[:100] if item["key"] and meta else
                         "Комплектация " + str(item["key"])[:12] if item["key"] else
                         "Legacy без комплектации · запись " + str(item["rows"][0]["product_id"]))
        output.append(item)
    return sorted(output, key=lambda e: (not bool(e["key"]), e["rows"][-1]["checked_at"], e["identity"]), reverse=False)


def resolve(db_path, token):
    candidates = [entry for entry in entries(db_path) if entry["token"] == token]
    return candidates[0] if len(candidates) == 1 else None  # Never guess on a token collision.


def page(db_path, index=0):
    items = entries(db_path)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    if type(index) is not int or not 0 <= index < pages:
        return "Страница истории устарела. Откройте список заново.", {"inline_keyboard": [[{"text": "К истории", "callback_data": "ctrl:hist"}]]}
    chosen = items[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
    rows = [[{"text": e["label"][:56], "callback_data": PICK + e["token"]}] for e in chosen]
    navigation = []
    if index:
        navigation.append({"text": "←", "callback_data": PAGE + str(index - 1)})
    if index + 1 < pages:
        navigation.append({"text": "→", "callback_data": PAGE + str(index + 1)})
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "Меню", "callback_data": "hist:menu"}])
    text = (f"<b>Сохранённая история · {index + 1}/{pages}</b>\n"
            "Каждая комплектация отдельно; legacy не привязан к комплектации.\n"
            "Это наблюдения из базы, не актуальные предложения.") if items else "Сохранённых наблюдений пока нет."
    return text, {"inline_keyboard": rows}


def card(entry):
    rows = entry["rows"]
    latest = rows[-1]
    lines = ["<b>Сохранённая история</b>", html.escape(entry["label"]),
             "Последнее наблюдение: " + format_msk(latest["checked_at"]),
             "Исторические данные. Текущая цена и наличие здесь не подтверждаются."]
    if entry["key"]:
        lines.append("Ключ комплектации: " + str(entry["key"])[:12])
        meta = entry["metadata"]
        if meta:
            lines.append("SKU: " + html.escape(", ".join(map(str, meta.get("sku_candidates") or []))[:150]))
            specs = {key: value.get("value") for key, value in (meta.get("specs") or {}).items()}
            lines.append(html.escape(" · ".join(f"{key}: {specs.get(key) or 'не подтверждено'}" for key in (
                "cpu", "gpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution"))[:800]))
        else:
            lines.append("Название/SKU этой старой комплектации не восстановлены; текущей карточке она не приписана.")
        minimum = historical_min_for_history(rows)
        lines.append("Последняя наблюдавшаяся цена: " + format_rub(latest.get("price")))
        lines.append("Минимум этой комплектации: " + format_rub(minimum[0] if minimum else None))
    else:
        lines.append("Legacy: состав товара неизвестен. Эти записи не объединены с подтверждённой комплектацией.")
        lines.extend(format_msk(row["checked_at"]) + " · " + format_rub(row.get("price")) for row in rows[-8:])
    buttons = [[{"text": "График 30 дней", "callback_data": CHART + "30:" + entry["token"]},
                {"text": "Всё время", "callback_data": CHART + "all:" + entry["token"]}]] if entry["key"] else []
    buttons.append([{"text": "К сохранённой истории", "callback_data": PAGE + "0"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def chart(entry, period):
    from datetime import datetime, timezone
    from comparison import Offer, ProductMatch
    from price_history_chart import (build_store_series_for_cluster, earliest_valid_timestamp,
                                    period_start, render_price_history_png)
    if period not in {"30", "all"} or not entry["key"]:
        raise ValueError("unsupported historical chart")
    histories = {}
    offers = []
    for row in entry["rows"]:
        pid = row["product_id"]
        if pid not in histories:
            histories[pid] = []
            offers.append(Offer(store=row["store"], external_id=row["external_id"], name=entry["label"],
                                sku=None, price=None, available=False, url=None, product_id=pid))
        histories[pid].append(row)
    match = ProductMatch(normalized_sku=entry["key"], display_sku=entry["key"][:12],
                         name=entry["label"], offers=offers, match_method="persisted_configuration")
    now = datetime.now(timezone.utc)
    start = period_start(period, now=now, earliest=earliest_valid_timestamp(histories))
    series = build_store_series_for_cluster(match, histories, start=start, now=now)
    if not any(segment for store in series for segment in store.segments):
        raise ValueError("no observations in period")
    png = render_price_history_png(series, title=entry["label"], period_label=period,
                                   best_now=None, best_now_store=None, annotation_fresh_best=False)
    caption = "Сохранённая история · " + html.escape(entry["label"][:160]) + "\nТекущая цена не подтверждена; данные не продолжаются до сегодня."
    return png, caption
