from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from product_identity import ProductIdentity, find_config_conflicts, normalize_identifier


def normalize_sku(sku: str | None) -> str | None:
    """
    Консервативная нормализация manufacturer SKU.

    Делает:
    - strip;
    - uppercase;
    - удаление пробельных символов.

    Не делает:
    - обрезку частей SKU;
    - сопоставление по подстроке;
    - удаление дефисов/точек (они часто значимы в артикулах).
    """
    if sku is None:
        return None
    if not isinstance(sku, str):
        sku = str(sku)

    normalized = sku.strip().upper()
    normalized = re.sub(r"\s+", "", normalized)
    return normalized or None


STRONG_IDENTIFIER_TYPES = frozenset(
    {
        "sku",
        "manufacturer_part_number",
        "alternative_part_number",
    }
)


@dataclass(frozen=True)
class Offer:
    store: str
    external_id: str
    name: str
    sku: str | None
    price: int | None
    available: bool
    url: str | None
    product_id: int | None = None


@dataclass
class ProductMatch:
    normalized_sku: str
    display_sku: str
    name: str
    offers: list[Offer] = field(default_factory=list)
    min_price: int | None = None
    max_price: int | None = None
    price_difference: int | None = None
    cheapest_product: Offer | None = None
    cheapest_available: Offer | None = None
    match_method: str = "sku"
    matched_identifier: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.display_sku,
            "normalized_sku": self.normalized_sku,
            "name": self.name,
            "match_method": self.match_method,
            "matched_identifier": self.matched_identifier,
            "offers": [asdict(offer) for offer in self.offers],
            "min_price": self.min_price,
            "max_price": self.max_price,
            "price_difference": self.price_difference,
            "cheapest_store": (
                self.cheapest_product.store if self.cheapest_product else None
            ),
            "cheapest_price": (
                self.cheapest_product.price if self.cheapest_product else None
            ),
            "cheapest_available_store": (
                self.cheapest_available.store if self.cheapest_available else None
            ),
            "cheapest_available_price": (
                self.cheapest_available.price if self.cheapest_available else None
            ),
        }


@dataclass
class ComparisonResult:
    matches: list[ProductMatch]
    unmatched: list[Offer]
    stats: dict[str, int]
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _as_offer(row: Mapping[str, Any]) -> Offer:
    product_id = row.get("id")
    if product_id is None:
        product_id = row.get("product_id")
    return Offer(
        store=str(row["store"]),
        external_id=str(row["external_id"]),
        name=str(row["name"]),
        sku=row.get("sku"),
        price=row.get("price"),
        available=bool(row.get("available")),
        url=row.get("url"),
        product_id=int(product_id) if product_id is not None else None,
    )


def _pick_display_name(offers: Sequence[Offer]) -> str:
    return sorted((o.name for o in offers), key=lambda n: (len(n), n))[0]


def _pick_display_sku(offers: Sequence[Offer], normalized: str) -> str:
    for offer in offers:
        if offer.sku:
            return offer.sku
    return normalized


def _priced_offers(offers: Sequence[Offer]) -> list[Offer]:
    return [o for o in offers if o.price is not None]


def _build_match(
    normalized: str,
    offers: Sequence[Offer],
    *,
    match_method: str,
    matched_identifier: str | None,
) -> ProductMatch:
    priced = _priced_offers(offers)
    available_priced = [o for o in priced if o.available]

    prices = [o.price for o in priced if o.price is not None]
    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None
    price_difference = (
        int(max_price - min_price)
        if min_price is not None and max_price is not None
        else None
    )

    cheapest = min(priced, key=lambda o: (o.price or 0, o.store)) if priced else None
    cheapest_available = (
        min(available_priced, key=lambda o: (o.price or 0, o.store))
        if available_priced
        else None
    )

    ordered = sorted(offers, key=lambda o: o.store)
    return ProductMatch(
        normalized_sku=normalized,
        display_sku=_pick_display_sku(ordered, normalized),
        name=_pick_display_name(ordered),
        offers=list(ordered),
        min_price=min_price,
        max_price=max_price,
        price_difference=price_difference,
        cheapest_product=cheapest,
        cheapest_available=cheapest_available,
        match_method=match_method,
        matched_identifier=matched_identifier,
    )


