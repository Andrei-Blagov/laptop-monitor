"""Compact Telegram cards from a stored D/E result. No second ranking or store call."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

MESSAGE_LIMIT = 3500

_AVAILABILITY = {
    "in_stock": "в наличии",
    "online_available": "доступно онлайн",
    "store_pickup_only": "только самовывоз",
    "out_of_stock": "нет в наличии",
    "preorder": "предзаказ",
    "unknown": "наличие не подтверждено",
}


def format_market_messages(
    result: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    purchase_current: bool,
    info_current: bool,
) -> list[dict[str, Any]]:
    """Cards for one requested view. Parts stay under the Telegram size limit."""
    view = str(event.get("requested_view") or "general_top")
    sections = _sections(result, view, purchase_current=purchase_current, info_current=info_current)
    parts: list[dict[str, Any]] = []
    for name, text in sections:
        for index, chunk in enumerate(_chunks(text)):
            parts.append({
                "part_id": f"{name}:{index}",
                "text": chunk,
                "reply_markup": None,
            })
    if parts:
        parts[-1]["reply_markup"] = {
            "inline_keyboard": [[{"text": "Обновить Таиланд", "callback_data": "ctrl:ptop_go"}]],
        }
    return parts


def _sections(result, view, *, purchase_current: bool, info_current: bool) -> list[tuple[str, str]]:
    comparison = result.get("comparison") or {}
    rankings = result.get("rankings") or {}
    blocks = {
        "thai_top": _thai_top(rankings, result, info_current=info_current),
        "cross": _cross(comparison, result, view, purchase_current=purchase_current, info_current=info_current),
        "unmatched": _unmatched(comparison, info_current=info_current),
    }
    order = ["thai_top", "cross", "unmatched"]
    if view == "personal_top3":
        order = ["cross", "thai_top", "unmatched"]
    elif view in {"general_top", "all"}:
        order = ["cross", "thai_top", "unmatched"]
    return [(name, blocks[name]) for name in order if blocks[name]]


def _thai_top(rankings: Mapping[str, Any], result: Mapping[str, Any], *, info_current: bool) -> str:
    rows = list(rankings.get("thai_top_value") or [])[:5]
    if not rows:
        return ""
    lines = ["Таиланд — самостоятельный TOP", _freshness_line(info_current, purchase=False)]
    for index, row in enumerate(rows, start=1):
        offer = (row.get("offers") or [{}])[0]
        lines.append("")
        lines.append(f"{index}. {_card_title(row.get('name') or offer.get('name'))}")
        if _personal(row) or _personal(offer):
            lines.append("Личный TOP-3: эталон, не ограничение поиска")
        lines.extend(_offer_lines(offer, row, result))
    lines.append("")
    lines.append(_fx_line(result))
    lines.append(_coverage_line(result))
    return "\n".join(line for line in lines if line is not None)


def _cross(comparison, result, view, *, purchase_current: bool, info_current: bool) -> str:
    pairs = _selected_pairs(comparison, view)
    confirmed = [pair for pair in pairs if pair.get("comparison_status") == "confirmed"][:5]
    extras = [
        pair for pair in (comparison.get("pairs") or [])
        if pair not in confirmed and pair.get("match_class") in {"ALTERNATIVE", "SAME_FAMILY"}
    ][:2]
    shown = (confirmed or pairs[:3]) + extras
    if not shown:
        return ""
    lines = ["Россия ↔ Таиланд", _freshness_line(info_current, purchase=not purchase_current)]
    for pair in shown:
        th = pair.get("th_observation") or {}
        ru = pair.get("ru_observation") or {}
        lines.append("")
        lines.append(_card_title(th.get("name") or "Тайское предложение"))
        lines.append(_relation_line(pair))
        if _personal_pair(pair):
            lines.append("Личный TOP-3: эталон для этой карточки, поиск им не ограничен")
        lines.extend(_offer_lines(th, {}, result, rub_price=ru.get("price"), rub_label="Россия"))
        lines.append(f"Магазин RU: {ru.get('source') or ru.get('store') or 'н/д'}")
        if ru.get("url"):
            lines.append(f"Ссылка RU: {ru['url']}")
        saving = _saving_line(pair, purchase_current=purchase_current)
        if saving:
            lines.append(saving)
        costs = pair.get("unknown_costs") or []
        if costs:
            lines.append("Неизвестные расходы не равны нулю: " + ", ".join(str(item) for item in costs))
        expenses = pair.get("confirmed_expenses") or []
        if expenses:
            shown_costs = ", ".join(
                f"{item.get('label')} {item.get('amount_rub')} ₽" for item in expenses if isinstance(item, dict)
            )
            if shown_costs:
                lines.append("Подтверждённые расходы: " + shown_costs)
        diffs = list(pair.get("conflicting_fields") or []) + list(pair.get("commercial_differences") or [])
        if diffs:
            lines.append("Отличия: " + ", ".join(str(item) for item in diffs))
    lines.append("")
    lines.append(_fx_line(result))
    lines.append(_coverage_line(comparison if comparison.get("limitations") or comparison.get("market_coverage_complete") is not None else result))
    return "\n".join(line for line in lines if line)


def _unmatched(comparison: Mapping[str, Any], *, info_current: bool) -> str:
    rows = list(comparison.get("unmatched_value_top") or [])
    if len(rows) < 3:
        for row in comparison.get("unmatched_th") or []:
            if row not in rows:
                rows.append(row)
            if len(rows) >= 5:
                break
    rows = rows[:5]
    if not rows:
        return ""
    lines = [
        "Таиланд без подтверждённой российской пары",
        "Подтверждённой пары нет. Это не значит, что данные устарели или модели нет на всём рынке.",
        _freshness_line(info_current, purchase=False),
    ]
    for index, row in enumerate(rows, start=1):
        label = "Выгодная по порогу, не сигнал BUY" if row.get("unmatched_value_candidate") else "Подходящая модель"
        lines.append("")
        lines.append(f"{index}. {_card_title(row.get('name'))} — {label}")
        if row.get("url"):
            lines.append(f"Ссылка: {row['url']}")
        if row.get("price_thb"):
            lines.append(f"Цена: {row['price_thb']} ฿")
        if row.get("price_rub"):
            lines.append(f"Ориентир: {row['price_rub']} ₽")
        if row.get("gpu"):
            lines.append(f"GPU: {row['gpu']}")
        reason = row.get("reason") or "not_observed"
        lines.append(f"Почему нет пары: {reason}")
        if reason in {"limited_coverage", "not_observed"}:
            lines.append("Покрытие проверенных источников неполное. Это не доказательство отсутствия модели.")
    return "\n".join(lines)


def _selected_pairs(comparison: Mapping[str, Any], view: str) -> list[Mapping[str, Any]]:
    pairs = list(comparison.get("pairs") or [])
    if view == "personal_top3":
        return [pair for pair in pairs if _personal_pair(pair)]
    if view == "general_top":
        marked = [pair for pair in pairs if (pair.get("ru_membership") or {}).get("in_general_top")]
        return marked or pairs[:5]
    return pairs[:5]


def _offer_lines(offer: Mapping[str, Any], row: Mapping[str, Any], result: Mapping[str, Any], *, rub_price=None, rub_label="Ориентир") -> list[str]:
    lines = []
    store = offer.get("source") or offer.get("store") or row.get("source")
    if store:
        lines.append(f"Магазин: {store}")
    state = offer.get("availability_state") or row.get("availability_state")
    if state:
        lines.append(f"Наличие: {_AVAILABILITY.get(str(state), state)}")
    price = offer.get("price_thb") if offer.get("price_thb") is not None else offer.get("price")
    if price is None:
        price = row.get("price_thb")
    if price is not None and rub_label != "Россия":
        lines.append(f"Цена: {price} ฿")
    if row.get("price_rub") and rub_label != "Россия":
        lines.append(f"Ориентир: {row['price_rub']} ₽")
    if rub_price is not None:
        lines.append(f"{rub_label}: {rub_price} ₽")
    url = offer.get("url") or row.get("url")
    if url:
        lines.append(f"Ссылка: {url}")
    specs = _specs(offer)
    if specs:
        lines.append(specs)
    checked = offer.get("price_checked_at") or row.get("price_checked_at")
    if checked:
        lines.append(f"Цена проверена: {checked}")
    available = offer.get("availability_checked_at")
    if available:
        lines.append(f"Наличие проверено: {available}")
    return lines


def _specs(offer: Mapping[str, Any]) -> str:
    found = []
    labels = (("gpu", "GPU"), ("cpu", "CPU"), ("ram_gb", "RAM"), ("ssd_gb", "SSD"), ("screen_inch", "Экран"))
    for key, label in labels:
        value = _spec_value(offer, key)
        if value not in (None, ""):
            found.append(f"{label} {value}")
    return ", ".join(found)


def _spec_value(offer: Mapping[str, Any], key: str):
    raw = (offer.get("specs") or {}).get(key)
    if isinstance(raw, dict):
        return raw.get("value")
    return raw if raw is not None else offer.get(key)


def _relation_line(pair: Mapping[str, Any]) -> str:
    kind = str(pair.get("match_class") or "")
    if kind == "EXACT":
        return "Подтверждено: та же конфигурация и полный артикул."
    if kind == "EQUIVALENT":
        return "Региональные артикулы одной конфигурации. Это не один и тот же полный SKU и не формулировка «та же модель дешевле»."
    if kind in {"ALTERNATIVE", "SAME_FAMILY"}:
        return "Другая конфигурация. Это не «та же модель дешевле»."
    if pair.get("comparison_status") == "confirmed":
        return "Подтверждённое сопоставление."
    return "Сопоставление не подтверждено. Экономию как текущую выгоду не показываем."


def _saving_line(pair: Mapping[str, Any], *, purchase_current: bool) -> str:
    if pair.get("comparison_status") != "confirmed" or pair.get("match_class") in {"ALTERNATIVE", "SAME_FAMILY"}:
        return ""
    if not purchase_current:
        return "Текущая выгода не подтверждена: курс, свежесть сторон или статус сравнения этого не позволяют."
    saving = pair.get("saving_rub")
    if saving in (None, ""):
        return ""
    amount = Decimal(str(saving))
    percent = pair.get("saving_pct")
    band = _band(percent)
    return f"Подтверждённая разница: {amount} ₽{band}"


def _band(percent) -> str:
    if percent in (None, ""):
        return ""
    value = abs(Decimal(str(percent)))
    if value < 5:
        return ", приблизительно одинаково"
    if value < 10:
        return ", небольшое преимущество"
    return ", заметное преимущество"


def _freshness_line(info_current: bool, *, purchase: bool) -> str:
    if not info_current:
        return "Наблюдение не текущее. Время проверки указано у карточки."
    if purchase:
        return "Текущая выгода покупки не подтверждена. Сами наблюдения при этом не называются устаревшими."
    return "Информационные данные можно показывать. Отсутствие пары само по себе не делает их устаревшими."


def _fx_line(result: Mapping[str, Any]) -> str:
    fx = result.get("fx") or {}
    if not fx or not fx.get("rub_per_thb"):
        return "Курс для текущего RUB-сравнения отсутствует."
    return f"Курс: {fx.get('rub_per_thb')} ₽ за 1 ฿, {fx.get('source') or 'CBR'}, получен {fx.get('fetched_at') or 'н/д'}."


def _coverage_line(document: Mapping[str, Any]) -> str:
    limits = list(document.get("limitations") or [])
    if document.get("market_coverage_complete") is False or limits:
        detail = ", ".join(limits) if limits else "проверены не все источники"
        return f"Покрытие неполное ({detail}). Это не минимум всего рынка и не доказательство отсутствия модели."
    return "Покрытие относится только к проверенным подключённым источникам."


def _personal(row: Mapping[str, Any]) -> bool:
    return bool(row.get("in_personal_top3"))


def _personal_pair(pair: Mapping[str, Any]) -> bool:
    return bool((pair.get("ru_membership") or {}).get("in_personal_top3"))


def _card_title(name: object) -> str:
    text = str(name or "Модель").replace("\n", " ").strip()
    return text or "Модель"


def _chunks(text: str) -> list[str]:
    if len(text) <= MESSAGE_LIMIT:
        return [text]
    parts: list[str] = []
    buffer = ""
    for block in text.split("\n\n"):
        piece = block if not buffer else buffer + "\n\n" + block
        if buffer and len(piece) > MESSAGE_LIMIT:
            parts.append(buffer)
            buffer = block
        else:
            buffer = piece
    if buffer:
        parts.append(buffer)
    return parts or [text[:MESSAGE_LIMIT]]
