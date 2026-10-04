from __future__ import annotations

"""Cross-country and Thai-internal matching."""

from dataclasses import dataclass, field
from typing import Any, Sequence

from deal_ranking import RankedDeal, canonical_gpu, classify_cpu
from thailand.models import CrossCountryMatch, ThailandOffer
from thailand.specs_parse import strip_regional_sku_suffix
from thailand.verification import is_purchasable_confirmed


@dataclass
class ThaiGroup:
    key: str
    offers: list[ThailandOffer] = field(default_factory=list)
    best: ThailandOffer | None = None


def _norm_mpn(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(ch for ch in value.upper() if ch.isalnum() or ch in "-_")


def strong_identifiers(offer: ThailandOffer) -> list[str]:
    ids: list[str] = []
    for raw in (offer.manufacturer_part_number, offer.sku):
        n = _norm_mpn(raw)
        if n and len(n) >= 6:
            ids.append(n)
    return ids


def config_tuple(obj: Any) -> tuple:
    gpu = canonical_gpu(getattr(obj, "gpu", None))
    cpu = getattr(obj, "cpu", None)
    ram = getattr(obj, "ram_gb", None)
    ssd = getattr(obj, "ssd_gb", None)
    screen = getattr(obj, "screen_size_inch", None)
    if screen is None:
        screen = getattr(obj, "screen_inch", None)
    return (gpu, cpu, ram, ssd, screen)


def configs_equivalent(a: Any, b: Any) -> tuple[bool, list[str]]:
    """EQUIVALENT requires GPU/CPU/RAM/SSD/screen confirmed and matching."""
    diffs: list[str] = []
    ag, ac, ar, as_, ascr = config_tuple(a)
    bg, bc, br, bs, bscr = config_tuple(b)
    if not ag or not bg or ag != bg:
        diffs.append("gpu")
    if not ac or not bc:
        diffs.append("cpu_unknown")
    else:
        # Compare CPU class, not fuzzy name-only.
        ca, _ = classify_cpu(ac)
        cb, _ = classify_cpu(bc)
        if ca != cb or ac.upper().split()[:3] != bc.upper().split()[:3]:
            # Allow same family token overlap
            if not (
                any(t in bc.upper() for t in ac.upper().split() if len(t) > 3)
                and abs(ca - cb) <= 3
            ):
                diffs.append("cpu")
    if ar is None or br is None:
        diffs.append("ram_unknown")
    elif int(ar) != int(br):
        diffs.append(f"ram:{ar}vs{br}")
    if as_ is None or bs is None:
        diffs.append("ssd_unknown")
    elif int(as_) != int(bs):
        diffs.append(f"ssd:{as_}vs{bs}")
    if ascr is None or bscr is None:
        diffs.append("screen_unknown")
    elif abs(float(ascr) - float(bscr)) > 0.2:
        diffs.append(f"screen:{ascr}vs{bscr}")
    # Resolution informational — mismatch does not alone reject if others match.
    ra = getattr(a, "screen_resolution", None)
    rb = getattr(b, "screen_resolution", None)
    if ra and rb and str(ra) != str(rb):
        diffs.append(f"resolution:{ra}vs{rb}")
    hard = [d for d in diffs if not d.endswith("_unknown") and not d.startswith("resolution:")]
    unknown = [d for d in diffs if d.endswith("_unknown")]
    if hard:
        return False, diffs
    if unknown:
        return False, diffs
    return True, diffs


def same_family(a: Any, b: Any) -> bool:
    """Same brand + model family tokens; not name-only fuzzy EXACT."""
    an = (getattr(a, "name", None) or getattr(a, "cluster_name", None) or "").upper()
    bn = (getattr(b, "name", None) or getattr(b, "cluster_name", None) or "").upper()
    if not an or not bn:
        return False
    ag = canonical_gpu(getattr(a, "gpu", None))
    bg = canonical_gpu(getattr(b, "gpu", None))
    if ag and bg and ag != bg:
        return False
    # Shared distinctive tokens (brand + series)
    stop = {
        "NOTEBOOK",
        "LAPTOP",
        "GEFORCE",
        "RTX",
        "WITH",
        "THE",
        "AND",
        "BLACK",
        "WHITE",
        "GRAY",
        "GREY",
    }
    ta = {t for t in an.replace("-", " ").split() if len(t) >= 3 and t not in stop}
    tb = {t for t in bn.replace("-", " ").split() if len(t) >= 3 and t not in stop}
    if len(ta & tb) < 2:
        return False
    return True


def match_russian_to_thai(
    deal: RankedDeal,
    thai_offers: Sequence[ThailandOffer],
    *,
    thai_price_rub: dict[str, int | None] | None = None,
) -> list[CrossCountryMatch]:
    """
    Classify Thai offers vs one Russian deal.

    EXACT only on strong identical MPN (regional suffix alone ≠ EXACT).
    """
    thai_price_rub = thai_price_rub or {}
    results: list[CrossCountryMatch] = []
    ru_ids = []
    offer = deal.offer or {}
    for key in ("sku", "manufacturer_part_number", "external_id"):
        # only sku-like from offer
        pass
    # Russian MPN often in offer sku / name — collect from deal offer fields.
    for raw in (offer.get("sku"), offer.get("manufacturer_part_number")):
        n = _norm_mpn(str(raw) if raw else None)
        if n:
            ru_ids.append(n)
    # Also scan cluster name for MPN-like tokens
    from thailand.specs_parse import extract_mpn_candidates

    ru_ids.extend(_norm_mpn(x) or "" for x in extract_mpn_candidates(deal.cluster_name))
    ru_ids = [x for x in ru_ids if x]

    for thai in thai_offers:
        level = None
        reason = ""
        diffs: list[str] = []
        t_ids = strong_identifiers(thai)
        exact = False
        for rid in ru_ids:
            for tid in t_ids:
                if rid == tid:
                    exact = True
                    break
                rb, rs = strip_regional_sku_suffix(rid)
                tb, ts = strip_regional_sku_suffix(tid)
                if rb and tb and rb == tb and (rs or ts) and rs != ts:
                    # Regional SKU differs — never auto EXACT
                    level = "SAME_FAMILY"
                    reason = "regional_sku_suffix"
                    break
            if exact or level:
                break
        if exact:
            level = "EXACT"
            reason = "strong_mpn"
        elif level is None and same_family(deal, thai):
            eq, diffs = configs_equivalent(deal, thai)
            if eq:
                level = "EQUIVALENT"
                reason = "same_family_same_config"
            else:
                level = "SAME_FAMILY"
                reason = "same_family_config_diff"
        else:
            # ALTERNATIVE: target GPU market alternative
            if canonical_gpu(thai.gpu) in {"RTX 5070 Ti", "RTX 5080"}:
                level = "ALTERNATIVE"
                reason = "target_gpu_alternative"
                diffs = []
                eq, d2 = configs_equivalent(deal, thai)
                if not eq:
                    diffs = d2
            else:
                continue

        key = f"{thai.store}:{thai.external_id}"
        t_rub = thai_price_rub.get(key)
        t_thb = thai.price_thb
        ru = deal.price
        delta = None
        delta_pct = None
        if t_rub is not None and ru is not None:
            delta = int(t_rub) - int(ru)
            if ru:
                delta_pct = delta / float(ru)
        results.append(
            CrossCountryMatch(
                level=level,
                russian_name=deal.cluster_name,
                thai_offer=thai,
                russian_price_rub=ru,
                thai_price_thb=t_thb,
                thai_price_rub=t_rub,
                delta_rub=delta,
                delta_percent=delta_pct,
                differences=diffs,
                reason=reason,
            )
        )
    # Prefer EXACT > EQUIVALENT > SAME_FAMILY > ALTERNATIVE, then cheaper THB
    order = {"EXACT": 0, "EQUIVALENT": 1, "SAME_FAMILY": 2, "ALTERNATIVE": 3}
    results.sort(
        key=lambda m: (
            order.get(m.level, 9),
            m.thai_price_thb or 10**12,
        )
    )
    return results


def group_thai_offers(offers: Sequence[ThailandOffer]) -> list[ThaiGroup]:
    """Group identical Thai configuration across stores; pick cheapest purchasable."""
    groups: dict[str, ThaiGroup] = {}
    for o in offers:
        ids = strong_identifiers(o)
        if ids:
            key = "mpn:" + ids[0]
        else:
            key = "cfg:" + "|".join(
                str(x)
                for x in (
                    canonical_gpu(o.gpu),
                    o.brand,
                    o.ram_gb,
                    o.ssd_gb,
                    o.screen_size_inch,
                    (o.name or "")[:40].upper(),
                )
            )
        g = groups.setdefault(key, ThaiGroup(key=key))
        g.offers.append(o)
    for g in groups.values():
        purchasable = [
            o
            for o in g.offers
            if is_purchasable_confirmed(o) and o.price_thb is not None
        ]
        pool = purchasable or []
        if pool:
            g.best = sorted(pool, key=lambda o: (o.price_thb or 10**12, o.store))[0]
        else:
            g.best = None
    return list(groups.values())
