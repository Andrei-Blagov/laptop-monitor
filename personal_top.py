"""Personal TOP-3: Russian prices plus one shared Thailand check.

The existing Thailand worker runs the scan. This module only chooses the
three models, decides whether a new scan is allowed, and formats the card.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import config
from product_identity import normalize_identifier
from russian_deals import _code_token, _exact_codes_in_text
from stores.common import extract_specs_from_name
from thailand.job_models import (
    TRIGGER_PERSONAL_DAILY,
    TRIGGER_PERSONAL_MANUAL,
    build_job,
)
from thailand.job_queue import enqueue_job, find_active_dedupe
from thailand.storage import atomic_write_json

DEDUPE_KEY = "personal_top"
STATE_VERSION = 1


def personal_models() -> list[dict[str, Any]]:
    return [dict(item) for item in getattr(config, "PERSONAL_TOP3", ()) or ()]


def state_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(getattr(config, "PERSONAL_TOP_STATE_PATH", "data/personal_top.json"))


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rub(value: int | None) -> str:
    if value is None:
        return "н/д"
    return f"{int(value):,}".replace(",", " ")


def _thb(value: int | None) -> str:
    if value is None:
        return "н/д"
    return f"{int(value):,}".replace(",", " ")


def product_codes(product: Mapping[str, Any]) -> set[str]:
    found = set(_exact_codes_in_text(str(product.get("name") or "")))
    token = _code_token(None if product.get("sku") is None else str(product.get("sku")))
    if token:
        found.add(token)
    return found


def model_codes(model: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for raw in model.get("identifiers") or ():
        token = _code_token(str(raw)) or normalize_identifier(str(raw))
        if token:
            found.add(token)
    return found


def match_products(model: Mapping[str, Any], products: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    wanted = model_codes(model)
    hits: list[dict[str, Any]] = []
    for product in products:
        if wanted & product_codes(product):
            hits.append(dict(product))
    return hits


def russian_side(
    model: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    *,
    history_mins: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    hits = match_products(model, products)
    priced = [row for row in hits if row.get("price") is not None and int(row["price"]) > 0]
    available = [row for row in priced if row.get("available")]
    pool = available or priced
    best = min(pool, key=lambda row: (int(row["price"]), str(row.get("store") or ""))) if pool else None
    offers = sorted(
        (
            {
                "store": str(row.get("store") or ""),
                "price": int(row["price"]),
                "url": row.get("url"),
                "available": bool(row.get("available")),
            }
            for row in priced
        ),
        key=lambda row: (row["price"], row["store"]),
    )
    hist = None
    if best is not None and history_mins and best.get("id") is not None:
        hist = history_mins.get(int(best["id"]))
    return {
        "found": best is not None,
        "store": None if best is None else str(best.get("store") or ""),
        "price": None if best is None else int(best["price"]),
        "url": None if best is None else best.get("url"),
        "name": None if best is None else str(best.get("name") or model.get("model") or ""),
        "available": bool(best and best.get("available")),
        "history_min": hist,
        "offers": offers,
    }


def target_for(model: Mapping[str, Any], russian: Mapping[str, Any]) -> dict[str, Any]:
    codes = sorted(model_codes(model))
    primary = codes[0] if codes else str(model.get("model") or "")
    specs = extract_specs_from_name(str(russian.get("name") or ""))
    return {
        "personal_key": str(model.get("key") or ""),
        "cluster_name": str(model.get("model") or ""),
        "brand": model.get("brand"),
        "canonical_model_code": primary,
        "model_codes": codes,
        "sku": primary,
        "mpn": primary,
        "alternative_part_numbers": codes[1:],
        "gpu": specs.get("gpu"),
        "cpu": specs.get("cpu"),
        "ram_gb": specs.get("ram_gb"),
        "ssd_gb": specs.get("ssd_gb"),
        "screen_size_inch": specs.get("screen_inch"),
        "screen_resolution": specs.get("screen_resolution"),
        "store": russian.get("store"),
        "price": russian.get("price"),
        "url": russian.get("url"),
    }


def confirmed_extra_rub() -> int:
    total = 0
    for item in getattr(config, "PERSONAL_TOP_CONFIRMED_EXTRAS_RUB", ()) or ():
        try:
            total += int(item.get("rub") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def unknown_cost_names() -> list[str]:
    return [str(item) for item in getattr(config, "PERSONAL_TOP_UNKNOWN_COSTS", ()) or ()]


def target_position(price: int | None, model: Mapping[str, Any]) -> str:
    if price is None:
        return "российской цены нет"
    lo = int(model["target_min_rub"])
    hi = int(model["target_max_rub"])
    if lo <= price <= hi:
        return "в целевом диапазоне"
    if price < lo:
        return "ниже цели"
    return "выше цели"


def is_fresh(checked_at: object, now: datetime, *, hours: float | None = None) -> bool:
    dt = _parse_dt(checked_at)
    if dt is None:
        return False
    limit = float(hours if hours is not None else config.PERSONAL_TOP_FRESH_HOURS)
    return now - dt <= timedelta(hours=limit)


def _in_stock(row: Mapping[str, Any]) -> bool:
    return not bool(row.get("out_of_stock")) and row.get("price_rub") is not None


def assess_model(
    model: Mapping[str, Any],
    russian: Mapping[str, Any],
    search: Mapping[str, Any] | None,
    *,
    fx: Mapping[str, Any] | None,
    checked_at: object,
    now: datetime,
) -> dict[str, Any]:
    fresh = bool(search) and is_fresh(checked_at, now)
    exact = list((search or {}).get("exact_matches") or []) if fresh else []
    family = list((search or {}).get("same_family_matches") or []) if fresh else []
    equivalent = list((search or {}).get("equivalent_matches") or []) if fresh else []
    exact_stock = [row for row in exact if _in_stock(row)]
    winner = exact_stock[0] if exact_stock else None
    analog = None
    if winner is None and fresh:
        analog_pool = [row for row in family + equivalent if _in_stock(row)] or family or equivalent
        analog = analog_pool[0] if analog_pool else None
    extras = confirmed_extra_rub()
    thai_rub = None if winner is None else int(winner["price_rub"]) + extras
    ru = russian.get("price")
    delta = None if ru is None or thai_rub is None else int(ru) - int(thai_rub)
    percent = None
    if delta is not None and ru:
        percent = round(delta / int(ru) * 100, 1)
    if winner is not None and delta is not None:
        if delta > 0:
            verdict = "thailand"
        elif delta < 0:
            verdict = "russia"
        else:
            verdict = "equal"
    elif fresh and exact and not exact_stock:
        verdict = "exact_out_of_stock"
    elif fresh and analog is not None:
        verdict = "analog_only"
    else:
        verdict = "no_fresh_thai"
    return {
        "key": model.get("key"),
        "model": model.get("model"),
        "brand": model.get("brand"),
        "target_label": model.get("target_label"),
        "target_position": target_position(None if ru is None else int(ru), model),
        "russian": dict(russian),
        "fresh": fresh,
        "checked_at": checked_at if fresh else None,
        "stale_checked_at": None if fresh else checked_at,
        "fx": dict(fx) if fresh and isinstance(fx, Mapping) else None,
        "exact": exact,
        "winner": winner,
        "analog": analog,
        "confirmed_extra_rub": extras,
        "unknown_costs": unknown_cost_names(),
        "thai_total_rub": thai_rub,
        "delta_rub": delta,
        "delta_pct": percent,
        "verdict": verdict,
        "stores_skipped": list((search or {}).get("stores_skipped") or []) if fresh else [],
    }


def _verdict_line(card: Mapping[str, Any]) -> str:
    verdict = card.get("verdict")
    delta = card.get("delta_rub")
    percent = card.get("delta_pct")
    if verdict == "thailand" and delta is not None:
        return f"Вывод: выгоднее Таиланд на {_rub(int(delta))} ₽ ({percent}%)."
    if verdict == "russia" and delta is not None:
        return f"Вывод: выгоднее Россия на {_rub(abs(int(delta)))} ₽ ({abs(percent)}%)."
    if verdict == "equal":
        return "Вывод: цены совпадают."
    if verdict == "exact_out_of_stock":
        return "Вывод: точная модель в Таиланде найдена, сейчас нет в наличии."
    if verdict == "analog_only":
        return "Вывод: точной модели в Таиланде нет. Аналог ниже не заменяет её в сравнении цены."
    return "Вывод: свежей тайской цены нет, сравнение не делается."


def _store_label(store: object) -> str:
    names = {
        "jib": "JIB",
        "advice": "Advice",
        "speedcom": "SpeedCom",
        "invadeit": "InvadeIT",
        "itcity": "IT City",
        "regard": "Regard",
        "kns": "KNS",
        "citilink": "Citilink",
        "andpro": "ANDPRO",
    }
    return names.get(str(store or ""), str(store or "магазин"))


def format_personal_card(card: Mapping[str, Any], *, history: Sequence[Mapping[str, Any]] | None = None) -> list[str]:
    lines = [f"<b>{card.get('model')}</b>", f"Цель: {card.get('target_label')}"]
    russian = card.get("russian") or {}
    price = russian.get("price")
    lines.append(
        f"Россия: {_rub(price)} ₽ — {_store_label(russian.get('store'))} — {card.get('target_position')}"
    )
    if russian.get("url"):
        lines.append(str(russian["url"]))
    if russian.get("history_min") is not None:
        lines.append(f"Минимум истории в России: {_rub(int(russian['history_min']))} ₽")
    offers = list(russian.get("offers") or [])
    if len(offers) > 1:
        others = ", ".join(f"{_store_label(row['store'])} {_rub(row['price'])}" for row in offers[:4])
        lines.append(f"Другие цены в России: {others}")
    lines.append("")
    if not card.get("fresh"):
        lines.append("Таиланд: свежей цены нет.")
        if card.get("stale_checked_at"):
            lines.append(f"Прошлая проверка {card['stale_checked_at']} не используется как актуальная.")
    else:
        fx = card.get("fx") or {}
        rate = fx.get("rub_per_thb")
        published = fx.get("published_date") or fx.get("fetched_at") or "н/д"
        if rate is not None:
            lines.append(f"Курс: 1 ฿ = {float(rate):.4f} ₽, дата курса {published}")
        lines.append(f"Проверено: {card.get('checked_at')}")
        winner = card.get("winner")
        if winner:
            lines.append(
                f"Таиланд EXACT: {_thb(winner.get('price_thb'))} ฿ ≈ {_rub(winner.get('price_rub'))} ₽"
            )
            lines.append(
                f"{_store_label(winner.get('store'))}, в наличии, {winner.get('url') or 'без ссылки'}"
            )
            if card.get("confirmed_extra_rub"):
                lines.append(f"Подтверждённые расходы: {_rub(int(card['confirmed_extra_rub']))} ₽")
                lines.append(f"Итого Таиланд: {_rub(card.get('thai_total_rub'))} ₽")
        elif card.get("verdict") == "exact_out_of_stock":
            lines.append("Таиланд: точная модель найдена, нет в наличии.")
        else:
            lines.append("Таиланд: точной модели нет.")
        analog = card.get("analog")
        if analog and card.get("verdict") != "thailand" and winner is None:
            lines.append("")
            lines.append("Ближайший аналог (отдельно, не точная модель):")
            lines.append(str(analog.get("name") or analog.get("manufacturer_part_number") or "модель"))
            lines.append(
                f"{_store_label(analog.get('store'))}: {_thb(analog.get('price_thb'))} ฿ "
                f"≈ {_rub(analog.get('price_rub'))} ₽"
            )
            diffs = list(analog.get("spec_differences") or [])
            if diffs:
                lines.append("Отличия: " + "; ".join(str(item) for item in diffs))
            elif analog.get("match_level") == "SAME_FAMILY":
                lines.append("Отличие: другая модификация той же серии.")
        skipped = [
            row for row in card.get("stores_skipped") or []
            if str(row.get("reason") or "") in {"RATE_LIMITED", "rate_limited"}
        ]
        if skipped:
            names = ", ".join(_store_label(row.get("store")) for row in skipped)
            lines.append(f"Не опрашивались из-за лимита: {names}.")
    if card.get("unknown_costs"):
        lines.append("Неизвестные расходы, в сумму не входят: " + ", ".join(card["unknown_costs"]) + ".")
    lines.append(_verdict_line(card))
    fresh_history = [row for row in (history or []) if row.get("checked_at")]
    if fresh_history:
        lines.append("История проверок Таиланда:")
        for row in fresh_history[-3:]:
            lines.append(
                f"{row.get('checked_at')}: {_thb(row.get('price_thb'))} ฿ "
                f"≈ {_rub(row.get('price_rub'))} ₽, {_store_label(row.get('store'))}, {row.get('level')}"
            )
    return lines


def format_personal_board(cards: Sequence[Mapping[str, Any]], *, histories: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> str:
    lines = ["<b>Мой TOP-3</b>", ""]
    histories = histories or {}
    for index, card in enumerate(cards, start=1):
        lines.append(f"{index}.")
        lines.extend(format_personal_card(card, history=histories.get(str(card.get("key")))))
        lines.append("")
    lines.append("Общий рейтинг не заменяет этот список.")
    return "\n".join(lines).strip()


def load_state(path: Path | str | None = None) -> dict[str, Any]:
    p = state_path(path)
    if not p.exists():
        return {"version": STATE_VERSION, "checked_at": None, "fx": None, "models": {}, "history": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "checked_at": None, "fx": None, "models": {}, "history": {}}
    if not isinstance(data, dict):
        return {"version": STATE_VERSION, "checked_at": None, "fx": None, "models": {}, "history": {}}
    data.setdefault("models", {})
    data.setdefault("history", {})
    return data


def persist_personal_scan(
    scan: Mapping[str, Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Store one shared Thailand scan. Older rows stay in history, not as the current quote."""
    checked_at = scan.get("finished_at") or scan.get("started_at")
    state = load_state(path)
    state["version"] = STATE_VERSION
    state["checked_at"] = checked_at
    state["fx"] = scan.get("fx") if isinstance(scan.get("fx"), dict) else None
    models = state.setdefault("models", {})
    history = state.setdefault("history", {})
    for item in scan.get("personal_searches") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if not key:
            continue
        search = item.get("search") if isinstance(item.get("search"), dict) else {}
        models[key] = {"search": search, "checked_at": checked_at}
        exact = list(search.get("exact_matches") or [])
        stocked = [row for row in exact if _in_stock(row)]
        chosen = (stocked or exact or [None])[0]
        if chosen:
            history.setdefault(key, []).append(
                {
                    "checked_at": checked_at,
                    "price_thb": chosen.get("price_thb"),
                    "price_rub": chosen.get("price_rub"),
                    "store": chosen.get("store"),
                    "level": "EXACT",
                }
            )
            history[key] = history[key][-8:]
    atomic_write_json(state_path(path), state)
    return state


