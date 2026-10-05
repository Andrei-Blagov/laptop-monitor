from __future__ import annotations

"""Match one Russian model against offers already collected in this Thailand job.

Search text is never GPU evidence. Identifiers come from the offer itself.
"""

from typing import Any, Sequence

from deal_ranking import RankedDeal, canonical_gpu
from thailand.fx import thb_to_rub
from thailand.grouping import normalize_model_code
from thailand.matching import configs_equivalent
from thailand.models import FxRate, ThailandOffer
from thailand.specs_parse import extract_brand, extract_mpn_candidates

EXACT = "EXACT"
SAME_FAMILY = "SAME_FAMILY"
EQUIVALENT = "EQUIVALENT"
NOT_FOUND = "NOT_FOUND"

# Advice has only generic keyword searches, not a public exact-model API.
ADVICE_EXACT_SEARCH = False


def family_stem(code: str | None) -> str | None:
    norm = normalize_model_code(code)
    if not norm or "-" not in norm:
        return None
    stem = norm.split("-", 1)[0]
    if len(stem) < 5:
        return None
    if not any(ch.isalpha() for ch in stem) or not any(ch.isdigit() for ch in stem):
        return None
    return stem


def _codes_from_texts(*values: str | None) -> list[str]:
    found: list[str] = []
    for value in values:
        if not value:
            continue
        norm = normalize_model_code(value)
        if len(norm) >= 6 and norm not in found:
            found.append(norm)
        for cand in extract_mpn_candidates(str(value)):
            norm = normalize_model_code(cand)
            if len(norm) >= 6 and norm not in found:
                found.append(norm)
    return found


def build_target_model(deal: RankedDeal, *, level: str | None = None) -> dict[str, Any]:
    """Immutable snapshot. Worker must not re-read this model from the live DB."""
    offer = deal.offer or {}
    sku = offer.get("sku") or offer.get("article")
    mpn = offer.get("mpn") or offer.get("part_number") or offer.get("manufacturer_part_number")
    codes = _codes_from_texts(
        str(sku) if sku else None,
        str(mpn) if mpn else None,
        deal.cluster_name,
    )
    primary = next((c for c in codes if "-" in c), codes[0] if codes else None)
    return {
        "cluster_key": f"{deal.store}:{offer.get('external_id') or ''}:{deal.cluster_name}",
        "cluster_name": deal.cluster_name,
        "brand": extract_brand(deal.cluster_name or ""),
        "model": deal.cluster_name,
        "canonical_model_code": primary,
        "model_codes": codes,
        "family_stem": family_stem(primary) if primary else None,
        "sku": sku,
        "mpn": mpn or primary,
        "alternative_part_numbers": [c for c in codes if c != primary],
        "gpu": deal.gpu,
        "cpu": deal.cpu,
        "ram_gb": deal.ram_gb,
        "ssd_gb": deal.ssd_gb,
        "screen_size_inch": deal.screen_inch,
        "screen_resolution": deal.screen_resolution,
        "store": deal.store,
        "price": deal.price,
        "url": deal.url,
        "score": deal.score,
        "confidence": deal.confidence,
        "level": level,
    }


def offer_codes(offer: ThailandOffer) -> list[str]:
    return _codes_from_texts(offer.manufacturer_part_number, offer.sku, offer.name)


def match_level(target: dict[str, Any], offer: ThailandOffer) -> str | None:
    """EXACT, SAME_FAMILY, EQUIVALENT, or None. Never copies the query GPU onto the offer."""
    target_codes = [normalize_model_code(c) for c in (target.get("model_codes") or []) if c]
    full_codes = [c for c in target_codes if "-" in c and len(c) >= 6]
    exact_codes = full_codes or [c for c in target_codes if len(c) >= 6]
    found = [c for c in offer_codes(offer) if "-" in c] or offer_codes(offer)
    if exact_codes and found and any(code in found for code in exact_codes):
        return EXACT
    stem = target.get("family_stem") or family_stem(target.get("canonical_model_code"))
    if stem:
        for code in found:
            if family_stem(code) == stem and code not in target_codes:
                return SAME_FAMILY
    same, _diffs = configs_equivalent(_target_view(target), offer)
    if same:
        return EQUIVALENT
    return None


class _target_view:
    def __init__(self, target: dict[str, Any]) -> None:
        self.gpu = target.get("gpu")
        self.cpu = target.get("cpu")
        self.ram_gb = target.get("ram_gb")
        self.ssd_gb = target.get("ssd_gb")
        self.screen_size_inch = target.get("screen_size_inch")
        self.screen_resolution = target.get("screen_resolution")


def _row(offer: ThailandOffer, level: str, *, fx: FxRate | None, cap: int) -> dict[str, Any]:
    rub = thb_to_rub(offer.price_thb, fx) if fx is not None else None
    purchasable = offer.availability_status in {"in_stock", "online_available", "store_pickup_only"} and offer.available
    return {
        "match_level": level,
        "store": offer.store,
        "name": offer.name,
        "sku": offer.sku,
        "manufacturer_part_number": offer.manufacturer_part_number,
        "gpu": offer.gpu,
        "cpu": offer.cpu,
        "ram_gb": offer.ram_gb,
        "ssd_gb": offer.ssd_gb,
        "screen_size_inch": offer.screen_size_inch,
        "price_thb": offer.price_thb,
        "price_rub": rub,
        "availability_status": offer.availability_status,
        "url": offer.url,
        "over_cap": rub is not None and rub > cap,
        "out_of_stock": not purchasable,
        "gpu_source": offer.gpu_source,
    }


def search_collected_offers(
    target: dict[str, Any],
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None = None,
    cap_rub: int = 300_000,
    stores_skipped: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {EXACT: [], SAME_FAMILY: [], EQUIVALENT: []}
    seen: set[tuple[str, str]] = set()
    for offer in offers:
        level = match_level(target, offer)
        if level is None:
            continue
        key = (offer.store, offer.external_id)
        if key in seen:
            continue
        seen.add(key)
        grouped[level].append(_row(offer, level, fx=fx, cap=cap_rub))

    def _sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: (
                1 if row.get("out_of_stock") else 0,
                row.get("price_rub") if row.get("price_rub") is not None else 10**12,
            ),
        )

    exact = _sort(grouped[EXACT])
    family = _sort(grouped[SAME_FAMILY])
    equivalent = _sort(grouped[EQUIVALENT])
    if exact:
        best = EXACT
    elif family:
        best = SAME_FAMILY
    elif equivalent:
        best = EQUIVALENT
    else:
        best = NOT_FOUND
    checked = sorted({o.store for o in offers})
    return {
        "match_level": best,
        "exact_matches": exact,
        "same_family_matches": family,
        "equivalent_matches": equivalent,
        "stores_checked": checked,
        "stores_skipped": list(stores_skipped or []),
        "over_cap": any(row.get("over_cap") for row in exact + family + equivalent),
        "out_of_stock": any(row.get("out_of_stock") for row in exact),
        "canonical_gpu_ignored_as_evidence": canonical_gpu(target.get("gpu")),
    }
