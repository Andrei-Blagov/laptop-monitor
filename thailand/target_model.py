from __future__ import annotations

"""Match one Russian model against offers already collected in this Thailand job.

Search text is never GPU evidence. Identifiers come from the offer itself.
"""

import re
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


# Strong platform codes only. A short manufacturer prefix such as MSI "9S7" is not a family.
_STRONG_FAMILY = re.compile(
    r"^(?:G\d{3}[A-Z]{1,4}|[A-Z]{2,4}\d{2,3}[A-Z]{0,3}|[A-Z]\d{2}[A-Z])$"
)
_DENY_FAMILY_STEMS = frozenset({"9S7"})


# Regional indexes such as TS113W / RV027 are not platform families.
_REGIONAL_SUFFIX = re.compile(r"^[A-Z]{2}\d{3}[A-Z]?$")


def family_stem(code: str | None) -> str | None:
    """Return a model-platform key, or None when the code is not strong evidence."""
    norm = normalize_model_code(code)
    if not norm:
        return None
    stem = norm.split("-", 1)[0] if "-" in norm else norm
    if stem in _DENY_FAMILY_STEMS or stem[:1].isdigit():
        return None
    if _REGIONAL_SUFFIX.match(stem):
        return None
    if not _STRONG_FAMILY.match(stem):
        return None
    return stem


def _append_code(found: list[str], value: str | None, *, from_title: bool) -> None:
    """Structured SKU/MPN values are kept whole. Titles contribute extracted codes only."""
    if not value:
        return
    if not from_title:
        norm = normalize_model_code(value)
        if len(norm) >= 6 and norm not in found:
            found.append(norm)
    for cand in extract_mpn_candidates(str(value)):
        norm = normalize_model_code(cand)
        if len(norm) >= 6 and norm not in found:
            found.append(norm)


def family_keys(codes: Sequence[str]) -> set[str]:
    """Platform keys from every model-like identifier. Conflicting keys stay in the set."""
    norms = [normalize_model_code(c) for c in codes if c]
    hyphenated = [c for c in norms if "-" in c]
    suffix_pieces: set[str] = set()
    for code in hyphenated:
        suffix_pieces.update(code.split("-")[1:])
    keys: set[str] = set()
    for code in hyphenated:
        stem = family_stem(code)
        if stem:
            keys.add(stem)
    for code in norms:
        if "-" in code or code in suffix_pieces:
            continue
        stem = family_stem(code)
        if stem:
            keys.add(stem)
    return keys


def coherent_family(codes: Sequence[str]) -> str | None:
    """One agreed platform, or None when identifiers disagree or carry no platform."""
    keys = family_keys(codes)
    if len(keys) == 1:
        return next(iter(keys))
    return None


def _is_exact_identifier(code: str) -> bool:
    """Full model or part number. A bare platform token is family evidence, not EXACT."""
    if len(code) < 6 or family_stem(code) == code:
        return False
    if "-" in code:
        return True
    return (
        len(code) >= 8
        and any(ch.isdigit() for ch in code)
        and any(ch.isalpha() for ch in code)
    )


def exact_identifiers(codes: Sequence[str]) -> list[str]:
    norms = [normalize_model_code(c) for c in codes if c]
    hyphenated = [c for c in norms if "-" in c]
    out: list[str] = []
    for code in norms:
        if not _is_exact_identifier(code):
            continue
        if "-" not in code and any(
            code == piece or code in parts
            for parts in (h.split("-") for h in hyphenated)
            for piece in parts
        ):
            continue
        if code not in out:
            out.append(code)
    return out


