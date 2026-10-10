from __future__ import annotations

"""Load current fresh Russian ranked deals (read-only). Shared by pipeline + control bot."""

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

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
from product_identity import normalize_identifier
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
_CRITICAL_SPECS = ("cpu", "gpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")


def single_store_matches(unmatched: Sequence[Offer]) -> list[ProductMatch]:
    """
    One cluster per product that matched no other store.

    The same SKU listed twice by one store becomes one cluster. Offers from
    different stores stay apart here. They are joined later only when the
    normalized identifier and the full configuration are proven equal.
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


def _code_token(value: str | None) -> str | None:
    """One complete identifier. A shorter code inside a longer one does not count."""
    norm = normalize_identifier(value)
    if not norm or not _CODE_RE.fullmatch(norm):
        return None
    if len(norm) < 8 or not any(c.isalpha() for c in norm) or not any(c.isdigit() for c in norm):
        return None
    return norm


def _exact_codes_in_text(text: str) -> set[str]:
    """Whole codes only. A token glued to a longer SKU by '_' or a letter is not a match."""
    found: set[str] = set()
    upper = str(text).upper()
    for match in _CODE_RE.finditer(upper):
        start = match.start()
        if start > 0 and (upper[start - 1].isalnum() or upper[start - 1] == "_"):
            continue
        token = _code_token(match.group(0))
        if token:
            found.add(token)
    return found


def _exact_identifiers(match: ProductMatch) -> set[str]:
    found: set[str] = set()
    wholes = [match.normalized_sku, match.display_sku, match.matched_identifier]
    texts = [match.name]
    for offer in match.offers:
        wholes.append(offer.sku)
        texts.append(offer.name)
    for value in wholes:
        token = _code_token(value)
        if token:
            found.add(token)
    for text in texts:
        if text:
            found |= _exact_codes_in_text(str(text))
    return found


def _identity_specs(
    match: ProductMatch,
    specs_by_key: Mapping[tuple[str, str], Any] | None,
) -> dict[str, Any]:
    """
    Configuration proven for every offer in the match.

    A value known on only some offers is not borrowed for the rest: the field
    stays unknown, so the match cannot be joined to another listing.
    """
    per_offer = [_spec_values(_offer_specs(offer, specs_by_key)) for offer in match.offers]
    if not per_offer:
        per_offer = [{field: None for field in _CRITICAL_SPECS}]
    proven: dict[str, Any] = {}
    for field in _CRITICAL_SPECS:
        values = [row[field] for row in per_offer]
        known = [value for value in values if value is not None]
        if len(known) != len(values) or any(value != known[0] for value in known):
            proven[field] = None
        else:
            proven[field] = known[0]
    return proven


def _norm_cpu(value: Any) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).upper().replace("Ё", "Е").split())
    for prefix in ("INTEL ", "AMD "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text or None


def _norm_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm_inch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _norm_resolution(value: Any) -> str | None:
    from laptop_eligibility import parse_resolution

    parsed = parse_resolution(value)
    if parsed is None:
        return None
    return f"{parsed[0]}x{parsed[1]}"


def _norm_gpu(value: Any) -> str | None:
    canonical = canonical_gpu(None if value is None else str(value))
    if canonical:
        return canonical
    if not value:
        return None
    text = " ".join(str(value).upper().split())
    return text or None


def _spec_values(specs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cpu": _norm_cpu(specs.get("cpu")),
        "gpu": _norm_gpu(specs.get("gpu")),
        "ram_gb": _norm_int(specs.get("ram_gb")),
        "ssd_gb": _norm_int(specs.get("ssd_gb")),
        "screen_inch": _norm_inch(specs.get("screen_inch")),
        "screen_resolution": _norm_resolution(specs.get("screen_resolution")),
    }


def _hardware_key(specs: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Complete configuration. Any missing critical field means identity is unproven."""
    values = _spec_values(specs)
    if any(values[field] is None for field in _CRITICAL_SPECS):
        return None
    return tuple(values[field] for field in _CRITICAL_SPECS)


