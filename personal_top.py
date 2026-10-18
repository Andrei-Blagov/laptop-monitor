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
from russian_deals import (
    _code_token,
    _exact_codes_in_text,
    _norm_cpu,
    _norm_gpu,
    _norm_inch,
    _norm_int,
    _norm_resolution,
)
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


def specs_for_products(products: Sequence[Mapping[str, Any]], specs_path: Path | str | None = None) -> dict[tuple[str, str], Any]:
    from identity_sync import identity_from_cache, load_specs_cache

    cache = load_specs_cache(specs_path) if specs_path is not None else load_specs_cache()
    found: dict[tuple[str, str], Any] = {}
    for product in products:
        store = str(product.get("store") or "")
        external_id = str(product.get("external_id") or "")
        identity = identity_from_cache(cache, store, external_id)
        if identity is not None:
            found[(store, external_id)] = identity
    return found


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


def exact_skus(model: Mapping[str, Any]) -> set[str]:
    """Configuration SKUs only. A series code such as 16AFR10H is not included."""
    found: set[str] = set()
    for raw in model.get("exact_skus") or ():
        token = _code_token(str(raw)) or normalize_identifier(str(raw))
        if token:
            found.add(token)
    return found


def series_codes(model: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for raw in model.get("series_codes") or ():
        token = normalize_identifier(str(raw))
        if token:
            found.add(token)
    return found


def contains_code(text: str, code: str) -> bool:
    """Whole token. 16AFR10H matches the series, not a longer glued identifier."""
    import re

    token = normalize_identifier(code)
    if not token:
        return False
    return re.search(
        rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])",
        str(text or "").upper(),
    ) is not None


def series_hit(model: Mapping[str, Any], text: str) -> bool:
    codes = series_codes(model)
    return bool(codes) and any(contains_code(text, code) for code in codes)


