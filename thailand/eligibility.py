from __future__ import annotations

"""Thailand user-facing eligibility (price cap + marketplace trust)."""

from typing import Any

import config
from thailand.fx import fx_usable_for_verdict, thb_to_rub
from thailand.models import FxRate, ThailandOffer
from thailand.seller_trust import (
    TIER_A,
    TIER_B,
    TIER_C,
    classify_seller_trust,
    marketplace_eligible_for_top,
)
from thailand.verification import is_purchasable_confirmed, is_verified_for_ranking

EXCLUDED_PRICE_ABOVE_CAP = "price_above_cap_rub"
EXCLUDED_FX_UNUSABLE = "fx_unusable_for_price_cap"
EXCLUDED_NOT_VERIFIED = "not_verified"
EXCLUDED_NOT_PURCHASABLE = "not_purchasable"
EXCLUDED_SELLER_TRUST = "seller_trust_insufficient"
EXCLUDED_VARIANT_PRICE = "variant_price_unverified"
EXCLUDED_NO_PRICE = "price_missing"


def max_tracked_price_rub() -> int:
    return int(config.MAX_TRACKED_PRICE_RUB)


def effective_thb_cap(fx: FxRate | None) -> float | None:
    """Diagnostic helper only — source of truth is converted RUB."""
    if not fx_usable_for_verdict(fx):
        return None
    rub_per = float(fx.rub_per_thb)  # type: ignore[union-attr]
    if rub_per <= 0:
        return None
    return float(max_tracked_price_rub()) / rub_per


def price_within_cap(price_rub: int | None) -> bool:
    if price_rub is None:
        return False
    try:
        return int(price_rub) > 0 and int(price_rub) <= max_tracked_price_rub()
    except (TypeError, ValueError):
        return False


def annotate_offer_price_scope(
    offer: ThailandOffer,
    *,
    fx: FxRate | None,
) -> ThailandOffer:
    """Set in_tracking_scope / excluded_reason for audit (raw offers retained)."""
    usable = fx_usable_for_verdict(fx)
    price_rub = thb_to_rub(offer.price_thb, fx) if usable else None
    offer.metadata = dict(offer.metadata or {})
    offer.metadata["price_rub"] = price_rub
    if offer.price_thb is None:
        offer.in_tracking_scope = False
        offer.excluded_reason = EXCLUDED_NO_PRICE
        return offer
    if not usable:
        offer.in_tracking_scope = False
        offer.excluded_reason = EXCLUDED_FX_UNUSABLE
        return offer
    if price_within_cap(price_rub):
        offer.in_tracking_scope = True
        offer.excluded_reason = None
    else:
        offer.in_tracking_scope = False
        offer.excluded_reason = EXCLUDED_PRICE_ABOVE_CAP
    return offer


def offer_user_facing_eligible(
    offer: ThailandOffer,
    *,
    fx: FxRate | None,
    require_verified: bool = True,
) -> tuple[bool, str | None, int | None]:
    """
    Eligibility for TOP / BEST / alternatives.

    Returns (ok, excluded_reason, price_rub).
    """
    usable = fx_usable_for_verdict(fx)
    if not usable:
        return False, EXCLUDED_FX_UNUSABLE, None
    price_rub = thb_to_rub(offer.price_thb, fx)
    if offer.price_thb is None or price_rub is None:
        return False, EXCLUDED_NO_PRICE, price_rub
    if not price_within_cap(price_rub):
        return False, EXCLUDED_PRICE_ABOVE_CAP, price_rub
    if require_verified and not is_verified_for_ranking(offer):
        return False, EXCLUDED_NOT_VERIFIED, price_rub
    if not is_purchasable_confirmed(offer):
        return False, EXCLUDED_NOT_PURCHASABLE, price_rub
    if offer.marketplace and not offer.price_verified_for_variant:
        return False, EXCLUDED_VARIANT_PRICE, price_rub
    if offer.marketplace:
        tier = offer.seller_trust_tier or classify_seller_trust(offer)
        offer.seller_trust_tier = tier
        if not marketplace_eligible_for_top(offer, tier=tier):
            return False, EXCLUDED_SELLER_TRUST, price_rub
    return True, None, price_rub


def sort_value_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """international_value_score DESC, price_rub ASC, price_thb ASC."""
    return sorted(
        rows,
        key=lambda r: (
            -(r.get("international_score") or 0),
            r.get("price_rub") if r.get("price_rub") is not None else 10**12,
            r.get("price_thb") if r.get("price_thb") is not None else 10**12,
        ),
    )


def sort_cheapest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """User-facing default: price_rub ASC, then value score, then confidence."""
    return sorted(
        rows,
        key=lambda r: (
            r.get("price_rub") if r.get("price_rub") is not None else 10**12,
            -(r.get("international_score") or 0),
            -(r.get("international_confidence") or 0),
            r.get("price_thb") if r.get("price_thb") is not None else 10**12,
        ),
    )


def trust_tiebreak_key(row: dict[str, Any]) -> tuple:
    """Prefer official/mall/TIER_A when prices are close."""
    tier = row.get("seller_trust_tier") or ""
    tier_rank = {TIER_A: 0, TIER_B: 1, TIER_C: 2}.get(tier, 3)
    official = 0 if row.get("official_store") or row.get("mall") else 1
    marketplace = 1 if row.get("marketplace") else 0  # direct slightly preferred
    return (tier_rank, official, marketplace)


def apply_near_price_trust_preference(
    rows: list[dict[str, Any]],
    *,
    near_thb: int | None = None,
) -> list[dict[str, Any]]:
    """
    Among near-price candidates, prefer official/mall/known retailer.

    Does not hide real prices — only reorders within near band after value sort.
    """
    if len(rows) < 2:
        return rows
    band = int(
        near_thb
        if near_thb is not None
        else getattr(config, "THAILAND_NEAR_PRICE_THB", 1500)
    )
    out = list(rows)
    i = 0
    while i < len(out):
        base = out[i]
        base_thb = base.get("price_thb")
        if base_thb is None:
            i += 1
            continue
        j = i + 1
        while j < len(out):
            other = out[j]
            o_thb = other.get("price_thb")
            if o_thb is None or abs(int(o_thb) - int(base_thb)) > band:
                break
            j += 1
        if j > i + 1:
            group = out[i:j]
            group.sort(
                key=lambda r: (
                    trust_tiebreak_key(r),
                    r.get("price_rub") if r.get("price_rub") is not None else 10**12,
                    r.get("price_thb") if r.get("price_thb") is not None else 10**12,
                    -(r.get("international_score") or 0),
                )
            )
            out[i:j] = group
        i = j if j > i + 1 else i + 1
    return out