def build_cards(
    *,
    products: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any] | None = None,
    history_mins: Mapping[int, int] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    stored = state if state is not None else load_state()
    fx = stored.get("fx") if isinstance(stored.get("fx"), dict) else None
    models_state = stored.get("models") if isinstance(stored.get("models"), dict) else {}
    cards: list[dict[str, Any]] = []
    for model in personal_models():
        russian = russian_side(model, products, history_mins=history_mins)
        saved = models_state.get(str(model.get("key"))) if isinstance(models_state, dict) else None
        search = None
        checked_at = None
        if isinstance(saved, dict):
            search = saved.get("search") if isinstance(saved.get("search"), dict) else None
            checked_at = saved.get("checked_at") or stored.get("checked_at")
        cards.append(
            assess_model(
                model,
                russian,
                search,
                fx=fx,
                checked_at=checked_at,
                now=now,
            )
        )
    return cards


def _last_checked(state: Mapping[str, Any]) -> datetime | None:
    return _parse_dt(state.get("checked_at"))


def enqueue_allowed(
    *,
    manual: bool,
    now: datetime | None = None,
    state: Mapping[str, Any] | None = None,
    jobs_dir: Path | str | None = None,
) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    if find_active_dedupe(DEDUPE_KEY, root=jobs_dir):
        return False, "already_queued"
    stored = state if state is not None else load_state()
    last = _last_checked(stored)
    if last is None:
        return True, "ok"
    minimum = float(
        config.PERSONAL_TOP_MANUAL_MIN_HOURS if manual else config.PERSONAL_TOP_DAILY_MIN_HOURS
    )
    if now - last < timedelta(hours=minimum):
        return False, "too_soon"
    return True, "ok"


