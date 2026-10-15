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
    """Cards for one requested view.

    Body chunks always keep ``#index``. The freshness sentence is a separate
    part whose id includes current or historical, so a wording change cannot
    collapse ``card#0``/``card#1`` into a bare ``card``.
    """
    parts = _render_parts(result, event, now)
    if not parts:
        parts = [{
            "part_id": "empty#0",
            "text": "По сохранённому результату показывать нечего. Каталог пуст или подходящих предложений нет.",
            "reply_markup": None,
        }]
    keyboard = {"inline_keyboard": [[{"text": "Обновить Таиланд", "callback_data": "ctrl:ptop_go"}]]}
    for part in parts:
        part["reply_markup"] = keyboard
    return parts


def iter_market_sends(result: Mapping[str, Any], event: Mapping[str, Any], now_fn):
    """Yield one part at a time. ``now_fn`` runs immediately before that part is rendered."""
    recipients = [str(item).strip() for item in (event.get("recipients") or []) if str(item).strip()]
    keyboard = {"inline_keyboard": [[{"text": "Обновить Таиланд", "callback_data": "ctrl:ptop_go"}]]}
    if str(result.get("status") or "") == "failed":
        text = _failure_text(result)
        for recipient in recipients:
            for index, chunk in enumerate(_chunks(text)):
                now_fn()
                yield {
                    "recipient": recipient,
                    "part_id": f"{recipient}:failed#{index}",
                    "text": chunk,
                    "reply_markup": keyboard,
                }
        return
    skeleton = _body_plan(result, str(event.get("requested_view") or "general_top"))
    product_cards = [spec for spec in skeleton if spec.get("card_id") != "coverage"]
    if not product_cards:
        for recipient in recipients:
            for index, chunk in enumerate(_chunks(_empty_text(result))):
                now_fn()
                yield {
                    "recipient": recipient,
                    "part_id": f"{recipient}:empty#{index}",
                    "text": chunk,
                    "reply_markup": keyboard,
                }
        return
    for recipient in recipients:
        for spec in skeleton:
            now = now_fn()
            state_name = "info"
            if spec["kind"] == "body":
                text = spec["text"]
                local_id = spec["part_id"]
            else:
                state_name, state_text = spec["state"](now)
                text = spec["text"]
                if state_text:
                    text = f"{text}\n{state_text}"
                local_id = spec["part_id"]
            yield {
                "recipient": recipient,
                "part_id": f"{recipient}:{local_id}",
                "text": text,
                "state": state_name,
                "reply_markup": keyboard,
            }


def _render_parts(result, event, now) -> list[dict[str, Any]]:
    if str(result.get("status") or "") == "failed":
        return [{"part_id": f"failed#{i}", "text": chunk, "reply_markup": None}
                for i, chunk in enumerate(_chunks(_failure_text(result)))]
    plan = _body_plan(result, str(event.get("requested_view") or "general_top"))
    if not any(spec.get("card_id") != "coverage" for spec in plan):
        return [{"part_id": f"empty#{i}", "text": chunk, "reply_markup": None}
                for i, chunk in enumerate(_chunks(_empty_text(result)))]
    rendered = []
    for spec in plan:
        text = spec["text"]
        if spec.get("kind") == "card" and spec.get("state"):
            _name, state_text = spec["state"](now)
            if state_text:
                text = f"{text}\n{state_text}"
        rendered.append({"part_id": spec["part_id"], "text": text, "reply_markup": None})
    return rendered


def _empty_text(result) -> str:
    text = "По сохранённому результату показывать нечего. Каталог пуст или подходящих предложений нет."
    coverage = _coverage_text(result.get("comparison") or {}, result)
    return f"{text}\n{coverage}" if coverage else text


def _body_plan(result: Mapping[str, Any], view: str) -> list[dict[str, Any]]:
    comparison = result.get("comparison") or {}
    rankings = result.get("rankings") or {}
    order = ["cross", "thai_top", "unmatched"] if view in {"general_top", "all", "personal_top3"} else ["thai_top", "cross", "unmatched"]
    built = {
        "thai_top": _thai_cards(rankings, result),
        "cross": _cross_cards(comparison, result, view),
        "unmatched": _unmatched_cards(comparison, result),
    }
    plan: list[dict[str, Any]] = []
    shown: dict[str, int] = {}
    for name in order:
        for card_id, body, state_fn, key in built[name]:
            relation = _relation_line_from(body)
            if key in shown:
                previous = plan[shown[key]]
                if relation and relation not in previous["text"]:
                    previous["text"] += "\n" + relation
                continue
            chunks = _chunks(body)
            for index, chunk in enumerate(chunks):
                plan.append({
                    "kind": "body" if index < len(chunks) - 1 else "card",
                    "part_id": f"{card_id}#{index}",
                    "text": chunk,
                    "card_id": card_id,
                    "state": state_fn,
                })
            shown[key] = len(plan) - 1
    coverage = _coverage_text(comparison, result)
    if coverage:
        if plan and len(plan[-1]["text"]) + len(coverage) + 1 <= MESSAGE_LIMIT - 512:
            plan[-1]["text"] = f"{plan[-1]['text']}\n{coverage}"
        else:
            for index, chunk in enumerate(_chunks(coverage)):
                plan.append({"kind": "body", "part_id": f"coverage#{index}", "text": chunk, "card_id": "coverage"})
    return plan


