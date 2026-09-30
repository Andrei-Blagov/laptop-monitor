from __future__ import annotations

"""Explainable deal ranking for TOP / Telegram priority."""

from dataclasses import dataclass, field
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


def _gpu_bonus(gpu: str | None) -> tuple[float, str | None]:
    if not gpu:
        return 0.0, None
    g = gpu.upper()
    if "5080" in g and "TI" not in g:
        return float(config.RANK_GPU_5080_BONUS), "+ RTX 5080"
    if "5070" in g and "TI" in g:
        return float(config.RANK_GPU_5070TI_BONUS), "+ RTX 5070 Ti"
    return 0.0, None


def _ram_bonus(ram_gb: int | None) -> tuple[float, str | None]:
    if ram_gb is None:
        return 0.0, None
    if ram_gb >= 32:
        return float(config.RANK_RAM_32_BONUS), f"+ {ram_gb}GB RAM"
    if ram_gb >= 16:
        return float(config.RANK_RAM_16_BONUS), f"+ {ram_gb}GB RAM"
    return 0.0, None


def _ssd_bonus(ssd_gb: int | None) -> tuple[float, str | None]:
    if ssd_gb is None:
        return 0.0, None
    if ssd_gb >= 1000:
        return float(config.RANK_SSD_1TB_BONUS), f"+ {ssd_gb}GB SSD"
    return 0.0, None


def _screen_bonus(inch: float | None) -> tuple[float, str | None]:
    if inch is None:
        return 0.0, None
    if inch >= 17:
        return float(config.RANK_SCREEN_17_BONUS), f"+ {inch:g}\""
    if inch >= 16:
        return float(config.RANK_SCREEN_16_BONUS), f"+ {inch:g}\""
    return 0.0, None