def enqueue_personal_job(
    *,
    manual: bool,
    chat_id: str | int | None = None,
    products: Sequence[Mapping[str, Any]] | None = None,
    jobs_dir: Path | str | None = None,
    state: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    allowed, reason = enqueue_allowed(manual=manual, now=now, state=state, jobs_dir=jobs_dir)
    if not allowed:
        return {"ok": False, "error": reason, "thailand_scan_status": reason}
    rows = list(products or [])
    targets = []
    for model in personal_models():
        russian = russian_side(model, rows)
        targets.append(target_for(model, russian))
    trigger = TRIGGER_PERSONAL_MANUAL if manual else TRIGGER_PERSONAL_DAILY
    job = build_job(
        trigger_type=trigger,
        signals=[],
        russian_deals=[],
        dedupe_key=DEDUPE_KEY,
        app_version=None,
        instance_id=config.get_instance_id(),
        requested_chat_id=chat_id,
        personal_targets=targets,
    )
    enq = enqueue_job(job, root=jobs_dir)
    return {
        "ok": bool(enq.get("ok")),
        "duplicate": bool(enq.get("duplicate")),
        "job_id": enq.get("job_id"),
        "error": enq.get("error"),
        "thailand_scan_status": "queued" if enq.get("ok") else enq.get("error"),
        "trigger_type": trigger,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Enqueue the personal TOP-3 Thailand check")
    parser.add_argument("command", choices=("enqueue",))
    parser.add_argument("--manual", action="store_true")
    args = parser.parse_args(argv)
    from storage import get_all_products_readonly

    result = enqueue_personal_job(
        manual=args.manual,
        products=get_all_products_readonly(),
    )
    print(result.get("thailand_scan_status") or result.get("error"))
    return 0 if result.get("ok") or result.get("error") == "too_soon" else 1


if __name__ == "__main__":
    raise SystemExit(main())