def _model_key(row: Mapping[str, Any], offer: Mapping[str, Any], fallback: str) -> str:
    # D assigns distinct variant IDs to conflicting/regional configurations.
    # A printed SKU or a display title cannot establish model identity.
    variant = row.get("th_variant_id") or row.get("variant_id")
    if variant:
        return f"variant:{variant}"
    offer_id = row.get("th_offer_id") or offer.get("offer_id")
    return f"offer:{offer_id}" if offer_id else fallback


def _relation_line_from(body: str) -> str:
    for line in body.splitlines():
        if line.startswith(("Подтверждено:", "Региональные", "Другая конфигурация", "Подтверждённой пары нет")):
            return line.strip()
    return ""


def _failure_text(result: Mapping[str, Any]) -> str:
    lines = ["Обновление не удалось."]
    for error in result.get("errors") or []:
        if error:
            lines.append(str(error))
    coverage = _coverage_text(result.get("comparison") or {}, result)
    if coverage:
        lines.append(coverage)
    return "\n".join(lines)


def _thai_cards(rankings: Mapping[str, Any], result: Mapping[str, Any]) -> list[tuple]:
    rows = list(rankings.get("thai_top_value") or [])[:5]
    cards = []
    for index, row in enumerate(rows, start=1):
        offer = _best_offer(row)
        lines = [f"Таиланд — самостоятельный TOP {index}. {_card_title(row.get('name') or offer.get('name'))}"]
        if _personal(row) or _personal(offer):
            lines.append("Личный TOP-3: эталон, не ограничение поиска")
        lines.extend(_offer_lines(offer, row, result))
        lines.append(_fx_line(result))
        card_id = f"thai:{row.get('model_id') or index}"
        cards.append((card_id, "\n".join(lines), lambda now, offer=offer: _thai_state(offer, now),
                      _model_key(row, offer, card_id)))
    return cards


def _thai_state(offer: Mapping[str, Any], now) -> tuple[str, str]:
    from market_contracts import ComparisonPolicy

    if _times_fresh(offer, now, ComparisonPolicy().quote_hours):
        return "info", "Свежие данные без российской пары остаются информационными."
    return "historical", "Эта карточка не текущая. Время проверки указано в карточке."


def _cross_cards(comparison, result, view) -> list[tuple]:
    confirmed = _confirmed_pairs(comparison, view)[:5]
    confirmed_ids = {id(pair) for pair in confirmed}
    extras = [
        pair for pair in (comparison.get("pairs") or [])
        if id(pair) not in confirmed_ids and pair.get("match_class") in {"ALTERNATIVE", "SAME_FAMILY"}
    ][:2]
    cards = []
    for pair in confirmed + extras:
        th = pair.get("th_observation") or {}
        lines = ["Россия ↔ Таиланд", _card_title(th.get("name") or "Тайское предложение"), _relation_line(pair)]
        if _personal_pair(pair):
            lines.append("Личный TOP-3: эталон для этой карточки, поиск им не ограничен")
        lines.extend(_pair_price_lines(pair, result, purchase_current=False))
        card_id = f"pair:{pair.get('pair_id')}"
        cards.append((card_id, "\n".join(line for line in lines if line), lambda now, pair=pair, result=result: _cross_state(pair, result, now),
                      _model_key(pair, th, card_id)))
    return cards


def _cross_state(pair, result, now) -> tuple[str, str]:
    if not _pair_info_fresh(pair, now):
        return "historical", "Эта пара не текущая. Время проверки сторон указано в карточке."
    if not _pair_purchase(pair, result, now):
        return "info", "Текущая выгода не подтверждена. Наблюдения из-за этого не называются устаревшими."
    return "current", _saving_line(pair, purchase_current=True) or "Подтверждённая текущая выгода."


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


