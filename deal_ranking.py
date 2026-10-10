from __future__ import annotations

"""Explainable deal ranking v2 for TOP / Telegram / n8n / history picker."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import config


@dataclass
class RankedDeal:
    score: float
    reasons: list[str] = field(default_factory=list)
    offer: dict[str, Any] = field(default_factory=dict)
    cluster_name: str = ""
    gpu: str | None = None
    store: str = ""
    price: int | None = None
    url: str | None = None
    saving_vs_next: int | None = None
    next_store: str | None = None
    next_price: int | None = None
    ram_gb: int | None = None
    ssd_gb: int | None = None
    screen_inch: float | None = None
    # v2 additions (backward-compatible extras)
    cpu: str | None = None
    confidence: int = 0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    historical_min: int | None = None
    screen_resolution: str | None = None
    # Cluster identity (normalized SKU) and earliest stored observation of any
    # offer in the cluster (ISO, UTC). Used by BUY maturity / repeat rules.
    cluster_key: str | None = None
    history_started_at: str | None = None


@dataclass
class ScoreResult:
    """score_offer return value; unpacks as (score, reasons) for compatibility."""

    score: float
    reasons: list[str]
    confidence: int = 0
    breakdown: dict[str, float] = field(default_factory=dict)

    def __iter__(self):
        yield self.score
        yield self.reasons


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def canonical_gpu(gpu: str | None) -> str | None:
    if not gpu:
        return None
    g = gpu.upper().replace("GEFORCE", " ").replace("LAPTOP", " ").replace("MOBILE", " ")
    g = " ".join(g.split())
    if "5080" in g and "TI" not in g:
        return "RTX 5080"
    if "5070" in g and "TI" in g:
        return "RTX 5070 Ti"
    return None


def target_price_for_gpu(gpu: str | None) -> int | None:
    key = canonical_gpu(gpu)
    if key is None:
        return None
    raw = config.TARGET_PRICES.get(key)
    return int(raw) if raw is not None else None


def score_gpu(gpu: str | None) -> tuple[float, str | None]:
    key = canonical_gpu(gpu)
    if key == "RTX 5080":
        return float(config.SCORE_GPU_5080), "RTX 5080"
    if key == "RTX 5070 Ti":
        return float(config.SCORE_GPU_5070TI), "RTX 5070 Ti"
    return 0.0, None


def score_price_value(price: int | None, gpu: str | None) -> tuple[float, str | None]:
    """Smooth 0..SCORE_PRICE_MAX vs GPU-specific target."""
    if price is None or price <= 0:
        return 0.0, None
    target = target_price_for_gpu(gpu)
    if target is None or target <= 0:
        return 0.0, None
    max_pts = float(config.SCORE_PRICE_MAX)
    at_target = float(config.SCORE_PRICE_AT_TARGET)
    if price <= target:
        discount = (target - price) / target
        span = max(float(config.SCORE_PRICE_UNDER_SPAN), 1e-6)
        pts = at_target + (max_pts - at_target) * _clamp(discount / span, 0.0, 1.0)
        if discount >= 0.03:
            reason = "цена ниже target"
        else:
            reason = "цена близко к целевой"
        return round(_clamp(pts, 0.0, max_pts), 2), reason
    over = (price - target) / target
    zero_at = max(float(config.SCORE_PRICE_OVER_ZERO_AT), 1e-6)
    pts = at_target * max(0.0, 1.0 - over / zero_at)
    if over <= 0.08:
        reason = "цена немного выше target"
    elif over <= 0.20:
        reason = "цена выше target"
    else:
        reason = None
    return round(_clamp(pts, 0.0, max_pts), 2), reason


def classify_cpu(cpu: str | None) -> tuple[float, str | None]:
    """Deterministic explainable CPU class — not a benchmark claim."""
    if not cpu:
        return 0.0, None
    u = " ".join(str(cpu).upper().replace("Ё", "Е").split())
    if "HX" in u:
        return float(config.SCORE_CPU_HIGH_END_HX), "мощный HX-процессор"
    if "RYZEN AI" in u:
        return float(config.SCORE_CPU_STRONG_H), "производительный Ryzen AI"
    if "ULTRA 9" in u:
        return float(config.SCORE_CPU_STRONG_H), "мощный Core Ultra"
    if "ULTRA 7" in u or "ULTRA 5" in u:
        return float(config.SCORE_CPU_STRONG_H), "производительный Core Ultra"
    if "RYZEN 9" in u:
        return float(config.SCORE_CPU_STRONG_H), "производительный Ryzen 9"
    # Intel Core 7/i7 H-class (e.g. 240H)
    if (
        "CORE 7" in u
        or "CORE I7" in u
        or " I7 " in f" {u} "
        or u.endswith("I7")
    ) and (
        "H" in u.replace("HX", "")
        or any(tok.endswith("H") for tok in u.split() if tok[:1].isdigit())
    ):
        return float(config.SCORE_CPU_STRONG_H), "производительный H-процессор"
    if "RYZEN 7" in u and ("H" in u or "HS" in u):
        return float(config.SCORE_CPU_STRONG_H), "производительный Ryzen"
    if "CORE I9" in u or " I9 " in f" {u} ":
        return float(config.SCORE_CPU_STRONG_H), "производительный Intel"
    if "RYZEN 7" in u or "RYZEN 5" in u or "ULTRA" in u or "CORE I5" in u:
        return float(config.SCORE_CPU_MID), "средний CPU-класс"
    if "CORE" in u or "RYZEN" in u or "INTEL" in u or "AMD" in u:
        return float(config.SCORE_CPU_LOWER), None
    return 0.0, None


def score_ram(ram_gb: int | None) -> tuple[float, str | None]:
    if ram_gb is None:
        return 0.0, None
    if ram_gb >= 64:
        return float(config.SCORE_RAM_64), f"{ram_gb}GB RAM"
    if ram_gb >= 32:
        return float(config.SCORE_RAM_32), f"{ram_gb}GB RAM"
    if ram_gb >= 16:
        return float(config.SCORE_RAM_16), f"{ram_gb}GB RAM"
    return 0.0, None


def score_ssd(ssd_gb: int | None) -> tuple[float, str | None]:
    if ssd_gb is None:
        return 0.0, None
    if ssd_gb >= 2000:
        return float(config.SCORE_SSD_2TB), "2TB SSD"
    if ssd_gb >= 1000:
        return float(config.SCORE_SSD_1TB), "1TB SSD"
    if ssd_gb >= 512:
        return float(config.SCORE_SSD_512), "512GB SSD"
    return 0.0, None


def _is_qhd_class(resolution: str | None) -> bool:
    if not resolution:
        return False
    text = resolution.upper().replace("×", "X").replace("*", "X")
    if "QHD" in text or "WQXGA" in text or "1600" in text:
        return True
    if "2560" in text and ("1600" in text or "1440" in text):
        return True
    return False


def score_screen(
    screen_inch: float | None,
    screen_resolution: str | None = None,
) -> tuple[float, str | None]:
    if screen_inch is None:
        base = 0.0
        reason = None
    elif screen_inch >= 18:
        base = float(config.SCORE_SCREEN_18)
        reason = f'{screen_inch:g}"'
    elif screen_inch >= 17:
        base = float(config.SCORE_SCREEN_17)
        reason = f'{screen_inch:g}"'
    elif screen_inch >= 16:
        base = float(config.SCORE_SCREEN_16)
        reason = f'{screen_inch:g}"'
    elif screen_inch >= 15.5:
        base = float(config.SCORE_SCREEN_156)
        reason = f'{screen_inch:g}"'
    else:
        base = 0.0
        reason = None
    pts = base
    if _is_qhd_class(screen_resolution):
        pts = min(float(config.SCORE_SCREEN_MAX), pts + float(config.SCORE_SCREEN_QHD_BONUS))
        if reason:
            reason = f"{reason} QHD"
        else:
            reason = "QHD"
    return round(pts, 2), reason


def score_historical_opportunity(
    current_price: int | None,
    historical_min: int | None,
    *,
    is_historical_low: bool = False,
) -> tuple[float, str | None]:
    if is_historical_low and (historical_min is None or current_price is None):
        return float(config.SCORE_HISTORY_MAX), "цена у исторического минимума"
    if current_price is None or historical_min is None or historical_min <= 0:
        return 0.0, None
    ratio = current_price / float(historical_min)
    if ratio <= 1.01:
        return float(config.SCORE_HISTORY_WITHIN_1PCT), "цена близка к минимуму"
    if ratio <= 1.03:
        return float(config.SCORE_HISTORY_WITHIN_3PCT), "в 3% от исторического минимума"
    if ratio <= 1.05:
        return float(config.SCORE_HISTORY_WITHIN_5PCT), "в 5% от исторического минимума"
    if ratio <= 1.10:
        return float(config.SCORE_HISTORY_WITHIN_10PCT), "в 10% от исторического минимума"
    return 0.0, None


def score_cross_store_saving(saving_vs_next: int | None) -> tuple[float, str | None]:
    if saving_vs_next is None:
        return 0.0, None
    saving = int(saving_vs_next)
    if saving < int(config.SCORE_SAVING_TIER_1):
        return 0.0, None
    if saving < int(config.SCORE_SAVING_TIER_2):
        pts = 1.0
    elif saving < int(config.SCORE_SAVING_TIER_3):
        pts = 2.0
    elif saving < int(config.SCORE_SAVING_TIER_4):
        pts = 3.0
    else:
        pts = float(config.SCORE_SAVING_MAX)
    return pts, None


def compute_confidence(
    *,
    gpu: str | None,
    cpu: str | None,
    ram_gb: int | None,
    ssd_gb: int | None,
    screen_inch: float | None,
    historical_min: int | None,
) -> int:
    flags = [
        canonical_gpu(gpu) is not None or bool(gpu),
        bool(cpu),
        ram_gb is not None,
        ssd_gb is not None,
        screen_inch is not None,
        historical_min is not None,
    ]
    known = sum(1 for f in flags if f)
    return int(round(100.0 * known / len(flags)))


def historical_mins_from_rows(
    histories: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[int, int]:
    """Pure helper: product_id → all-time valid historical minimum price."""
    out: dict[int, int] = {}
    for pid, rows in histories.items():
        best: int | None = None
        for row in rows:
            try:
                price = int(row.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            if best is None or price < best:
                best = price
        if best is not None:
            out[int(pid)] = best
    return out


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def history_starts_from_rows(
    histories: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[int, str]:
    """Pure helper: product_id → earliest stored observation (ISO UTC).

    The first price_history row is written when a product is first seen, so
    this is the observation start even if the price never changed.
    """
    out: dict[int, str] = {}
    for pid, rows in histories.items():
        starts = [dt for dt in (_parse_iso(r.get("checked_at")) for r in rows) if dt]
        if starts:
            out[int(pid)] = min(starts).astimezone(timezone.utc).isoformat()
    return out


def cluster_history_started_at(
    match: Any,
    history_starts: Mapping[int, str] | None,
) -> str | None:
    if not history_starts:
        return None
    starts = [
        dt
        for dt in (
            _parse_iso(history_starts.get(int(o.product_id)))
            for o in getattr(match, "offers", []) or []
            if getattr(o, "product_id", None) is not None
        )
        if dt
    ]
    return min(starts).isoformat() if starts else None


def collect_match_product_ids(matches: Sequence[Any]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for match in matches:
        for offer in getattr(match, "offers", []) or []:
            pid = getattr(offer, "product_id", None)
            if pid is None:
                continue
            ipid = int(pid)
            if ipid not in seen:
                seen.add(ipid)
                ids.append(ipid)
    return ids


def cluster_historical_min(
    match: Any,
    historical_mins: Mapping[int, int] | None,
) -> int | None:
    if not historical_mins:
        return None
    values: list[int] = []
    for offer in getattr(match, "offers", []) or []:
        pid = getattr(offer, "product_id", None)
        if pid is None:
            continue
        value = historical_mins.get(int(pid))
        if value is not None:
            values.append(int(value))
    if not values:
        return None
    return min(values)


def score_offer(
    *,
    price: int | None,
    gpu: str | None = None,
    ram_gb: int | None = None,
    ssd_gb: int | None = None,
    screen_inch: float | None = None,
    saving_vs_next: int | None = None,
    is_historical_low: bool = False,
    cpu: str | None = None,
    screen_resolution: str | None = None,
    historical_min: int | None = None,
) -> ScoreResult:
    breakdown: dict[str, float] = {
        "gpu": 0.0,
        "price": 0.0,
        "cpu": 0.0,
        "ram": 0.0,
        "ssd": 0.0,
        "screen": 0.0,
        "history": 0.0,
        "saving": 0.0,
    }
    reasons: list[str] = []

    g_pts, g_reason = score_gpu(gpu)
    breakdown["gpu"] = g_pts
    if g_reason:
        reasons.append(g_reason)

    p_pts, p_reason = score_price_value(price, gpu)
    breakdown["price"] = p_pts
    if p_reason:
        reasons.append(p_reason)

    c_pts, c_reason = classify_cpu(cpu)
    breakdown["cpu"] = c_pts
    if c_reason:
        reasons.append(c_reason)

    r_pts, r_reason = score_ram(ram_gb)
    breakdown["ram"] = r_pts
    if r_reason:
        reasons.append(r_reason)

    s_pts, s_reason = score_ssd(ssd_gb)
    breakdown["ssd"] = s_pts
    if s_reason:
        reasons.append(s_reason)

    sc_pts, sc_reason = score_screen(screen_inch, screen_resolution)
    breakdown["screen"] = sc_pts
    if sc_reason:
        reasons.append(sc_reason)

    h_pts, h_reason = score_historical_opportunity(
        price, historical_min, is_historical_low=is_historical_low
    )
    breakdown["history"] = h_pts
    if h_reason:
        reasons.append(h_reason)

    sv_pts, _ = score_cross_store_saving(saving_vs_next)
    breakdown["saving"] = sv_pts

    total = round(sum(breakdown.values()), 2)
    total = round(_clamp(total, 0.0, 100.0), 2)
    confidence = compute_confidence(
        gpu=gpu,
        cpu=cpu,
        ram_gb=ram_gb,
        ssd_gb=ssd_gb,
        screen_inch=screen_inch,
        historical_min=historical_min,
    )
    # Keep 4–5 most useful reasons; GPU/price/history first if present.
    reasons = _select_reasons(reasons, breakdown)
    return ScoreResult(
        score=total,
        reasons=reasons,
        confidence=confidence,
        breakdown=breakdown,
    )


def _select_reasons(reasons: list[str], breakdown: Mapping[str, float]) -> list[str]:
    if len(reasons) <= 5:
        return reasons
    # Prefer hardware + value signals already appended in priority order.
    return reasons[:5]


def _norm_spec_value(field: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if field in {"gpu", "cpu", "screen_resolution"}:
        return str(value).upper().replace(" ", "")
    if field in {"ram_gb", "ssd_gb"}:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if field == "screen_inch":
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return value
    return value


def _merge_spec_field(
    current: Any,
    incoming: Any,
    *,
    field: str,
    conflicts: list[str],
) -> Any:
    if field in conflicts:
        return None
    if incoming is None or incoming == "":
        return current
    if current is None or current == "":
        return incoming
    if _norm_spec_value(field, current) == _norm_spec_value(field, incoming):
        return current
    # Conflicting known values → do not use for ranking/confidence.
    if field not in conflicts:
        conflicts.append(field)
    return None


def _extract_specs(
    match: Any,
    best: Any,
    specs_by_key: Mapping[tuple[str, str], Any] | None,
) -> dict[str, Any]:
    """
    Resolve specs for a cluster without inventing values.

    Prefer identity/cache from any offer in the cluster (not only the cheapest
    store), then fall back to title/URL extraction across offer names.
    Conflicting known values across offers are dropped (SPEC_CONFLICT).
    """
    gpu = None
    ram_gb = None
    ssd_gb = None
    screen_inch = None
    cpu = None
    screen_resolution = None
    conflicts: list[str] = []

    offers = list(getattr(match, "offers", []) or [])
    # Cheapest/fresh best first, then remaining offers.
    ordered_offers = [best] + [o for o in offers if o is not best]

    if specs_by_key:
        for offer in ordered_offers:
            key = (offer.store, offer.external_id)
            spec = specs_by_key.get(key)
            if spec is None:
                continue
            gpu = _merge_spec_field(
                gpu,
                getattr(spec, "gpu", None) or getattr(spec, "normalized_gpu", None),
                field="gpu",
                conflicts=conflicts,
            )
            ram_gb = _merge_spec_field(
                ram_gb, getattr(spec, "ram_gb", None), field="ram_gb", conflicts=conflicts
            )
            ssd_gb = _merge_spec_field(
                ssd_gb, getattr(spec, "ssd_gb", None), field="ssd_gb", conflicts=conflicts
            )
            screen_inch = _merge_spec_field(
                screen_inch,
                getattr(spec, "screen_inch", None)
                or getattr(spec, "screen_size_inch", None),
                field="screen_inch",
                conflicts=conflicts,
            )
            cpu = _merge_spec_field(
                cpu, getattr(spec, "cpu", None), field="cpu", conflicts=conflicts
            )
            screen_resolution = _merge_spec_field(
                screen_resolution,
                getattr(spec, "screen_resolution", None),
                field="screen_resolution",
                conflicts=conflicts,
            )

    from stores.common import extract_specs_from_name

    # Title fallback is weaker: fill only still-missing fields; never override.
    name_parts = [getattr(match, "name", None)]
    for offer in ordered_offers:
        name_parts.append(getattr(offer, "name", None))
        name_parts.append(getattr(offer, "url", None))
    name_blob = " ".join(x for x in name_parts if x)
    inferred = extract_specs_from_name(name_blob)
    if gpu is None and "gpu" not in conflicts:
        gpu = inferred.get("gpu")
    if ram_gb is None and "ram_gb" not in conflicts:
        ram_gb = inferred.get("ram_gb")
    if ssd_gb is None and "ssd_gb" not in conflicts:
        ssd_gb = inferred.get("ssd_gb")
    if screen_inch is None and "screen_inch" not in conflicts:
        screen_inch = inferred.get("screen_inch")
    if cpu is None and "cpu" not in conflicts:
        cpu = inferred.get("cpu")
    if screen_resolution is None and "screen_resolution" not in conflicts:
        screen_resolution = inferred.get("screen_resolution")
    return {
        "gpu": gpu,
        "ram_gb": ram_gb,
        "ssd_gb": ssd_gb,
        "screen_inch": screen_inch,
        "cpu": cpu,
        "screen_resolution": screen_resolution,
        "spec_conflicts": list(conflicts),
    }


def rank_clusters(
    matches: Sequence[Any],
    *,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
    limit: int | None = None,
    fresh_stores: set[str] | None = None,
    allowed_stores: set[str] | None = None,
    historical_mins: Mapping[int, int] | None = None,
    history_starts: Mapping[int, str] | None = None,
    hard_filters: bool = False,
    exclusions: list[dict[str, Any]] | None = None,
) -> list[RankedDeal]:
    """
    Rank cheapest available offer per matched model cluster.

    fresh_stores / allowed_stores: if set, only those stores participate
    in cheapest / second-cheapest / saving calculation.

    historical_mins: optional product_id → all-time min (preloaded, no DB I/O here).
    history_starts: optional product_id → first observation (does not affect score).

    hard_filters: drop clusters that fail laptop_eligibility (GPU, installed
    RAM, screen). Scores of the remaining clusters are unchanged.
    exclusions: when given, receives one record per dropped cluster.
    """
    allowed = fresh_stores if fresh_stores is not None else allowed_stores
    ranked: list[RankedDeal] = []
    for match in matches:
        available = [
            o
            for o in match.offers
            if o.available
            and o.price is not None
            and config.is_price_in_tracking_scope(o.price)
            and (allowed is None or o.store in allowed)
        ]
        if not available:
            if exclusions is not None:
                exclusions.append(_exclusion_record(match, allowed, specs_by_key))
            continue
        ordered = sorted(available, key=lambda o: (int(o.price), o.store))
        best = ordered[0]
        # Saving is a cross-store figure: the next offer must be another store.
        nxt = next((o for o in ordered[1:] if o.store != best.store), None)
        saving = int(nxt.price) - int(best.price) if nxt is not None else None

        specs = _extract_specs(match, best, specs_by_key)
        if hard_filters:
            from laptop_eligibility import evaluate_hardware

            verdict = evaluate_hardware(
                gpu=specs["gpu"],
                ram_gb=specs["ram_gb"],
                screen_resolution=specs["screen_resolution"],
            )
            if not verdict.eligible:
                if exclusions is not None:
                    exclusions.append(
                        _exclusion_dict(
                            match,
                            reason=verdict.reason,
                            details=verdict.details,
                            specs=specs,
                            price=int(best.price),
                            store=best.store,
                        )
                    )
                continue
        hist_min = cluster_historical_min(match, historical_mins)

        result = score_offer(
            price=int(best.price),
            gpu=specs["gpu"],
            ram_gb=specs["ram_gb"],
            ssd_gb=specs["ssd_gb"],
            screen_inch=specs["screen_inch"],
            saving_vs_next=saving,
            cpu=specs["cpu"],
            screen_resolution=specs["screen_resolution"],
            historical_min=hist_min,
        )
        ranked.append(
            RankedDeal(
                score=result.score,
                reasons=result.reasons,
                offer={
                    "store": best.store,
                    "external_id": best.external_id,
                    "sku": best.sku,
                    "name": match.name,
                    "price": int(best.price),
                    "url": best.url,
                },
                cluster_name=match.name,
                gpu=specs["gpu"],
                store=best.store,
                price=int(best.price),
                url=best.url,
                saving_vs_next=saving,
                next_store=nxt.store if nxt else None,
                next_price=int(nxt.price) if nxt else None,
                ram_gb=specs["ram_gb"],
                ssd_gb=specs["ssd_gb"],
                screen_inch=specs["screen_inch"],
                cpu=specs["cpu"],
                confidence=result.confidence,
                score_breakdown=dict(result.breakdown),
                historical_min=hist_min,
                screen_resolution=specs["screen_resolution"],
                cluster_key=getattr(match, "normalized_sku", None),
                history_started_at=cluster_history_started_at(match, history_starts),
            )
        )

    ranked.sort(key=lambda d: (-d.score, d.price or 10**12, d.cluster_name))
    if limit is not None:
        return ranked[: int(limit)]
    return ranked


def cluster_specs(
    match: Any,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Cluster specs exactly as rank_clusters resolves them (cheapest offer first)."""
    offers = list(getattr(match, "offers", []) or [])
    if not offers:
        return {
            "gpu": None,
            "ram_gb": None,
            "ssd_gb": None,
            "screen_inch": None,
            "cpu": None,
            "screen_resolution": None,
            "spec_conflicts": [],
        }
    priced = [o for o in offers if o.price is not None]
    best = min(priced, key=lambda o: (int(o.price), o.store)) if priced else offers[0]
    return _extract_specs(match, best, specs_by_key)


