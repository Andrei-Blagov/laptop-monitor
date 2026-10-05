from __future__ import annotations

"""Marketplace seller trust classification (separate from hardware score)."""

from typing import Any

import config
from thailand.models import ThailandOffer

TIER_A = "TIER_A"
TIER_B = "TIER_B"
TIER_C = "TIER_C"

# Seed known major Thailand retailers / brand official stores (verified from listing).
TRUSTED_SELLER_NAMES = frozenset(
    {
        "asus official store",
        "asus",
        "lenovo",
        "lenovo official store",
        "jib computer group",
        "jib",
        "advice online",
        "advice",
        "banana it",
        "msi official store",
        "msi",
        "gigabyte official store",
        "gigabyte",
        "aorus",
        "gigabyte | aorus",
        "acer official store",
        "acer",
    }
)

# Exact seller aliases for BaNANA IT marketplace identity (no fuzzy guessing).
BANANA_IT_SELLER_ALIASES = frozenset(
    {
        "banana it",
        "banana it store",
        "banana it official store",
        "bnn",
        "banana",
    }
)


def _norm_name(name: str | None) -> str:
    return " ".join(str(name or "").strip().lower().split())


def is_banana_it_seller(name: str | None) -> bool:
    n = _norm_name(name)
    if not n:
        return False
    if n in BANANA_IT_SELLER_ALIASES:
        return True
    return n.startswith("banana it")


def is_jib_seller(name: str | None) -> bool:
    n = _norm_name(name)
    return bool(n) and ("jib" in n)


def is_seed_trusted_seller(name: str | None) -> bool:
    n = _norm_name(name)
    if not n:
        return False
    if is_banana_it_seller(name):
        return True
    if n in TRUSTED_SELLER_NAMES:
        return True
    for seed in TRUSTED_SELLER_NAMES:
        if seed in n or n in seed:
            return True
    return False


def annotate_marketplace_identity(offer: ThailandOffer) -> ThailandOffer:
    """Set channel / retailer_brand from verified listing seller identity."""
    if not offer.marketplace:
        offer.channel = offer.channel or "direct"
        return offer
    offer.channel = offer.channel or "lazada"
    if is_banana_it_seller(offer.seller_name):
        offer.retailer_brand = "BaNANA"
    elif is_jib_seller(offer.seller_name):
        offer.retailer_brand = "JIB"
    return offer


def classify_seller_trust(offer: ThailandOffer) -> str:
    """
    TIER_A: official brand / LazMall / known major retailer
    TIER_B: established seller with adequate rating/reviews when available
    TIER_C: unknown / weak / insufficient data
    """
    if not offer.marketplace:
        return TIER_A  # direct retailer channel
    annotate_marketplace_identity(offer)
    if offer.official_store or offer.mall:
        return TIER_A
    if is_banana_it_seller(offer.seller_name) or is_seed_trusted_seller(offer.seller_name):
        return TIER_A

    min_rating = float(getattr(config, "THAILAND_MARKETPLACE_MIN_SELLER_RATING", 4.7))
    min_reviews = int(getattr(config, "THAILAND_MARKETPLACE_MIN_REVIEWS", 50))
    min_sold = int(getattr(config, "THAILAND_MARKETPLACE_MIN_UNITS_SOLD", 20))

    rating = offer.seller_rating
    reviews = offer.seller_reviews_count
    sold = offer.units_sold
    has_metric = rating is not None or reviews is not None or sold is not None
    if not has_metric:
        # No marketplace evidence → unknown → C
        if offer.seller_name:
            return TIER_C
        return TIER_C

    rating_ok = rating is None or float(rating) >= min_rating
    reviews_ok = reviews is None or int(reviews) >= min_reviews
    sold_ok = sold is None or int(sold) >= min_sold
    # Require the metrics that exist to pass.
    if rating_ok and reviews_ok and sold_ok and (
        rating is not None or reviews is not None or sold is not None
    ):
        # At least one strong signal present and none failing.
        if (rating is not None and float(rating) >= min_rating) or (
            reviews is not None and int(reviews) >= min_reviews
        ) or (sold is not None and int(sold) >= min_sold):
            return TIER_B
    return TIER_C


def marketplace_confidence(offer: ThailandOffer, *, tier: str | None = None) -> float:
    """0..100 confidence separate from hardware international_value_score."""
    tier = tier or offer.seller_trust_tier or classify_seller_trust(offer)
    base = {TIER_A: 90.0, TIER_B: 70.0, TIER_C: 35.0}.get(tier, 30.0)
    if offer.official_store:
        base = max(base, 95.0)
    if offer.mall:
        base = max(base, 92.0)
    if offer.price_verified_for_variant:
        base = min(100.0, base + 3.0)
    else:
        base = max(0.0, base - 25.0)
    return round(base, 2)


def marketplace_eligible_for_top(
    offer: ThailandOffer, *, tier: str | None = None
) -> bool:
    tier = tier or offer.seller_trust_tier or classify_seller_trust(offer)
    if tier == TIER_C:
        return False
    if tier in {TIER_A, TIER_B}:
        return True
    return False


def store_display_label(offer: ThailandOffer | dict[str, Any]) -> str:
    """Telegram / report channel label."""
    if isinstance(offer, dict):
        store = str(offer.get("store") or "")
        marketplace = bool(offer.get("marketplace"))
        seller = offer.get("seller_name")
        brand = offer.get("retailer_brand")
        channel = offer.get("channel")
    else:
        store = offer.store
        marketplace = bool(offer.marketplace)
        seller = offer.seller_name
        brand = offer.retailer_brand
        channel = offer.channel
    if store == "lazada" or marketplace or channel == "lazada":
        name = str(seller or "unknown seller").strip()
        if brand == "BaNANA" or is_banana_it_seller(name):
            # Never mislabel as direct BaNANA.
            return "Lazada · BaNANA IT"
        return f"Lazada · {name}"
    if store == "banana":
        return "BaNANA"
    return {
        "jib": "JIB",
        "advice": "Advice",
        "banana": "BaNANA",
        "lazada": "Lazada",
        "speedcom": "SpeedCom",
        "invadeit": "InvadeIT",
        "itcity": "IT City",
    }.get(store, store.upper())