def _price_value_score(
    price: int | None,
    gpu: str | None,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if price is None:
        return 0.0, reasons
    target = None
    if gpu:
        g = gpu.upper()
        if "5080" in g and "TI" not in g:
            target = config.TARGET_PRICES.get("RTX 5080")
        elif "5070" in g and "TI" in g:
            target = config.TARGET_PRICES.get("RTX 5070 Ti")
    score = 0.0
    if target is not None:
        # Closer / under target → higher score (cap 40).
        delta = target - price
        pts = max(-20.0, min(40.0, delta / 5000.0 * 10.0))
        score += pts
        if delta >= 0:
            reasons.append(f"+ цена ≤ target ({price:,} ≤ {target:,})".replace(",", " "))
        elif delta > -50_000:
            reasons.append("+ цена близко к target")
    # Budget anchor ~200k
    budget = int(config.RANK_BUDGET_ANCHOR_RUB)
    if price <= budget:
        score += float(config.RANK_BUDGET_UNDER_BONUS)
        reasons.append(f"+ в бюджете ≤ {budget:,}".replace(",", " "))
    elif price <= budget * 1.25:
        score += float(config.RANK_BUDGET_NEAR_BONUS)
        reasons.append("+ около бюджета")
    return score, reasons


def score_offer(
    *,
    price: int | None,
    gpu: str | None = None,
    ram_gb: int | None = None,
    ssd_gb: int | None = None,
    screen_inch: float | None = None,
    saving_vs_next: int | None = None,
    is_historical_low: bool = False,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    pv, pr = _price_value_score(price, gpu)
    score += pv
    reasons.extend(pr)

    b, r = _gpu_bonus(gpu)
    score += b
    if r:
        reasons.append(r)

    b, r = _ram_bonus(ram_gb)
    score += b
    if r:
        reasons.append(r)

    b, r = _ssd_bonus(ssd_gb)
    score += b
    if r:
        reasons.append(r)

    b, r = _screen_bonus(screen_inch)
    score += b
    if r:
        reasons.append(r)

    if saving_vs_next and saving_vs_next >= int(config.CROSS_STORE_DIFFERENCE_RUB):
        pts = min(25.0, saving_vs_next / 1000.0)
        score += pts
        reasons.append(
            f"+ дешевле следующего магазина на {saving_vs_next:,} ₽".replace(",", " ")
        )

    if is_historical_low:
        score += float(config.RANK_HISTORICAL_LOW_BONUS)
        reasons.append("+ новый исторический минимум")

    return round(score, 2), reasons


def rank_clusters(
    matches: Sequence[Any],
    *,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
    limit: int | None = None,
    fresh_stores: set[str] | None = None,
    allowed_stores: set[str] | None = None,
) -> list[RankedDeal]:
    """
    Rank cheapest available offer per matched model cluster.

    fresh_stores / allowed_stores: if set, only those stores participate
    in cheapest / second-cheapest / saving calculation.
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
            continue
        ordered = sorted(available, key=lambda o: (int(o.price), o.store))
        best = ordered[0]
        nxt = ordered[1] if len(ordered) > 1 else None
        saving = (
            int(nxt.price) - int(best.price) if nxt is not None else None
        )

        gpu = None
        ram_gb = None
        ssd_gb = None
        screen_inch = None
        if specs_by_key:
            key = (best.store, best.external_id)
            spec = specs_by_key.get(key)
            if spec is not None:
                gpu = getattr(spec, "gpu", None) or getattr(spec, "normalized_gpu", None)
                ram_gb = getattr(spec, "ram_gb", None)
                ssd_gb = getattr(spec, "ssd_gb", None)
                screen_inch = getattr(spec, "screen_inch", None) or getattr(
                    spec, "screen_size_inch", None
                )
        if gpu is None or ram_gb is None or ssd_gb is None or screen_inch is None:
            from stores.common import extract_specs_from_name

            name_blob = " ".join(
                x
                for x in (
                    match.name,
                    getattr(best, "name", None),
                    best.url,
                )
                if x
            )
            inferred = extract_specs_from_name(name_blob)
            gpu = gpu or inferred.get("gpu")  # type: ignore[assignment]
            ram_gb = ram_gb if ram_gb is not None else inferred.get("ram_gb")  # type: ignore[assignment]
            ssd_gb = ssd_gb if ssd_gb is not None else inferred.get("ssd_gb")  # type: ignore[assignment]
            screen_inch = (
                screen_inch
                if screen_inch is not None
                else inferred.get("screen_inch")  # type: ignore[assignment]
            )

        score, reasons = score_offer(
            price=int(best.price),
            gpu=gpu,
            ram_gb=ram_gb,
            ssd_gb=ssd_gb,
            screen_inch=screen_inch,
            saving_vs_next=saving,
        )
        ranked.append(
            RankedDeal(
                score=score,
                reasons=reasons,
                offer={
                    "store": best.store,
                    "external_id": best.external_id,
                    "sku": best.sku,
                    "name": match.name,
                    "price": int(best.price),
                    "url": best.url,
                },
                cluster_name=match.name,
                gpu=gpu,
                store=best.store,
                price=int(best.price),
                url=best.url,
                saving_vs_next=saving,
                next_store=nxt.store if nxt else None,
                next_price=int(nxt.price) if nxt else None,
                ram_gb=ram_gb,
                ssd_gb=ssd_gb,
                screen_inch=screen_inch,
            )
        )

    ranked.sort(key=lambda d: (-d.score, d.price or 10**12, d.cluster_name))
    if limit is not None:
        return ranked[: int(limit)]
    return ranked


def format_top_deals_message(
    deals: Sequence[RankedDeal],
    *,
    limit: int = 10,
    max_age_minutes: int | None = None,
    empty_message: str | None = None,
) -> str:
    cap_label = config.format_price_cap_label()
    if empty_message is None:
        empty_message = f"Нет свежих предложений до {cap_label} ₽."
    if not deals:
        return empty_message
    lines = [f"<b>ТОП ПРЕДЛОЖЕНИЙ ДО {cap_label} ₽</b>"]
    if max_age_minutes is not None:
        lines.append(f"Данные не старше: {max_age_minutes} мин.")
    lines.append("")
    for i, deal in enumerate(deals[:limit], start=1):
        price = f"{deal.price:,}".replace(",", " ") if deal.price else "?"
        lines.append(f"<b>{i}. {deal.cluster_name}</b>")
        if deal.gpu:
            lines.append(deal.gpu)
        cfg = []
        if deal.ram_gb:
            cfg.append(f"{deal.ram_gb}GB")
        if deal.ssd_gb:
            cfg.append(f"{deal.ssd_gb}GB SSD")
        if deal.screen_inch:
            cfg.append(f'{deal.screen_inch:g}"')
        if cfg:
            lines.append(" / ".join(cfg))
        store_label = (
            deal.store.upper() if deal.store == "andpro" else deal.store.capitalize()
        )
        lines.append(f"{store_label}: {price} ₽")
        if deal.saving_vs_next and deal.next_store:
            lines.append(
                f"Экономия vs {deal.next_store}: {deal.saving_vs_next:,} ₽".replace(
                    ",", " "
                )
            )
        lines.append(f"Priority score: {deal.score:g}")
        if deal.reasons:
            lines.append("Почему: " + "; ".join(deal.reasons[:5]))
        if deal.url:
            lines.append(deal.url)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)