def _exclusion_dict(
    match: Any,
    *,
    reason: str | None,
    details: Sequence[str],
    specs: Mapping[str, Any],
    price: int | None,
    store: str | None,
) -> dict[str, Any]:
    return {
        "cluster_name": getattr(match, "name", None),
        "reason": reason,
        "details": list(details),
        "price": price,
        "store": store,
        "gpu": specs.get("gpu"),
        "ram_gb": specs.get("ram_gb"),
        "screen_resolution": specs.get("screen_resolution"),
        "screen_inch": specs.get("screen_inch"),
        "offers": [
            (o.store, o.external_id, o.price, bool(o.available))
            for o in getattr(match, "offers", []) or []
        ],
    }


def _exclusion_record(
    match: Any,
    allowed: set[str] | None,
    specs_by_key: Mapping[tuple[str, str], Any] | None,
) -> dict[str, Any]:
    """NOT_AVAILABLE or PRICE_OVER_CAP for a cluster with no in-scope offer."""
    from laptop_eligibility import NOT_AVAILABLE, PRICE_OVER_CAP, evaluate_hardware

    in_stores = [
        o
        for o in getattr(match, "offers", []) or []
        if allowed is None or o.store in allowed
    ]
    selling = [o for o in in_stores if o.available and o.price is not None and int(o.price) > 0]
    specs = cluster_specs(match, specs_by_key)
    hardware = evaluate_hardware(
        gpu=specs.get("gpu"),
        ram_gb=specs.get("ram_gb"),
        screen_resolution=specs.get("screen_resolution"),
    )
    also = [f"also:{d}" for d in hardware.details]
    if selling:
        cheapest = min(selling, key=lambda o: (int(o.price), o.store))
        return _exclusion_dict(
            match,
            reason=PRICE_OVER_CAP,
            details=[f"price_{int(cheapest.price)}", *also],
            specs=specs,
            price=int(cheapest.price),
            store=cheapest.store,
        )
    return _exclusion_dict(
        match,
        reason=NOT_AVAILABLE,
        details=["no_available_offer_in_fresh_stores", *also],
        specs=specs,
        price=None,
        store=None,
    )