def _unmatched_cards(comparison: Mapping[str, Any], result: Mapping[str, Any]) -> list[tuple]:
    rows = list(comparison.get("unmatched_value_top") or [])
    if len(rows) < 3:
        for row in comparison.get("unmatched_th") or []:
            if row not in rows:
                rows.append(row)
            if len(rows) >= 5:
                break
    cards = []
    for index, row in enumerate(rows[:5], start=1):
        offer = _best_offer(row)
        lines = [
            "Таиланд без подтверждённой российской пары",
            "Подтверждённой пары нет. Само по себе это не означает, что цена устарела или модели нет на всём рынке.",
            f"{index}. {_card_title(row.get('name') or offer.get('name'))}",
        ]
        lines.extend(_offer_lines(offer, row, result))
        reason = row.get("reason") or "not_observed"
        lines.append(f"Почему нет пары: {reason}")
        if reason in {"limited_coverage", "not_observed"}:
            lines.append("Покрытие проверенных источников неполное. Это не доказательство отсутствия модели.")
        card_id = f"unmatched:{row.get('variant_id') or row.get('model_id') or offer.get('offer_id') or index}"
        cards.append((card_id, "\n".join(lines), lambda now, row=row, offer=offer, result=result: _unmatched_state(row, offer, result, now),
                      _model_key(row, offer, card_id)))
    return cards


def _unmatched_state(row, offer, result, now) -> tuple[str, str]:
    from market_contracts import ComparisonPolicy
    from thailand.market_events import _fx_usable

    fresh = _times_fresh(offer, now, ComparisonPolicy().quote_hours)
    fx_ok = bool(result.get("fx")) and _fx_usable(result.get("fx") or {}, now, ComparisonPolicy())
    if row.get("unmatched_value_candidate") and (not fresh or not fx_ok):
        return "historical", "Выгодная оценка историческая, не сигнал BUY. Отсутствие пары само по себе цену не устаревает."
    if row.get("unmatched_value_candidate"):
        return "current", "Выгодная по порогу, не сигнал BUY. Отсутствие пары не означает устаревание."
    if not fresh:
        return "historical", "Цена и наличие этой карточки исторические. Отсутствие пары само по себе этого не означает."
    return "info", "Подходящая модель. Отсутствие пары не означает устаревание."


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
    seen = set()

    def add(source: str, note: str) -> None:
        key = f"{source}: {note}"
        if key not in seen:
            seen.add(key)
            notes.append(key)

    for row in list(result.get("coverage") or []):
        if isinstance(row, dict):
            _coverage_note(row, add)
    grouped = comparison.get("source_coverage") or {}
    groups = grouped.items() if isinstance(grouped, dict) else (("TH", grouped),)
    for _side, rows in groups:
        for row in rows or []:
            if isinstance(row, dict):
                _coverage_note(row, add)
    limits = [str(item) for item in (comparison.get("limitations") or [])]
    if not notes and not limits and comparison.get("market_coverage_complete") is not False:
        return ""
    detail = ", ".join([*notes, *limits]) or "проверены не все источники"
    return f"Покрытие неполное ({detail}). Это не минимум всего рынка и не доказательство отсутствия модели."


def _coverage_note(row: Mapping[str, Any], add) -> None:
    source = str(row.get("source") or row.get("store") or "источник")
    state = str(row.get("state") or row.get("status") or "")
    limitations = [str(item) for item in (row.get("limitations") or [])]
    if state in {"", "ok"} and "not_attempted" not in limitations and "rate_limited" not in limitations:
        return
    if state == "deferred" and row.get("policy_disabled"):
        return
    if state == "429" or "rate_limited" in limitations or state == "rate_limited":
        add(source, "429")
        return
    if state == "partial" or any(item.startswith("partial") for item in limitations):
        add(source, "partial")
        return
    if "not_attempted" in limitations or state in {"failed", "skipped"}:
        add(source, "не проверен")
        return
    if state not in {"", "ok"}:
        add(source, state)


def _personal(row: Mapping[str, Any]) -> bool:
    return bool(row.get("in_personal_top3"))


def _personal_pair(pair: Mapping[str, Any]) -> bool:
    return bool((pair.get("ru_membership") or {}).get("in_personal_top3"))


def _card_title(name: object) -> str:
    text = str(name or "Модель").replace("\n", " ").strip()
    return text or "Модель"


def _chunks(text: str) -> list[str]:
    # Reserve space for the freshness sentence rendered at send time.
    limit = MESSAGE_LIMIT - 512
    blocks = []
    for block in text.split("\n\n"):
        while len(block) > limit:
            blocks.append(block[:limit])
            block = block[limit:]
        if block:
            blocks.append(block)
    if not blocks:
        return [""]
    parts: list[str] = []
    buffer = ""
    for block in blocks:
        piece = block if not buffer else buffer + "\n\n" + block
        if buffer and len(piece) > limit:
            parts.append(buffer)
            buffer = block
        else:
            buffer = piece
    if buffer:
        parts.append(buffer)
    return parts
