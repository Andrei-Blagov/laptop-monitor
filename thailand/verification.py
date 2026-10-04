from __future__ import annotations

"""Thailand offer verification / provenance helpers."""

from typing import Any

from deal_ranking import canonical_gpu
from thailand.models import ThailandOffer

# GPU provenance (strongest → weakest for documentation; search_query is never authoritative).
GPU_SOURCE_STRUCTURED_PRODUCT_PAGE = "structured_product_page"
GPU_SOURCE_STRUCTURED_API = "structured_api"
GPU_SOURCE_EXPLICIT_TITLE = "explicit_title"
GPU_SOURCE_CATALOG_FILTER = "catalog_filter"
GPU_SOURCE_SEARCH_QUERY = "search_query"
GPU_SOURCE_UNKNOWN = "unknown"

# Authoritative GPU sources only (search_query / catalog_filter NEVER qualify).
AUTHORITATIVE_GPU_SOURCES = frozenset(
    {
        GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
        GPU_SOURCE_STRUCTURED_API,
        GPU_SOURCE_EXPLICIT_TITLE,
    }
)

VERIFIED = "VERIFIED"
PARTIAL = "PARTIAL"
UNVERIFIED = "UNVERIFIED"

AVAIL_SOURCE_PRODUCT_DETAIL = "product_detail"
AVAIL_SOURCE_STRUCTURED_API = "structured_api"
AVAIL_SOURCE_SEARCH_SUGGESTION = "search_suggestion"
AVAIL_SOURCE_UNKNOWN = "unknown"

PRICE_SOURCE_SEARCH_SUGGESTION = "search_suggestion"
PRICE_SOURCE_PRODUCT_DETAIL = "product_detail"
PRICE_SOURCE_STRUCTURED_API = "structured_api"
PRICE_SOURCE_UNKNOWN = "unknown"


def gpu_is_authoritative(offer: ThailandOffer) -> bool:
    src = (offer.gpu_source or GPU_SOURCE_UNKNOWN).strip()
    if src not in AUTHORITATIVE_GPU_SOURCES:
        return False
    return canonical_gpu(offer.gpu) in {"RTX 5070 Ti", "RTX 5080"}


def availability_confirmed(offer: ThailandOffer) -> bool:
    if not offer.availability_confirmed:
        return False
    return offer.availability_status in {
        "in_stock",
        "online_available",
        "store_pickup_only",
        "out_of_stock",
        "preorder",
    }


def is_purchasable_confirmed(offer: ThailandOffer) -> bool:
    """Automatic BEST/TOP: only confirmed purchasable statuses."""
    if not offer.availability_confirmed:
        return False
    if not offer.available:
        return False
    return offer.availability_status in {
        "in_stock",
        "online_available",
        "store_pickup_only",
    }


def compute_verification(offer: ThailandOffer) -> ThailandOffer:
    """
    Set verification_status / reasons / availability_confirmed consistency.

    VERIFIED (for automatic ranking) requires:
    - target GPU authoritative
    - price known
    - purchasable availability confirmed
    """
    reasons: list[str] = []
    gpu_ok = gpu_is_authoritative(offer)
    price_ok = offer.price_thb is not None and offer.price_thb > 0
    avail_ok = is_purchasable_confirmed(offer)

    if not gpu_ok:
        if offer.gpu:
            reasons.append(f"gpu_source_untrusted:{offer.gpu_source or 'unknown'}")
        else:
            reasons.append("gpu_missing")
        if offer.candidate_gpu:
            reasons.append(f"candidate_gpu:{offer.candidate_gpu}")
    else:
        reasons.append(f"gpu_confirmed:{offer.gpu}:{offer.gpu_source}")

    if not price_ok:
        reasons.append("price_missing")
    else:
        reasons.append(f"price_source:{offer.price_source or 'unknown'}")

    if not offer.availability_confirmed:
        reasons.append("availability_unconfirmed")
    elif offer.availability_status == "out_of_stock":
        reasons.append("out_of_stock")
    elif offer.availability_status == "preorder":
        reasons.append("preorder")
    elif avail_ok:
        reasons.append(f"availability_confirmed:{offer.availability_status}")
    else:
        reasons.append(f"availability:{offer.availability_status}")

    for field, label in (
        (offer.cpu, "cpu"),
        (offer.ram_gb, "ram"),
        (offer.ssd_gb, "ssd"),
        (offer.screen_size_inch, "screen"),
    ):
        if field is None:
            reasons.append(f"{label}_unknown")

    if gpu_ok and price_ok and avail_ok:
        status = VERIFIED
    elif gpu_ok and price_ok:
        status = PARTIAL
    else:
        status = UNVERIFIED

    offer.verification_status = status
    offer.verification_reasons = reasons
    return offer


def is_verified_for_ranking(offer: ThailandOffer) -> bool:
    if offer.verification_status != VERIFIED:
        # Recompute if unset
        if not offer.verification_status:
            compute_verification(offer)
        return offer.verification_status == VERIFIED
    return True


def international_confidence(offer: ThailandOffer) -> int:
    """0..100 confidence separate from international_value_score."""
    pts = 0
    if gpu_is_authoritative(offer):
        pts += 30
    elif offer.gpu:
        pts += 5  # untrusted GPU → keep low
    if offer.price_thb is not None:
        pts += 20
    if offer.availability_confirmed and offer.availability_status in {
        "in_stock",
        "online_available",
        "store_pickup_only",
    }:
        pts += 20
    elif offer.availability_confirmed:
        pts += 5
    if offer.cpu:
        pts += 10
    if offer.ram_gb is not None:
        pts += 8
    if offer.ssd_gb is not None:
        pts += 7
    if offer.screen_size_inch is not None:
        pts += 5
    # Cap: unknown GPU/availability cannot be high confidence
    if not gpu_is_authoritative(offer) or not offer.availability_confirmed:
        pts = min(pts, 40)
    return max(0, min(100, pts))


def offer_snapshot_fields(offer: ThailandOffer) -> dict[str, Any]:
    return {
        "verification_status": offer.verification_status,
        "verification_reasons": list(offer.verification_reasons or []),
        "gpu_source": offer.gpu_source,
        "candidate_gpu": offer.candidate_gpu,
        "availability_source": offer.availability_source,
        "availability_confirmed": offer.availability_confirmed,
        "price_source": offer.price_source,
        "international_confidence": international_confidence(offer),
    }