def _offer_key(offer: Offer) -> tuple[str, str]:
    return (offer.store, offer.external_id)


def _specs_for_offer(
    offer: Offer,
    specs_by_product_id: Mapping[int, ProductIdentity] | None,
    specs_by_key: Mapping[tuple[str, str], ProductIdentity] | None,
) -> ProductIdentity | None:
    if specs_by_product_id and offer.product_id is not None:
        found = specs_by_product_id.get(offer.product_id)
        if found is not None:
            return found
    if specs_by_key:
        return specs_by_key.get(_offer_key(offer))
    return None


def _level1_sku_matches(
    offers: Sequence[Offer],
) -> tuple[list[ProductMatch], set[tuple[str, str]], list[Offer]]:
    by_sku: dict[str, list[Offer]] = {}
    no_sku: list[Offer] = []

    for offer in offers:
        key = normalize_sku(offer.sku)
        if key is None:
            no_sku.append(offer)
            continue
        by_sku.setdefault(key, []).append(offer)

    matches: list[ProductMatch] = []
    matched_keys: set[tuple[str, str]] = set()
    unmatched: list[Offer] = list(no_sku)

    for normalized, group in by_sku.items():
        stores = {o.store for o in group}
        if len(stores) >= 2:
            # One-to-one: more than one offer per store for same SKU -> AMBIGUOUS
            # handled by caller via checking counts; here we still require
            # exactly one per store for a clean SKU match.
            by_store: dict[str, list[Offer]] = {}
            for offer in group:
                by_store.setdefault(offer.store, []).append(offer)
            if any(len(v) != 1 for v in by_store.values()):
                unmatched.extend(group)
                continue
            match = _build_match(
                normalized,
                group,
                match_method="sku",
                matched_identifier=normalized,
            )
            matches.append(match)
            for offer in group:
                matched_keys.add(_offer_key(offer))
        else:
            unmatched.extend(group)

    return matches, matched_keys, unmatched


