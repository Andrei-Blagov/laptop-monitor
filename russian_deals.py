from __future__ import annotations

"""Load current fresh Russian ranked deals (read-only). Shared by pipeline + control bot."""

import re
from pathlib import Path
from typing import Any, Sequence

import config
from comparison import Offer, ProductMatch, match_products, normalize_sku
from deal_ranking import (
    RankedDeal,
    canonical_gpu,
    collect_match_product_ids,
    historical_mins_from_rows,
    history_starts_from_rows,
    rank_clusters,
)
from identity_sync import identity_from_cache, load_specs_cache
from store_freshness import get_fresh_store_slugs
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
)

SINGLE_STORE_METHOD = "single_store"

_CODE_RE = re.compile(r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+")


def single_store_matches(unmatched: Sequence[Offer]) -> list[ProductMatch]:
    """
    One cluster per product that matched no other store.

    The same SKU listed twice by one store becomes one cluster. Offers from
    different stores are never merged here: the matcher left them apart on
    purpose, and duplicates are resolved after ranking by model code and specs.
    """
    groups: dict[tuple[str, str], list[Offer]] = {}
    for offer in unmatched:
        key = normalize_sku(offer.sku) or f"id:{offer.external_id}"
        groups.setdefault((offer.store, key), []).append(offer)
    out: list[ProductMatch] = []
    for (_store, key), offers in groups.items():
        first = offers[0]
        out.append(
            ProductMatch(
                normalized_sku=key,
                display_sku=str(first.sku or first.external_id),
                name=first.name,
                offers=list(offers),
                match_method=SINGLE_STORE_METHOD,
                matched_identifier=key,
            )
        )
    return out


def _model_codes(deal: RankedDeal) -> set[str]:
    text = " ".join(
        str(x or "") for x in (deal.cluster_name, (deal.offer or {}).get("sku"))
    ).upper()
    return {
        code
        for code in _CODE_RE.findall(text)
        if len(code) >= 8 and any(c.isalpha() for c in code) and any(c.isdigit() for c in code)
    }


def _same_hardware(a: RankedDeal, b: RankedDeal) -> bool:
    return (
        canonical_gpu(a.gpu) == canonical_gpu(b.gpu)
        and a.ram_gb == b.ram_gb
        and str(a.screen_resolution or "").lower() == str(b.screen_resolution or "").lower()
    )


def _shares_code(a: set[str], b: set[str]) -> bool:
    return any(x == y or x in y or y in x for x in a for y in b)


def _offer_key(deal: RankedDeal) -> tuple[str, str]:
    return (str(deal.store), str((deal.offer or {}).get("external_id") or ""))


def drop_duplicate_single_store(
    deals: Sequence[RankedDeal],
    single_offer_keys: set[tuple[str, str]],
    exclusions: list[dict[str, Any]] | None = None,
) -> list[RankedDeal]:
    """
    A single-store deal and another deal are the same laptop when they share a
    model code and have the same GPU, RAM, and screen. Only the cheaper one
    stays; on a tie the multi-store cluster stays.
    """
    from laptop_eligibility import DUPLICATE_MODEL

    codes = {id(d): _model_codes(d) for d in deals}
    removed: set[int] = set()

    def _drop(loser: RankedDeal, winner: RankedDeal) -> None:
        removed.add(id(loser))
        if exclusions is not None:
            exclusions.append(
                {
                    "cluster_name": loser.cluster_name,
                    "reason": DUPLICATE_MODEL,
                    "details": [f"duplicate_of:{winner.cluster_name}"],
                    "price": loser.price,
                    "store": loser.store,
                    "gpu": loser.gpu,
                    "ram_gb": loser.ram_gb,
                    "screen_resolution": loser.screen_resolution,
                    "screen_inch": loser.screen_inch,
                    "offers": [],
                }
            )

    for deal in deals:
        if id(deal) in removed or _offer_key(deal) not in single_offer_keys:
            continue
        twins = [
            other
            for other in deals
            if other is not deal
            and id(other) not in removed
            and _shares_code(codes[id(deal)], codes[id(other)])
            and _same_hardware(deal, other)
        ]
        if not twins:
            continue
        cheapest = min(
            twins,
            key=lambda d: (d.price or 10**12, _offer_key(d) in single_offer_keys),
        )
        if (deal.price or 10**12) < (cheapest.price or 10**12):
            for twin in twins:
                _drop(twin, deal)
        else:
            _drop(deal, cheapest)
    return [d for d in deals if id(d) not in removed]


def load_russian_ranked_deals(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    limit: int | None = None,
    specs_path: Path | str | None = None,
    hard_filters: bool = True,
    exclusions: list[dict[str, Any]] | None = None,
    include_single_store: bool = True,
) -> list[RankedDeal]:
    """
    Fresh ranked deals for every user-facing selection.

    TOP, recommendation, BUY, and model menus all read this one list, so a
    laptop that fails laptop_eligibility is absent from all of them.
    Models sold by one store are ranked too (no cross-store saving for them).
    limit=0 returns every eligible deal.
    """
    latest = get_latest_store_runs_readonly(db_path)
    fresh = get_fresh_store_slugs(latest)
    if not fresh:
        return []
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    comparison = match_products(products, identifiers)
    singles = single_store_matches(comparison.unmatched) if include_single_store else []
    matches = list(comparison.matches) + singles
    cache = load_specs_cache(specs_path) if specs_path is not None else load_specs_cache()
    specs_by_key: dict[tuple[str, str], Any] = {}
    for p in products:
        identity = identity_from_cache(cache, str(p["store"]), str(p["external_id"]))
        if identity is not None:
            specs_by_key[(str(p["store"]), str(p["external_id"]))] = identity
    pids = collect_match_product_ids(matches)
    histories = get_price_history_for_products_readonly(db_path, pids)
    ranked = rank_clusters(
        matches,
        specs_by_key=specs_by_key,
        limit=None,
        fresh_stores=fresh,
        historical_mins=historical_mins_from_rows(histories),
        history_starts=history_starts_from_rows(histories),
        hard_filters=hard_filters,
        exclusions=exclusions,
    )
    if singles:
        ranked = drop_duplicate_single_store(
            ranked,
            {(o.store, str(o.external_id)) for m in singles for o in m.offers},
            exclusions,
        )
    resolved = int(limit if limit is not None else config.TOP_DEALS_LIMIT)
    return ranked[:resolved] if resolved > 0 else ranked