def match_products(model: Mapping[str, Any], products: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    wanted = exact_skus(model) - series_codes(model)
    if not wanted:
        return []
    hits: list[dict[str, Any]] = []
    for product in products:
        if wanted & product_codes(product):
            hits.append(dict(product))
    return hits


def _identity_value(identity: Any, *names: str) -> Any:
    if identity is None:
        return None
    for name in names:
        if isinstance(identity, Mapping):
            value = identity.get(name)
        else:
            value = getattr(identity, name, None)
        if value not in (None, ""):
            return value
    return None


def confirmed_specs(product: Mapping[str, Any], identity: Any = None) -> dict[str, Any] | None:
    """CPU, GPU, RAM, SSD, and screen must all be stated. Missing fields are not guessed."""
    inferred = extract_specs_from_name(
        " ".join(str(product.get(key) or "") for key in ("name", "sku"))
    )
    raw = {
        "cpu": _identity_value(identity, "cpu") or inferred.get("cpu"),
        "gpu": _identity_value(identity, "gpu", "normalized_gpu") or inferred.get("gpu"),
        "ram_gb": _identity_value(identity, "ram_gb") or inferred.get("ram_gb"),
        "ssd_gb": _identity_value(identity, "ssd_gb") or inferred.get("ssd_gb"),
        "screen_inch": _identity_value(identity, "screen_inch", "screen_size_inch") or inferred.get("screen_inch"),
        "screen_resolution": _identity_value(identity, "screen_resolution") or inferred.get("screen_resolution"),
    }
    required = {
        "cpu": _norm_cpu(raw["cpu"]),
        "gpu": _norm_gpu(raw["gpu"]),
        "ram_gb": _norm_int(raw["ram_gb"]),
        "ssd_gb": _norm_int(raw["ssd_gb"]),
        "screen_inch": _norm_inch(raw["screen_inch"]),
        "screen_resolution": _norm_resolution(raw["screen_resolution"]),
    }
    if any(value is None for value in required.values()):
        return None
    required["gpu_vram_gb"] = _identity_value(identity, "gpu_vram_gb")
    return required


def _candidate_differences(sku: str, specs: Mapping[str, Any] | None) -> list[str]:
    diffs: list[str] = []
    if "WPRO" in str(sku or "").upper():
        diffs.append("вариант -wpro")
    if not specs:
        return diffs + ["комплектация не подтверждена полностью"]
    if specs.get("cpu") is None:
        diffs.append("CPU не подтверждён")
    if specs.get("ssd_gb") is None:
        diffs.append("SSD не подтверждён")
    if specs.get("screen_resolution") is None:
        diffs.append("разрешение не подтверждено")
    return diffs


def _profile_specs(product: Mapping[str, Any], identity: Any = None) -> dict[str, Any]:
    """Partial specs for a series candidate. Missing fields stay missing."""
    inferred = extract_specs_from_name(
        " ".join(str(product.get(key) or "") for key in ("name", "sku"))
    )
    raw = {
        "cpu": _identity_value(identity, "cpu") or inferred.get("cpu"),
        "gpu": _identity_value(identity, "gpu", "normalized_gpu") or inferred.get("gpu"),
        "ram_gb": _identity_value(identity, "ram_gb") or inferred.get("ram_gb"),
        "ssd_gb": _identity_value(identity, "ssd_gb") or inferred.get("ssd_gb"),
        "screen_inch": _identity_value(identity, "screen_inch", "screen_size_inch") or inferred.get("screen_inch"),
        "screen_resolution": _identity_value(identity, "screen_resolution") or inferred.get("screen_resolution"),
    }
    return {
        "cpu": _norm_cpu(raw["cpu"]),
        "gpu": _norm_gpu(raw["gpu"]),
        "ram_gb": _norm_int(raw["ram_gb"]),
        "ssd_gb": _norm_int(raw["ssd_gb"]),
        "screen_inch": _norm_inch(raw["screen_inch"]),
        "screen_resolution": _norm_resolution(raw["screen_resolution"]),
        "gpu_vram_gb": _identity_value(identity, "gpu_vram_gb"),
    }


def _suits_series_profile(model: Mapping[str, Any], specs: Mapping[str, Any]) -> bool:
    from laptop_eligibility import evaluate_hardware

    required_gpu = _norm_gpu(model.get("required_gpu"))
    if required_gpu is None or specs.get("gpu") != required_gpu:
        return False
    required_ram = _norm_int(model.get("required_ram_gb"))
    if required_ram is None or specs.get("ram_gb") != required_ram:
        return False
    return evaluate_hardware(
        gpu=specs.get("gpu"),
        gpu_vram_gb=specs.get("gpu_vram_gb"),
        ram_gb=specs.get("ram_gb"),
        ssd_gb=specs.get("ssd_gb"),
        screen_resolution=specs.get("screen_resolution"),
        screen_inch=specs.get("screen_inch"),
    ).eligible


def series_product_candidates(
    model: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    *,
    fresh_stores: set[str] | None = None,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Suitable series variants. Never an EXACT configuration."""
    if not series_codes(model) or not model.get("required_gpu"):
        return []
    found: list[dict[str, Any]] = []
    for row in products:
        blob = f"{row.get('name') or ''} {row.get('sku') or ''}"
        if not series_hit(model, blob):
            continue
        if fresh_stores is not None and str(row.get("store") or "") not in fresh_stores:
            continue
        if not row.get("available") or not config.is_price_in_tracking_scope(row.get("price")):
            continue
        identity = None
        if specs_by_key is not None:
            identity = specs_by_key.get((str(row.get("store") or ""), str(row.get("external_id") or "")))
        specs = _profile_specs(row, identity)
        if not _suits_series_profile(model, specs):
            continue
        sku = str(row.get("sku") or "")
        found.append(
            {
                "match_level": "CANDIDATE",
                "sku": sku,
                "store": str(row.get("store") or ""),
                "price": int(row["price"]),
                "url": row.get("url"),
                "name": str(row.get("name") or ""),
                "differences": _candidate_differences(sku, specs),
            }
        )
    return sorted(found, key=lambda item: (item["price"], item["store"], item["sku"]))


def attach_series_candidates(
    target: Mapping[str, Any],
    search: Mapping[str, Any],
    offers: Sequence[Any],
    fx: Any = None,
) -> dict[str, Any]:
    """Add series variants to a Thailand search. They are never EXACT matches."""
    from thailand.fx import thb_to_rub
    from thailand.eligibility import offer_user_facing_eligible

    out = dict(search)
    codes = [str(code) for code in target.get("series_codes") or [] if code]
    if not codes or not target.get("required_gpu"):
        out.setdefault("series_candidates", [])
        return out
    model = {
        "series_codes": tuple(codes),
        "required_gpu": target.get("required_gpu"),
        "required_ram_gb": target.get("required_ram_gb"),
    }
    rows: list[dict[str, Any]] = []
    for offer in offers:
        if not offer_user_facing_eligible(offer, fx=fx)[0]:
            continue
        blob = " ".join(
            str(getattr(offer, field, "") or "")
            for field in ("name", "sku", "manufacturer_part_number")
        )
        if not series_hit(model, blob):
            continue
        specs = _profile_specs(
            {"name": getattr(offer, "name", ""), "sku": getattr(offer, "sku", "")},
            {
                "cpu": getattr(offer, "cpu", None),
                "gpu": getattr(offer, "gpu", None),
                "ram_gb": getattr(offer, "ram_gb", None),
                "ssd_gb": getattr(offer, "ssd_gb", None),
                "screen_inch": getattr(offer, "screen_size_inch", None),
                "screen_resolution": getattr(offer, "screen_resolution", None),
                "screen_size_inch": getattr(offer, "screen_size_inch", None),
            },
        )
        if not _suits_series_profile(model, specs):
            continue
        sku = str(getattr(offer, "sku", None) or getattr(offer, "manufacturer_part_number", None) or "")
        price_thb = getattr(offer, "price_thb", None)
        rows.append(
            {
                "match_level": "CANDIDATE",
                "sku": sku,
                "store": getattr(offer, "store", None),
                "name": getattr(offer, "name", None),
                "price_thb": price_thb,
                "price_rub": thb_to_rub(price_thb, fx),
                "url": getattr(offer, "url", None),
                "out_of_stock": not bool(getattr(offer, "available", False)),
                "differences": _candidate_differences(sku, specs),
                "gpu": getattr(offer, "gpu", None),
                "ram_gb": getattr(offer, "ram_gb", None),
                "ssd_gb": getattr(offer, "ssd_gb", None),
                "screen_resolution": getattr(offer, "screen_resolution", None),
                "screen_size_inch": getattr(offer, "screen_size_inch", None),
                "availability_confirmed": bool(getattr(offer, "availability_confirmed", False)),
                "verification_status": getattr(offer, "verification_status", None),
                "marketplace": bool(getattr(offer, "marketplace", False)),
                "price_verified_for_variant": bool(getattr(offer, "price_verified_for_variant", False)),
                "purchase_eligible": True,
            }
        )
    out["series_candidates"] = sorted(
        rows,
        key=lambda item: (
            item["price_rub"] if item.get("price_rub") is not None else 10**12,
            str(item.get("store") or ""),
            str(item.get("sku") or ""),
        ),
    )
    return out


def russian_side(
    model: Mapping[str, Any],
    products: Sequence[Mapping[str, Any]],
    *,
    history_mins: Mapping[int, int] | None = None,
    fresh_stores: set[str] | None = None,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
) -> dict[str, Any]:
    hits = []
    for row in match_products(model, products):
        if fresh_stores is not None and str(row.get("store") or "") not in fresh_stores:
            continue
        if not row.get("available"):
            continue
        if row.get("price") is None or not config.is_price_in_tracking_scope(row.get("price")):
            continue
        identity = None
        if specs_by_key is not None:
            identity = specs_by_key.get((str(row.get("store") or ""), str(row.get("external_id") or "")))
        specs = confirmed_specs(row, identity)
        from laptop_eligibility import evaluate_hardware

        if specs is None or not evaluate_hardware(
            gpu=specs.get("gpu"),
            gpu_vram_gb=specs.get("gpu_vram_gb"),
            ram_gb=specs.get("ram_gb"),
            ssd_gb=specs.get("ssd_gb"),
            screen_resolution=specs.get("screen_resolution"),
            screen_inch=specs.get("screen_inch"),
        ).eligible:
            continue
        item = dict(row)
        item["confirmed_specs"] = specs
        hits.append(item)
    signatures = {tuple(row["confirmed_specs"].values()) for row in hits}
    priced = hits if len(signatures) <= 1 else []
    pool = priced
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
        "specs_conflict": len(signatures) > 1,
        "store": None if best is None else str(best.get("store") or ""),
        "price": None if best is None else int(best["price"]),
        "url": None if best is None else best.get("url"),
        "name": None if best is None else str(best.get("name") or model.get("model") or ""),
        "available": bool(best and best.get("available")),
        "history_min": hist,
        "offers": offers,
        "confirmed_specs": None if best is None else best.get("confirmed_specs"),
        "candidates": series_product_candidates(
            model,
            products,
            fresh_stores=fresh_stores,
            specs_by_key=specs_by_key,
        ),
    }


def target_for(model: Mapping[str, Any], russian: Mapping[str, Any]) -> dict[str, Any]:
    codes = sorted(exact_skus(model))
    primary = codes[0] if codes else None
    specs = dict(russian.get("confirmed_specs") or {})
    if not specs:
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
        "series_codes": sorted(series_codes(model)),
        "required_gpu": model.get("required_gpu"),
        "required_ram_gb": model.get("required_ram_gb"),
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


def fx_confirmed(fx: Mapping[str, Any] | None) -> bool:
    if not isinstance(fx, Mapping) or fx.get("stale") or fx.get("error"):
        return False
    try:
        return float(fx.get("rub_per_thb") or 0) > 0
    except (TypeError, ValueError):
        return False


def _same_spec(left: Any, right: Any, kind: str) -> bool | None:
    if kind == "cpu":
        a, b = _norm_cpu(left), _norm_cpu(right)
    elif kind == "gpu":
        a, b = _norm_gpu(left), _norm_gpu(right)
    elif kind == "inch":
        a, b = _norm_inch(left), _norm_inch(right)
    elif kind == "resolution":
        a, b = _norm_resolution(left), _norm_resolution(right)
    else:
        a, b = _norm_int(left), _norm_int(right)
    if a is None or b is None:
        return None
    return a == b


def confirmation_gaps(row: Mapping[str, Any], russian: Mapping[str, Any], fx: Mapping[str, Any] | None) -> list[str]:
    """Reasons a Thai row cannot support a cheaper/dearer verdict."""
    gaps: list[str] = []
    from thailand.eligibility import search_row_user_facing_eligible

    if not search_row_user_facing_eligible(dict(row)):
        gaps.append("соответствие требованиям покупки")
    if row.get("price_thb") is None or row.get("price_rub") is None:
        gaps.append("цена")
    if row.get("out_of_stock") or not row.get("availability_confirmed"):
        gaps.append("наличие")
    if str(row.get("verification_status") or "") != "VERIFIED":
        gaps.append("достоверность предложения")
    if row.get("marketplace") and str(row.get("seller_trust_tier") or "") not in {"A", "B"}:
        gaps.append("продавец")
    if not fx_confirmed(fx):
        gaps.append("курс")
    specs = russian.get("confirmed_specs") or {}
    checks = (
        ("CPU", "cpu", specs.get("cpu"), row.get("cpu")),
        ("GPU", "gpu", specs.get("gpu"), row.get("gpu")),
        ("RAM", "int", specs.get("ram_gb"), row.get("ram_gb")),
        ("SSD", "int", specs.get("ssd_gb"), row.get("ssd_gb")),
        ("диагональ", "inch", specs.get("screen_inch"), row.get("screen_size_inch")),
        ("разрешение", "resolution", specs.get("screen_resolution"), row.get("screen_resolution")),
    )
    for label, kind, left, right in checks:
        same = _same_spec(left, right, kind)
        if same is None:
            gaps.append(f"{label} не подтверждён")
        elif not same:
            gaps.append(f"{label} отличается")
    return gaps


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
    from thailand.eligibility import search_row_user_facing_eligible

    thai_candidates = [
        row for row in (search or {}).get("series_candidates") or []
        if search_row_user_facing_eligible(row)
    ] if fresh else []
    exact_stock = [row for row in exact if _in_stock(row)]
    confirmed = []
    gap_notes: list[str] = []
    for row in exact_stock:
        gaps = confirmation_gaps(row, russian, fx if fresh else None)
        if gaps:
            gap_notes.extend(gaps)
            continue
        confirmed.append(row)
    winner = min(confirmed, key=lambda row: (int(row["price_rub"]), str(row.get("store") or ""))) if confirmed else None
    analog = None
    if winner is None and fresh:
        analog_pool = [row for row in family + equivalent if search_row_user_facing_eligible(row)]
        analog = analog_pool[0] if analog_pool else None
    extras = confirmed_extra_rub()
    thai_rub = None if winner is None else int(winner["price_rub"]) + extras
    ru = russian.get("price")
    delta = None if ru is None or thai_rub is None else int(ru) - int(thai_rub)
    percent = None
    if delta is not None and ru:
        percent = round(delta / int(ru) * 100, 1)
    coverage = (search or {}).get("coverage") if fresh and isinstance((search or {}).get("coverage"), Mapping) else None
    coverage_stores = list((coverage or {}).get("stores") or [])
    if not coverage_stores:
        thai_check = None
    elif any(row.get("checked") for row in coverage_stores):
        thai_check = "checked"
    else:
        thai_check = "not_checked"
    if winner is not None and delta is not None and fx_confirmed(fx):
        if delta > 0:
            verdict = "thailand"
        elif delta < 0:
            verdict = "russia"
        else:
            verdict = "equal"
    elif fresh and exact_stock and winner is None:
        verdict = "unconfirmed"
    elif fresh and exact and not exact_stock:
        verdict = "exact_out_of_stock"
    elif fresh and analog is not None:
        verdict = "analog_only"
    elif russian.get("candidates") or thai_candidates:
        verdict = "candidates_only"
    elif fresh and thai_check == "not_checked":
        verdict = "not_checked"
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
        "thai_candidates": thai_candidates,
        "confirmation_gaps": sorted(set(gap_notes)),
        "confirmed_extra_rub": extras,
        "unknown_costs": unknown_cost_names(),
        "thai_total_rub": thai_rub,
        "delta_rub": delta,
        "delta_pct": percent,
        "verdict": verdict,
        "coverage": dict(coverage) if coverage else None,
        "thai_check": thai_check,
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
    if verdict == "candidates_only":
        return "Вывод о выгоде не делается: подтверждённой комплектации для сравнения нет."
    if verdict == "unconfirmed":
        gaps = ", ".join(card.get("confirmation_gaps") or []) or "не хватает данных"
        return f"Сравнение не подтверждено: {gaps}."
    if verdict == "not_checked":
        return "Вывод: магазины не проверены, сравнение не делается."
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


def _check_reason(reason: object) -> str:
    labels = {
        "RATE_LIMITED": "лимит запросов",
        "rate_limited": "лимит запросов",
        "TIMEOUT": "таймаут",
        "timeout": "таймаут",
        "skipped": "пропуск",
        "disabled": "отключён",
        "failed": "ошибка",
    }
    return labels.get(str(reason or ""), "ошибка")


def coverage_lines(coverage: Mapping[str, Any] | None) -> list[str]:
    """Say which codes a store actually queried. A miss is not the same as a skipped store."""
    if not isinstance(coverage, Mapping):
        return []
    lines: list[str] = []
    stores = [row for row in coverage.get("stores") or [] if isinstance(row, Mapping)]
    for code in coverage.get("codes") or []:
        addressed: list[str] = []
        catalog: list[str] = []
        missed: list[str] = []
        for row in stores:
            name = _store_label(row.get("store"))
            mode = str(row.get("mode") or "")
            found = str(code) in [str(item) for item in (row.get("codes") or [])]
            if mode in {"targeted", "partial"} and found:
                addressed.append(name)
            elif mode == "catalog":
                catalog.append(name)
            elif mode == "partial":
                missed.append(f"{name} ({_check_reason(row.get('reason'))})")
            else:
                missed.append(f"{name} ({_check_reason(row.get('reason'))})")
        parts: list[str] = []
        if addressed:
            parts.append("адресный поиск " + ", ".join(addressed))
        if catalog:
            parts.append("каталог " + ", ".join(catalog))
        if missed:
            parts.append("не проверено " + ", ".join(missed))
        if parts:
            lines.append(f"{code}: " + "; ".join(parts))
    return lines


def format_personal_card(card: Mapping[str, Any], *, history: Sequence[Mapping[str, Any]] | None = None) -> list[str]:
    lines = [f"<b>{card.get('model')}</b>", f"Цель: {card.get('target_label')}"]
    russian = card.get("russian") or {}
    price = russian.get("price")
    if russian.get("specs_conflict"):
        lines.append("Россия: несколько разных комплектаций с одним кодом, цена не выбрана.")
    elif price is None:
        lines.append("Россия: свежего доступного предложения с подтверждённой комплектацией нет.")
    else:
        lines.append(
            f"Россия: {_rub(price)} ₽ — {_store_label(russian.get('store'))} — {card.get('target_position')}"
        )
    if russian.get("url"):
        lines.append(str(russian["url"]))
    if russian.get("history_min") is not None:
        lines.append(f"Минимум истории в России: {_rub(int(russian['history_min']))} ₽")
    for candidate in russian.get("candidates") or []:
        lines.append(
            f"Кандидат {candidate.get('sku') or 'без SKU'}: {_rub(candidate.get('price'))} ₽ — "
            f"{_store_label(candidate.get('store'))} — не EXACT"
        )
        diffs = list(candidate.get("differences") or [])
        if diffs:
            lines.append("Отличия: " + "; ".join(str(item) for item in diffs))
        if candidate.get("url"):
            lines.append(str(candidate["url"]))
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
        elif card.get("verdict") == "unconfirmed":
            lines.append("Таиланд: точное предложение есть, сравнение не подтверждено.")
        elif card.get("thai_check") == "not_checked":
            lines.append("Таиланд: не проверено.")
        else:
            lines.append("Таиланд: точной модели нет.")
        lines.extend(coverage_lines(card.get("coverage")))
        for candidate in card.get("thai_candidates") or []:
            lines.append(
                f"Кандидат Таиланд {candidate.get('sku') or candidate.get('name') or 'модель'}: "
                f"{_thb(candidate.get('price_thb'))} ฿ ≈ {_rub(candidate.get('price_rub'))} ₽ — "
                f"{_store_label(candidate.get('store'))} — не EXACT"
            )
            diffs = list(candidate.get("differences") or [])
            if diffs:
                lines.append("Отличия: " + "; ".join(str(item) for item in diffs))
            if candidate.get("url"):
                lines.append(str(candidate["url"]))
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
    fresh_stores: set[str] | None = None,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    stored = state if state is not None else load_state()
    fx = stored.get("fx") if isinstance(stored.get("fx"), dict) else None
    models_state = stored.get("models") if isinstance(stored.get("models"), dict) else {}
    cards: list[dict[str, Any]] = []
    for model in personal_models():
        russian = russian_side(
            model,
            products,
            history_mins=history_mins,
            fresh_stores=fresh_stores,
            specs_by_key=specs_by_key,
        )
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
    fresh_stores: set[str] | None = None,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
    jobs_dir: Path | str | None = None,
    state: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if config.stage22_market_telegram_enabled():
        from thailand.market_schedule import enqueue_personal_market

        return enqueue_personal_market(chat_id=chat_id, now=now, jobs_dir=jobs_dir)
    allowed, reason = enqueue_allowed(manual=manual, now=now, state=state, jobs_dir=jobs_dir)
    if not allowed:
        return {"ok": False, "error": reason, "thailand_scan_status": reason}
    rows = list(products or [])
    targets = []
    for model in personal_models():
        russian = russian_side(model, rows, fresh_stores=fresh_stores, specs_by_key=specs_by_key)
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
    if config.stage22_market_telegram_enabled():
        from thailand.market_schedule import enqueue_personal_market

        result = enqueue_personal_market()
        print(result.get("thailand_scan_status") or result.get("error"))
        return 0
    from storage import DEFAULT_DB_PATH, get_all_products_readonly, get_latest_store_runs_readonly
    from store_freshness import get_fresh_store_slugs

    products = get_all_products_readonly()
    result = enqueue_personal_job(
        manual=args.manual,
        products=products,
        fresh_stores=get_fresh_store_slugs(get_latest_store_runs_readonly(DEFAULT_DB_PATH)),
        specs_by_key=specs_for_products(products),
    )
    print(result.get("thailand_scan_status") or result.get("error"))
    return 0 if result.get("ok") or result.get("error") == "too_soon" else 1


if __name__ == "__main__":
    raise SystemExit(main())