def _index_strong_identifiers(
    identifiers: Sequence[Mapping[str, Any]],
    offers_by_product_id: Mapping[int, Offer],
    matched_keys: set[tuple[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for row in identifiers:
        id_type = str(row.get("identifier_type") or "")
        if id_type not in STRONG_IDENTIFIER_TYPES:
            continue
        normalized = normalize_identifier(str(row.get("normalized_value") or row.get("value") or ""))
        if not normalized:
            continue
        product_id = row.get("product_id")
        if product_id is None:
            continue
        product_id = int(product_id)
        offer = offers_by_product_id.get(product_id)
        if offer is None:
            store = row.get("store")
            external_id = row.get("external_id")
            if store is None or external_id is None:
                continue
            # Offer may only be keyed by id; skip if already matched via store/ext
            key = (str(store), str(external_id))
            if key in matched_keys:
                continue
            # Without offer object we cannot build a match later.
            continue
        if _offer_key(offer) in matched_keys:
            continue
        index.setdefault(normalized, []).append(
            {
                "product_id": product_id,
                "store": offer.store,
                "external_id": offer.external_id,
                "identifier_type": id_type,
                "value": row.get("value"),
                "normalized_value": normalized,
                "offer": offer,
            }
        )
    return index


def _level2_strong_identifier_matches(
    remaining: Sequence[Offer],
    identifiers: Sequence[Mapping[str, Any]],
    *,
    specs_by_product_id: Mapping[int, ProductIdentity] | None = None,
    specs_by_key: Mapping[tuple[str, str], ProductIdentity] | None = None,
) -> tuple[list[ProductMatch], list[Offer], list[dict[str, Any]], list[dict[str, Any]]]:
    offers_by_product_id = {
        o.product_id: o for o in remaining if o.product_id is not None
    }
    remaining_keys = {_offer_key(o) for o in remaining}
    # Filter identifiers to remaining products only via offer map + matched empty
    index = _index_strong_identifiers(identifiers, offers_by_product_id, set())

    ambiguous: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    # pair_key frozenset({regard_key, andpro_key}) -> set of matched normalized values
    pair_identifiers: dict[frozenset[tuple[str, str]], set[str]] = {}
    pair_offers: dict[frozenset[tuple[str, str]], dict[str, Offer]] = {}

    for normalized, entries in index.items():
        # Keep only remaining offers
        entries = [
            e for e in entries if _offer_key(e["offer"]) in remaining_keys
        ]
        if not entries:
            continue

        by_store: dict[str, dict[int, dict[str, Any]]] = {}
        for entry in entries:
            store = entry["store"]
            pid = entry["product_id"]
            by_store.setdefault(store, {})
            by_store[store][pid] = entry

        if len(by_store) < 2:
            continue

        multi = {
            store: list(products.values())
            for store, products in by_store.items()
            if len(products) > 1
        }
        if multi:
            ambiguous.append(
                {
                    "status": "AMBIGUOUS",
                    "reason": (
                        "Один strong identifier соответствует нескольким товарам "
                        "одного магазина"
                    ),
                    "matched_identifier": normalized,
                    "by_store": {
                        store: [
                            {
                                "external_id": e["external_id"],
                                "sku": e["offer"].sku,
                                "identifier_type": e["identifier_type"],
                            }
                            for e in items
                        ]
                        for store, items in multi.items()
                    },
                }
            )
            continue

        # Exactly one product per participating store; need ≥2 stores.
        if len(by_store) < 2:
            continue
        if any(len(v) != 1 for v in by_store.values()):
            continue

        offers_map: dict[str, Offer] = {
            store: next(iter(entries.values()))["offer"]
            for store, entries in by_store.items()
        }
        cluster_key = frozenset(_offer_key(o) for o in offers_map.values())
        pair_identifiers.setdefault(cluster_key, set()).add(normalized)
        pair_offers[cluster_key] = offers_map

    # One-to-one: each product may belong to at most one counterpart cluster.
    product_to_pairs: dict[tuple[str, str], list[frozenset[tuple[str, str]]]] = {}
    for pair_key in pair_offers:
        for key in pair_key:
            product_to_pairs.setdefault(key, []).append(pair_key)

    blocked_pairs: set[frozenset[tuple[str, str]]] = set()
    for key, pairs in product_to_pairs.items():
        unique_pairs = list({p for p in pairs})
        if len(unique_pairs) > 1:
            ambiguous.append(
                {
                    "status": "AMBIGUOUS",
                    "reason": (
                        "Один товар имеет несколько возможных counterparts "
                        "по strong identifiers"
                    ),
                    "product": {"store": key[0], "external_id": key[1]},
                    "candidate_pairs": [
                        {
                            "offers": [
                                {"store": s, "external_id": e} for s, e in sorted(p)
                            ],
                            "identifiers": sorted(pair_identifiers[p]),
                        }
                        for p in unique_pairs
                    ],
                }
            )
            blocked_pairs.update(unique_pairs)

    matches: list[ProductMatch] = []
    matched_now: set[tuple[str, str]] = set()

    for cluster_key, offers_map in pair_offers.items():
        if cluster_key in blocked_pairs:
            continue
        offer_list = list(offers_map.values())
        identifiers_hit = sorted(pair_identifiers[cluster_key])
        matched_id = identifiers_hit[0]

        # Pairwise config check across all offers that have specs.
        specs = [
            (_offer_key(o), o, _specs_for_offer(o, specs_by_product_id, specs_by_key))
            for o in offer_list
        ]
        conflicted = False
        for i in range(len(specs)):
            for j in range(i + 1, len(specs)):
                left_spec = specs[i][2]
                right_spec = specs[j][2]
                if left_spec is None or right_spec is None:
                    continue
                diffs = find_config_conflicts(left_spec, right_spec)
                if diffs:
                    conflicts.append(
                        {
                            "status": "CONFLICT",
                            "reason": (
                                "Strong identifier совпал, но конфигурация "
                                "противоречит"
                            ),
                            "matched_identifier": matched_id,
                            "different_fields": diffs,
                            "offers": [
                                {
                                    "store": specs[i][1].store,
                                    "external_id": specs[i][1].external_id,
                                    "sku": specs[i][1].sku,
                                    "name": specs[i][1].name,
                                },
                                {
                                    "store": specs[j][1].store,
                                    "external_id": specs[j][1].external_id,
                                    "sku": specs[j][1].sku,
                                    "name": specs[j][1].name,
                                },
                            ],
                        }
                    )
                    conflicted = True
                    break
            if conflicted:
                break
        if conflicted:
            continue

        display_normalized = (
            normalize_sku(offer_list[0].sku) or matched_id
        )
        match = _build_match(
            display_normalized,
            offer_list,
            match_method="strong_identifier",
            matched_identifier=matched_id,
        )
        matches.append(match)
        for o in offer_list:
            matched_now.add(_offer_key(o))

    still_unmatched = [o for o in remaining if _offer_key(o) not in matched_now]
    return matches, still_unmatched, ambiguous, conflicts


def match_products(
    products: Iterable[Mapping[str, Any]],
    identifiers: Sequence[Mapping[str, Any]] | None = None,
    *,
    specs_by_product_id: Mapping[int, ProductIdentity] | None = None,
    specs_by_key: Mapping[tuple[str, str], ProductIdentity] | None = None,
) -> ComparisonResult:
    """
    Сопоставление товаров:

    LEVEL 1 — exact/normalized SKU across stores (match_method=sku)
    LEVEL 2 — strong identifier overlap across stores
              (match_method=strong_identifier)

    Strong types allowlist:
      sku, manufacturer_part_number, alternative_part_number

    Не требует одинаковый identifier_type.
    При коллизиях — AMBIGUOUS (без auto-match).
    При strong id + явном config contradiction — CONFLICT.
    """
    rows = list(products)
    offers = [_as_offer(row) for row in rows]
    store_skus: dict[str, set[str]] = {}
    for offer in offers:
        key = normalize_sku(offer.sku)
        if key:
            store_skus.setdefault(offer.store, set()).add(key)

    sku_matches, matched_keys, remaining = _level1_sku_matches(offers)

    strong_matches: list[ProductMatch] = []
    ambiguous: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    if identifiers:
        strong_matches, remaining, ambiguous, conflicts = _level2_strong_identifier_matches(
            remaining,
            identifiers,
            specs_by_product_id=specs_by_product_id,
            specs_by_key=specs_by_key,
        )
        # Also surface L1 multi-offer SKU collisions as AMBIGUOUS if needed —
        # currently L1 leaves them unmatched silently; keep conservative.

    matches = sku_matches + strong_matches
    matches.sort(
        key=lambda m: (
            -(m.price_difference if m.price_difference is not None else -1),
            m.display_sku,
        )
    )
    remaining.sort(key=lambda o: (o.store, o.sku or "", o.external_id))

    stats = {
        "total_products": len(rows),
        "matched_models": len(matches),
        "sku_matches": len(sku_matches),
        "strong_identifier_matches": len(strong_matches),
        "unmatched_products": len(remaining),
        "ambiguous": len(ambiguous),
        "conflicts": len(conflicts),
        "sku_regard": len(store_skus.get("regard", set())),
        "sku_andpro": len(store_skus.get("andpro", set())),
        "matched_offers": sum(len(m.offers) for m in matches),
    }
    return ComparisonResult(
        matches=matches,
        unmatched=remaining,
        stats=stats,
        ambiguous=ambiguous,
        conflicts=conflicts,
    )


def analyze_sku_overlap(products: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Диагностика пересечения SKU между магазинами."""
    raw_by_store: dict[str, set[str]] = {}
    norm_by_store: dict[str, set[str]] = {}

    for row in products:
        store = str(row["store"])
        sku = row.get("sku")
        if not sku:
            continue
        raw_by_store.setdefault(store, set()).add(str(sku))
        key = normalize_sku(str(sku))
        if key:
            norm_by_store.setdefault(store, set()).add(key)

    regard_raw = raw_by_store.get("regard", set())
    andpro_raw = raw_by_store.get("andpro", set())
    regard_norm = norm_by_store.get("regard", set())
    andpro_norm = norm_by_store.get("andpro", set())

    return {
        "regard_sku_count": len(regard_raw),
        "andpro_sku_count": len(andpro_raw),
        "exact_matches": len(regard_raw & andpro_raw),
        "normalized_matches": len(regard_norm & andpro_norm),
        "exact_sku_list": sorted(regard_raw & andpro_raw),
        "normalized_sku_list": sorted(regard_norm & andpro_norm),
    }
