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
    now,
) -> list[dict[str, Any]]:
    """Cards for one requested view. Part ids stay put when the freshness line changes."""
    view = str(event.get("requested_view") or "general_top")
    cards = _cards(result, view, now)
    if not cards:
        cards = [("empty:0", "По сохранённому результату показывать нечего. Каталог пуст или подходящих предложений нет.")]
    parts: list[dict[str, Any]] = []
    for part_id, text in cards:
        chunks = _chunks(text)
        for index, chunk in enumerate(chunks):
            item_id = part_id if len(chunks) == 1 else f"{part_id}#{index}"
            parts.append({"part_id": item_id, "text": chunk, "reply_markup": None})
    parts[-1]["reply_markup"] = {
        "inline_keyboard": [[{"text": "Обновить Таиланд", "callback_data": "ctrl:ptop_go"}]],
    }
    return parts


def _cards(result: Mapping[str, Any], view: str, now) -> list[tuple[str, str]]:
    comparison = result.get("comparison") or {}
    rankings = result.get("rankings") or {}
    order = ["cross", "thai_top", "unmatched"] if view in {"general_top", "all", "personal_top3"} else ["thai_top", "cross", "unmatched"]
    built = {
        "thai_top": _thai_cards(rankings, result, now),
        "cross": _cross_cards(comparison, result, view, now),
        "unmatched": _unmatched_cards(comparison, now),
    }
    cards: list[tuple[str, str]] = []
    for name in order:
        cards.extend(built[name])
    coverage = _coverage_text(comparison, result)
    if coverage and cards:
        cards.append(("coverage:0", coverage))
    return cards


def _thai_cards(rankings: Mapping[str, Any], result: Mapping[str, Any], now) -> list[tuple[str, str]]:
    from market_contracts import ComparisonPolicy

    rows = list(rankings.get("thai_top_value") or [])[:5]
    cards = []
    for index, row in enumerate(rows, start=1):
        offer = _best_offer(row)
        fresh = _times_fresh(offer, now, ComparisonPolicy().quote_hours)
        lines = [f"Таиланд — самостоятельный TOP {index}. {_card_title(row.get('name') or offer.get('name'))}"]
        if fresh:
            lines.append("Свежие данные без российской пары остаются информационными.")
        else:
            lines.append("Эта карточка не текущая. Время проверки указано ниже.")
        if _personal(row) or _personal(offer):
            lines.append("Личный TOP-3: эталон, не ограничение поиска")
        lines.extend(_offer_lines(offer, row, result))
        lines.append(_fx_line(result))
        cards.append((f"thai:{row.get('model_id') or index}", "\n".join(lines)))
    return cards


def _cross_cards(comparison, result, view, now) -> list[tuple[str, str]]:
    confirmed = _confirmed_pairs(comparison, view)[:5]
    confirmed_ids = {id(pair) for pair in confirmed}
    extras = [
        pair for pair in (comparison.get("pairs") or [])
        if id(pair) not in confirmed_ids and pair.get("match_class") in {"ALTERNATIVE", "SAME_FAMILY"}
    ][:2]
    cards = []
    for pair in confirmed + extras:
        purchase = _pair_purchase(pair, result, now)
        info = _pair_info_fresh(pair, now)
        th = pair.get("th_observation") or {}
        ru = pair.get("ru_observation") or {}
        lines = ["Россия ↔ Таиланд", _card_title(th.get("name") or "Тайское предложение"), _relation_line(pair)]
        if info and not purchase:
            lines.append("Текущая выгода не подтверждена. Наблюдения из-за этого не называются устаревшими.")
        elif not info:
            lines.append("Эта пара не текущая. Время проверки сторон указано ниже.")
        if _personal_pair(pair):
            lines.append("Личный TOP-3: эталон для этой карточки, поиск им не ограничен")
        lines.extend(_pair_price_lines(pair, result, purchase_current=purchase))
        cards.append((f"pair:{pair.get('pair_id')}", "\n".join(line for line in lines if line)))
    return cards