def _store_label(store: str) -> str:
    if store == "andpro":
        return "ANDPRO"
    if store == "kns":
        return "KNS"
    return store.capitalize()


def format_top_deals_message(
    deals: Sequence[RankedDeal],
    *,
    limit: int = 10,
    max_age_minutes: int | None = None,
    empty_message: str | None = None,
    footer: str = "",
) -> str:
    cap_label = config.format_price_cap_label()
    if empty_message is None:
        empty_message = f"Нет свежих предложений до {cap_label} ₽."
    footer = (footer or "").strip()
    if not deals:
        return f"{empty_message}\n\n{footer}" if footer else empty_message
    lines = [f"<b>ТОП ПРЕДЛОЖЕНИЙ ДО {cap_label} ₽</b>"]
    if max_age_minutes is not None:
        lines.append(f"Данные не старше: {max_age_minutes} мин.")
    lines.append("")
    for i, deal in enumerate(deals[:limit], start=1):
        price = f"{deal.price:,}".replace(",", " ") if deal.price else "?"
        lines.append(f"<b>{i}. {deal.cluster_name}</b>")
        if deal.gpu:
            lines.append(str(deal.gpu).replace(" LAPTOP", "").replace(" TI", " Ti"))
        cfg = []
        if deal.ram_gb:
            cfg.append(f"{deal.ram_gb}GB")
        if deal.ssd_gb:
            if deal.ssd_gb >= 1000 and deal.ssd_gb % 1024 == 0:
                cfg.append(f"{deal.ssd_gb // 1024}TB")
            elif deal.ssd_gb >= 1000:
                cfg.append("1TB" if deal.ssd_gb < 1500 else f"{deal.ssd_gb}GB")
            else:
                cfg.append(f"{deal.ssd_gb}GB")
        if deal.screen_inch:
            cfg.append(f'{deal.screen_inch:g}"')
        if cfg:
            lines.append(" / ".join(cfg))
        lines.append(f"{_store_label(deal.store)}: {price} ₽")
        lines.append(f"Оценка: {deal.score:g}/100")
        if deal.confidence < 100:
            lines.append(f"Данные: {deal.confidence}%")
        if deal.reasons:
            lines.append("Почему: " + " · ".join(deal.reasons[:5]))
        if (
            deal.saving_vs_next
            and deal.next_store
            and deal.saving_vs_next >= int(config.SCORE_SAVING_TIER_1)
        ):
            lines.append(
                f"Экономия vs {_store_label(deal.next_store)}: "
                f"{deal.saving_vs_next:,} ₽".replace(",", " ")
            )
        if deal.url:
            lines.append(deal.url)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    text = "\n".join(lines)
    soft = int(getattr(config, "TELEGRAM_TOP_SOFT_LIMIT", 3500))
    if footer:
        soft = max(1000, soft - len(footer) - 2)
    if len(text) > soft:
        # Drop URLs from the bottom entries first.
        trimmed = text
        while len(trimmed) > soft and "\nhttp" in trimmed:
            idx = trimmed.rfind("\nhttp")
            end = trimmed.find("\n", idx + 1)
            if end < 0:
                trimmed = trimmed[:idx]
            else:
                trimmed = trimmed[:idx] + trimmed[end:]
        text = trimmed[:soft]
    if footer:
        text = f"{text}\n\n{footer}"
    return text