def _spec_difference_lines(target: dict[str, Any], offer: ThailandOffer) -> list[str]:
    _same, diffs = configs_equivalent(_target_view(target), offer)
    hard = [d for d in diffs if not d.endswith("_unknown") and not d.startswith("resolution:")]
    view = _target_view(target)
    lines: list[str] = []
    for diff in hard:
        if diff == "gpu":
            lines.append(f"GPU: {view.gpu or 'н/д'} и {offer.gpu or 'н/д'}")
        elif diff == "cpu":
            lines.append(f"CPU: {view.cpu or 'н/д'} и {offer.cpu or 'н/д'}")
        elif diff.startswith("ram:"):
            lines.append(f"RAM: {view.ram_gb} ГБ и {offer.ram_gb} ГБ")
        elif diff.startswith("ssd:"):
            lines.append(f"SSD: {view.ssd_gb} ГБ и {offer.ssd_gb} ГБ")
        elif diff.startswith("screen:"):
            lines.append(f"Экран: {view.screen_size_inch}\" и {offer.screen_size_inch}\"")
    return lines


def build_target_model(deal: RankedDeal, *, level: str | None = None) -> dict[str, Any]:
    """Immutable snapshot. Worker must not re-read this model from the live DB."""
    offer = deal.offer or {}
    sku = offer.get("sku") or offer.get("article")
    mpn = offer.get("mpn") or offer.get("part_number") or offer.get("manufacturer_part_number")
    codes: list[str] = []
    _append_code(codes, str(sku) if sku else None, from_title=False)
    _append_code(codes, str(mpn) if mpn else None, from_title=False)
    _append_code(codes, deal.cluster_name, from_title=True)
    primary = next((c for c in codes if "-" in c), codes[0] if codes else None)
    keys = family_keys(codes)
    return {
        "cluster_key": f"{deal.store}:{offer.get('external_id') or ''}:{deal.cluster_name}",
        "cluster_name": deal.cluster_name,
        "brand": extract_brand(deal.cluster_name or ""),
        "model": deal.cluster_name,
        "canonical_model_code": primary,
        "model_codes": codes,
        "family_keys": sorted(keys),
        "family_stem": next(iter(keys)) if len(keys) == 1 else None,
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
    found: list[str] = []
    _append_code(found, offer.manufacturer_part_number, from_title=False)
    _append_code(found, offer.sku, from_title=False)
    _append_code(found, offer.name, from_title=True)
    meta = offer.metadata or {}
    for key in ("alternative_part_numbers", "model_codes", "mpn"):
        raw = meta.get(key)
        if isinstance(raw, str):
            _append_code(found, raw, from_title=False)
        elif isinstance(raw, list):
            for item in raw:
                _append_code(found, str(item) if item else None, from_title=False)
    return found


def _target_codes(target: dict[str, Any]) -> list[str]:
    stored = [normalize_model_code(c) for c in (target.get("model_codes") or []) if c]
    if stored:
        return stored
    found: list[str] = []
    _append_code(found, target.get("sku"), from_title=False)
    _append_code(found, target.get("mpn"), from_title=False)
    _append_code(found, target.get("canonical_model_code"), from_title=False)
    for item in target.get("alternative_part_numbers") or []:
        _append_code(found, str(item) if item else None, from_title=False)
    _append_code(found, target.get("cluster_name"), from_title=True)
    return found


def _pick_family_code(codes: Sequence[str], key: str) -> str | None:
    hyphenated = [c for c in codes if family_stem(c) == key]
    if hyphenated:
        return hyphenated[0]
    bare = [c for c in codes if normalize_model_code(c) == key]
    return bare[0] if bare else None


def match_detail(target: dict[str, Any], offer: ThailandOffer) -> dict[str, Any] | None:
    """EXACT, then SAME_FAMILY across every strong identifier, then EQUIVALENT."""
    target_codes = _target_codes(target)
    found = offer_codes(offer)
    shared = [code for code in exact_identifiers(target_codes) if code in exact_identifiers(found)]
    if shared:
        return {
            "match_level": EXACT,
            "matched_identifier": shared[0],
            "matched_offer_identifier": shared[0],
            "family_key": family_stem(shared[0]),
            "match_basis": "exact_identifier",
            "spec_differences": [],
        }
    target_family = coherent_family(target_codes)
    offer_family = coherent_family(found)
    if target_family and target_family == offer_family:
        canonical = normalize_model_code(target.get("canonical_model_code"))
        basis = (
            "canonical_model_code"
            if family_stem(canonical) == target_family
            else "alternative_model_code"
        )
        return {
            "match_level": SAME_FAMILY,
            "matched_identifier": _pick_family_code(target_codes, target_family),
            "matched_offer_identifier": _pick_family_code(found, offer_family),
            "family_key": target_family,
            "match_basis": basis,
            "spec_differences": _spec_difference_lines(target, offer),
        }
    same, _diffs = configs_equivalent(_target_view(target), offer)
    if same:
        return {
            "match_level": EQUIVALENT,
            "matched_identifier": None,
            "matched_offer_identifier": None,
            "family_key": None,
            "match_basis": "configuration",
            "spec_differences": [],
        }
    return None


def match_level(target: dict[str, Any], offer: ThailandOffer) -> str | None:
    """EXACT, SAME_FAMILY, EQUIVALENT, or None. Never copies the query GPU onto the offer."""
    detail = match_detail(target, offer)
    return str(detail["match_level"]) if detail else None


class _target_view:
    def __init__(self, target: dict[str, Any]) -> None:
        self.gpu = target.get("gpu")
        self.cpu = target.get("cpu")
        self.ram_gb = target.get("ram_gb")
        self.ssd_gb = target.get("ssd_gb")
        self.screen_size_inch = target.get("screen_size_inch")
        self.screen_resolution = target.get("screen_resolution")


def _row(
    offer: ThailandOffer,
    detail: dict[str, Any],
    *,
    fx: FxRate | None,
    cap: int,
) -> dict[str, Any]:
    rub = thb_to_rub(offer.price_thb, fx) if fx is not None else None
    purchasable = offer.availability_status in {"in_stock", "online_available", "store_pickup_only"} and offer.available
    level = str(detail.get("match_level") or "")
    return {
        "match_level": level,
        "matched_identifier": detail.get("matched_identifier"),
        "matched_offer_identifier": detail.get("matched_offer_identifier"),
        "family_key": detail.get("family_key"),
        "match_basis": detail.get("match_basis"),
        "spec_differences": list(detail.get("spec_differences") or []),
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
    cap_rub: int | None = None,
    stores_skipped: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if cap_rub is None:
        from thailand.eligibility import max_tracked_price_rub

        cap_rub = max_tracked_price_rub()
    grouped: dict[str, list[dict[str, Any]]] = {EXACT: [], SAME_FAMILY: [], EQUIVALENT: []}
    seen: set[tuple[str, str]] = set()
    for offer in offers:
        detail = match_detail(target, offer)
        if detail is None:
            continue
        level = str(detail["match_level"])
        key = (offer.store, offer.external_id)
        if key in seen:
            continue
        seen.add(key)
        grouped[level].append(_row(offer, detail, fx=fx, cap=cap_rub))

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
        winner = exact[0]
    elif family:
        best = SAME_FAMILY
        winner = family[0]
    elif equivalent:
        best = EQUIVALENT
        winner = equivalent[0]
    else:
        best = NOT_FOUND
        winner = {}
    checked = sorted({o.store for o in offers})
    return {
        "match_level": best,
        "matched_identifier": winner.get("matched_identifier"),
        "matched_offer_identifier": winner.get("matched_offer_identifier"),
        "family_key": winner.get("family_key"),
        "match_basis": winner.get("match_basis"),
        "exact_matches": exact,
        "same_family_matches": family,
        "equivalent_matches": equivalent,
        "stores_checked": checked,
        "stores_skipped": list(stores_skipped or []),
        "over_cap": any(row.get("over_cap") for row in exact + family + equivalent),
        "out_of_stock": any(row.get("out_of_stock") for row in exact),
        "canonical_gpu_ignored_as_evidence": canonical_gpu(target.get("gpu")),
    }