def _pair_price_lines(pair: Mapping[str, Any], result: Mapping[str, Any], *, purchase_current: bool) -> list[str]:
    th = pair.get("th_observation") or {}
    ru = pair.get("ru_observation") or {}
    gap = pair.get("price_gap") or {}
    lines = [f"Магазин TH: {th.get('source') or th.get('store') or 'н/д'}"]
    state = th.get("availability_state")
    if state:
        lines.append(f"Наличие TH: {_AVAILABILITY.get(str(state), state)}")
    th_thb = th.get("price")
    th_rub = gap.get("thai_base_rub")
    if th_thb is not None:
        rub = f" ({th_rub} ₽)" if th_rub not in (None, "") else ""
        lines.append(f"Цена TH: {th_thb} ฿{rub}")
    ru_rub = ru.get("price") if ru.get("price") is not None else gap.get("ru_price_rub")
    if ru_rub is not None:
        lines.append(f"Цена RU: {ru_rub} ₽")
    lines.append(f"Магазин RU: {ru.get('source') or ru.get('store') or 'н/д'}")
    if th.get("url"):
        lines.append(f"Ссылка TH: {th['url']}")
    if ru.get("url"):
        lines.append(f"Ссылка RU: {ru['url']}")
    resolution = _spec_value(th, "screen_resolution")
    if resolution:
        lines.append(f"Разрешение: {resolution}")
    specs = _specs(th)
    if specs:
        lines.append(specs)
    if th.get("price_checked_at"):
        lines.append(f"Цена TH проверена: {th['price_checked_at']}")
    if th.get("availability_checked_at"):
        lines.append(f"Наличие TH проверено: {th['availability_checked_at']}")
    if ru.get("price_checked_at"):
        lines.append(f"Цена RU проверена: {ru['price_checked_at']}")
    if ru.get("availability_checked_at"):
        lines.append(f"Наличие RU проверено: {ru['availability_checked_at']}")
    saving = _saving_line(pair, purchase_current=purchase_current)
    if saving:
        lines.append(saving)
    costs = pair.get("unknown_costs") or []
    if costs:
        lines.append("Неизвестные расходы не равны нулю: " + ", ".join(str(item) for item in costs))
    expenses = pair.get("confirmed_expenses") or []
    shown_costs = ", ".join(
        f"{item.get('label')} {item.get('amount_rub')} ₽" for item in expenses if isinstance(item, dict)
    )
    if shown_costs:
        lines.append("Подтверждённые расходы: " + shown_costs)
    diffs = list(pair.get("conflicting_fields") or []) + list(pair.get("commercial_differences") or [])
    if diffs:
        lines.append("Отличия: " + ", ".join(str(item) for item in diffs))
    lines.append(_fx_line(result))
    return lines


def _unmatched_cards(comparison: Mapping[str, Any], now) -> list[tuple[str, str]]:
    del now
    rows = list(comparison.get("unmatched_value_top") or [])
    if len(rows) < 3:
        for row in comparison.get("unmatched_th") or []:
            if row not in rows:
                rows.append(row)
            if len(rows) >= 5:
                break
    cards = []
    for index, row in enumerate(rows[:5], start=1):
        label = "Выгодная по порогу, не сигнал BUY" if row.get("unmatched_value_candidate") else "Подходящая модель"
        lines = [
            "Таиланд без подтверждённой российской пары",
            "Подтверждённой пары нет. Это не значит, что данные устарели или модели нет на всём рынке.",
            f"{index}. {_card_title(row.get('name'))} — {label}",
        ]
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
        cards.append((f"unmatched:{row.get('variant_id') or row.get('model_id') or index}", "\n".join(lines)))
    return cards


def _confirmed_pairs(comparison: Mapping[str, Any], view: str) -> list[Mapping[str, Any]]:
    """D already chose the confirmed pair for each Thai variant. Do that before the card cap."""
    pairs = list(comparison.get("pairs") or [])
    by_id = {str(pair.get("pair_id")): pair for pair in pairs}
    chosen = []
    for pair_id in (comparison.get("best_pairs_by_th_variant_id") or {}).values():
        pair = by_id.get(str(pair_id))
        if pair is not None and _pair_in_view(pair, view):
            chosen.append(pair)
    return chosen


def _pair_in_view(pair: Mapping[str, Any], view: str) -> bool:
    if view == "personal_top3":
        return _personal_pair(pair)
    if view == "general_top":
        return bool((pair.get("ru_membership") or {}).get("in_general_top"))
    return True