def _explicit_differences(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    a = _spec_values(left)
    b = _spec_values(right)
    return [
        field
        for field in _CRITICAL_SPECS
        if a[field] is not None and b[field] is not None and a[field] != b[field]
    ]


def _missing_fields(specs: Mapping[str, Any]) -> list[str]:
    values = _spec_values(specs)
    return [field for field in _CRITICAL_SPECS if values[field] is None]


def _offer_specs(offer: Offer, specs_by_key: Mapping[tuple[str, str], Any] | None) -> dict[str, Any]:
    from stores.common import extract_specs_from_name

    out: dict[str, Any] = {}
    spec = (specs_by_key or {}).get((offer.store, str(offer.external_id)))
    if spec is not None:
        out["gpu"] = getattr(spec, "gpu", None) or getattr(spec, "normalized_gpu", None)
        out["ram_gb"] = getattr(spec, "ram_gb", None)
        out["ssd_gb"] = getattr(spec, "ssd_gb", None)
        out["screen_inch"] = getattr(spec, "screen_inch", None) or getattr(
            spec, "screen_size_inch", None
        )
        out["cpu"] = getattr(spec, "cpu", None)
        out["screen_resolution"] = getattr(spec, "screen_resolution", None)
    inferred = extract_specs_from_name(" ".join(x for x in (offer.name, offer.url) if x))
    for field in _CRITICAL_SPECS:
        if out.get(field) is None:
            out[field] = inferred.get(field)
    return out


def _match_from_offers(source: ProductMatch, offers: Sequence[Offer]) -> ProductMatch:
    kept = list(offers)
    return ProductMatch(
        normalized_sku=source.normalized_sku,
        display_sku=source.display_sku,
        name=source.name,
        offers=kept,
        match_method=source.match_method,
        matched_identifier=source.matched_identifier,
    )


def _match_summary(match: ProductMatch) -> dict[str, Any]:
    prices = [o.price for o in match.offers if o.price is not None]
    return {
        "name": match.name,
        "sku": match.display_sku,
        "matched_identifier": match.matched_identifier,
        "stores": sorted({o.store for o in match.offers}),
        "offers": [
            {"store": o.store, "external_id": o.external_id, "sku": o.sku, "price": o.price}
            for o in match.offers
        ],
        "min_price": min(prices) if prices else None,
    }


def split_conflicting_configs(
    matches: Sequence[ProductMatch],
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
) -> list[ProductMatch]:
    """
    Keep an exact-SKU cluster together unless two of its offers state different
    CPU, GPU, RAM, SSD, diagonal, or resolution. A missing field is not a
    conflict. Offers that contradict the rest leave the cluster.
    """
    out: list[ProductMatch] = []
    for match in matches:
        offers = list(match.offers)
        if len(offers) < 2:
            out.append(match)
            continue
        specs = [_offer_specs(offer, specs_by_key) for offer in offers]
        conflicts = [False] * len(offers)
        for i in range(len(offers)):
            for j in range(i + 1, len(offers)):
                if _explicit_differences(specs[i], specs[j]):
                    conflicts[i] = True
                    conflicts[j] = True
        if not any(conflicts):
            out.append(match)
            continue
        clean = [offers[i] for i, bad in enumerate(conflicts) if not bad]
        dirty = [offers[i] for i, bad in enumerate(conflicts) if bad]
        if clean:
            out.append(_match_from_offers(match, clean))
        for offer in dirty:
            key = normalize_sku(offer.sku) or f"id:{offer.external_id}"
            out.append(
                ProductMatch(
                    normalized_sku=key,
                    display_sku=str(offer.sku or offer.external_id),
                    name=offer.name,
                    offers=[offer],
                    match_method=SINGLE_STORE_METHOD,
                    matched_identifier=key,
                )
            )
    return out


def fold_proven_duplicates(
    matches: Sequence[ProductMatch],
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
    *,
    merges: list[dict[str, Any]] | None = None,
    ambiguous: list[dict[str, Any]] | None = None,
) -> list[ProductMatch]:
    """
    Join separate listings only when an exact normalized identifier matches and
    CPU, GPU, RAM, SSD, diagonal, and resolution are all present and equal.

    Proven joins keep every offer, so later ranking still sees each store,
    price, and history row. Substring codes and incomplete specs stay apart.
    """
    items = list(matches)
    specs = [_identity_specs(match, specs_by_key) for match in items]
    identifiers = [_exact_identifiers(match) for match in items]
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            shared = identifiers[i] & identifiers[j]
            if not shared:
                continue
            left_key = _hardware_key(specs[i])
            right_key = _hardware_key(specs[j])
            if left_key is not None and left_key == right_key:
                union(i, j)
                continue
            if ambiguous is not None:
                ambiguous.append(
                    {
                        "status": "AMBIGUOUS",
                        "reason": "identity_not_proven",
                        "shared_identifiers": sorted(shared),
                        "missing": {
                            "left": _missing_fields(specs[i]),
                            "right": _missing_fields(specs[j]),
                        },
                        "different": _explicit_differences(specs[i], specs[j]),
                        "left": _match_summary(items[i]),
                        "right": _match_summary(items[j]),
                    }
                )

    groups: dict[int, list[int]] = {}
    for index in range(len(items)):
        groups.setdefault(find(index), []).append(index)

    folded: list[ProductMatch] = []
    for indexes in groups.values():
        if len(indexes) == 1:
            folded.append(items[indexes[0]])
            continue
        shared_ids = set.intersection(*(identifiers[index] for index in indexes))
        if not shared_ids:
            if ambiguous is not None:
                ambiguous.append(
                    {
                        "status": "AMBIGUOUS",
                        "reason": "no_shared_identifier_across_group",
                        "shared_identifiers": [],
                        "names": [items[index].name for index in indexes],
                    }
                )
            for index in indexes:
                folded.append(items[index])
            continue
        seen: set[tuple[str, str]] = set()
        offers: list[Offer] = []
        for index in indexes:
            for offer in items[index].offers:
                key = (offer.store, str(offer.external_id))
                if key in seen:
                    continue
                seen.add(key)
                offers.append(offer)
        primary = max(
            indexes,
            key=lambda index: (
                len({offer.store for offer in items[index].offers}),
                -min((offer.price for offer in items[index].offers if offer.price is not None), default=10**12),
            ),
        )
        base = items[primary]
        combined = ProductMatch(
            normalized_sku=base.normalized_sku,
            display_sku=base.display_sku,
            name=base.name,
            offers=offers,
            match_method=base.match_method if base.match_method != SINGLE_STORE_METHOD else "proven_identifier",
            matched_identifier=sorted(shared_ids)[0] if shared_ids else base.matched_identifier,
        )
        folded.append(combined)
        if merges is not None:
            merges.append(
                {
                    "status": "MERGED",
                    "shared_identifiers": sorted(shared_ids),
                    "name": combined.name,
                    "offers": _match_summary(combined)["offers"],
                    "stores": sorted({offer.store for offer in offers}),
                }
            )
    return folded


def load_russian_ranked_deals(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    limit: int | None = None,
    specs_path: Path | str | None = None,
    hard_filters: bool = True,
    exclusions: list[dict[str, Any]] | None = None,
    include_single_store: bool = True,
    merges: list[dict[str, Any]] | None = None,
    ambiguous: list[dict[str, Any]] | None = None,
) -> list[RankedDeal]:
    """
    Fresh ranked deals for every user-facing selection.

    TOP, recommendation, BUY, and model menus all read this one list, so a
    laptop that fails laptop_eligibility is absent from all of them.
    Models sold by one store are ranked too (no cross-store saving for them).
    Separate listings are joined only when the normalized identifier and the
    full configuration are proven equal; every store offer stays on that model.
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
    matches = split_conflicting_configs(matches, specs_by_key)
    matches = fold_proven_duplicates(
        matches, specs_by_key, merges=merges, ambiguous=ambiguous
    )
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
    resolved = int(limit if limit is not None else config.TOP_DEALS_LIMIT)
    return ranked[:resolved] if resolved > 0 else ranked