def _best_offer(row: Mapping[str, Any]) -> Mapping[str, Any]:
    offers = [offer for offer in row.get("offers") or [] if isinstance(offer, dict)]
    wanted = str(row.get("best_offer_id") or "")
    for offer in offers:
        if str(offer.get("offer_id") or "") == wanted:
            return offer
    return offers[0] if offers else {}


def _times_fresh(offer: Mapping[str, Any], now, hours: int) -> bool:
    from market_contracts import age_valid, moment

    return age_valid(moment(offer.get("price_checked_at")), now, hours) and age_valid(
        moment(offer.get("availability_checked_at")), now, hours
    )


def _pair_info_fresh(pair: Mapping[str, Any], now) -> bool:
    from market_contracts import ComparisonPolicy

    policy = ComparisonPolicy()
    th = pair.get("th_observation") or {}
    ru = pair.get("ru_observation") or {}
    return _times_fresh(th, now, policy.quote_hours) and _times_fresh(ru, now, policy.ru_quote_hours)


def _pair_purchase(pair: Mapping[str, Any], result: Mapping[str, Any], now) -> bool:
    from market_contracts import ComparisonPolicy
    from thailand.market_events import _fx_usable

    if pair.get("comparison_status") != "confirmed" or pair.get("match_class") in {"ALTERNATIVE", "SAME_FAMILY"}:
        return False
    fx = result.get("fx") or {}
    if not fx or not _fx_usable(fx, now, ComparisonPolicy()):
        return False
    return _pair_info_fresh(pair, now)


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
    labels = (
        ("gpu", "GPU"), ("cpu", "CPU"), ("ram_gb", "RAM"), ("ssd_gb", "SSD"),
        ("screen_inch", "Экран"), ("screen_resolution", "Разрешение"),
    )
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
    if amount > 0:
        direction = "Таиланд дешевле России"
    elif amount < 0:
        direction = "Россия дешевле Таиланда"
    else:
        direction = "Стороны совпали по сумме"
    pct = "" if percent in (None, "") else f", {percent}%"
    return f"Подтверждённая разница: {direction}, {abs(amount)} ₽{pct}{_band(percent)}"


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


def _coverage_text(comparison: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    notes = []
    coverage = comparison.get("source_coverage") or {}
    groups = coverage.items() if isinstance(coverage, dict) else (("TH", coverage),)
    for side, rows in groups:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or "")
            if state in {"", "ok"}:
                continue
            notes.append(f"{side} {row.get('source') or row.get('store')}: {state}")
    for row in result.get("coverage") or []:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or row.get("reason") or "")
        if state in {"", "ok"}:
            continue
        notes.append(f"{row.get('source') or 'источник'}: {state}")
    limits = list(comparison.get("limitations") or [])
    if comparison.get("market_coverage_complete") is False or limits or notes:
        detail = ", ".join([*notes, *limits]) or "проверены не все источники"
        return f"Покрытие неполное ({detail}). Это не минимум всего рынка и не доказательство отсутствия модели."
    if notes:
        return "Покрытие: " + ", ".join(notes)
    return ""


def _personal(row: Mapping[str, Any]) -> bool:
    return bool(row.get("in_personal_top3"))


def _personal_pair(pair: Mapping[str, Any]) -> bool:
    return bool((pair.get("ru_membership") or {}).get("in_personal_top3"))


def _card_title(name: object) -> str:
    text = str(name or "Модель").replace("\n", " ").strip()
    return text or "Модель"


def _chunks(text: str) -> list[str]:
    blocks = []
    for block in text.split("\n\n"):
        while len(block) > MESSAGE_LIMIT:
            blocks.append(block[:MESSAGE_LIMIT])
            block = block[MESSAGE_LIMIT:]
        if block:
            blocks.append(block)
    if not blocks:
        return [""]
    parts: list[str] = []
    buffer = ""
    for block in blocks:
        piece = block if not buffer else buffer + "\n\n" + block
        if buffer and len(piece) > MESSAGE_LIMIT:
            parts.append(buffer)
            buffer = block
        else:
            buffer = piece
    if buffer:
        parts.append(buffer)
    return parts
