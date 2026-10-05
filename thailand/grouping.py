from __future__ import annotations

"""User-facing Thailand offer grouping / best-channel selection."""

from typing import Any

import config
from deal_ranking import canonical_gpu
from thailand.seller_trust import TIER_A, TIER_B, TIER_C


_RETAILER_PREFIXES = ("ASUS-", "ACER-", "MSI-", "LENOVO-", "GIGABYTE-", "HP-")


def normalize_model_code(value: str | None) -> str:
    """Drop a retailer prefix so ASUS-G614PR-TS113W matches JIB G614PR-TS113W."""
    norm = "".join(ch for ch in str(value or "").upper() if ch.isalnum() or ch in "-_")
    for prefix in _RETAILER_PREFIXES:
        rest = norm[len(prefix):]
        if norm.startswith(prefix) and "-" in rest and len(rest) >= 6:
            return rest
    return norm


def config_group_key(row: dict[str, Any]) -> str:
    """Group by strong MPN/SKU when present, else verified config tuple."""
    mpn = row.get("manufacturer_part_number") or row.get("sku")
    if mpn:
        norm = normalize_model_code(str(mpn))
        if len(norm) >= 6:
            return f"mpn:{norm}"
    gpu = canonical_gpu(row.get("gpu"))
    return "cfg:" + "|".join(
        str(x)
        for x in (
            gpu,
            row.get("cpu"),
            row.get("ram_gb"),
            row.get("ssd_gb"),
            row.get("screen_size_inch"),
            (row.get("name") or "")[:40].upper(),
        )
    )


def channel_preference_rank(row: dict[str, Any]) -> int:
    """
    Lower is better for near-price ties.

    direct > official marketplace > known marketplace retailer > Tier B
    """
    if not row.get("marketplace"):
        return 0
    if row.get("official_store") or row.get("mall"):
        return 1
    tier = row.get("seller_trust_tier")
    if tier == TIER_A:
        return 2
    if tier == TIER_B:
        return 3
    if tier == TIER_C:
        return 4
    return 5


def _row_identity(row: dict[str, Any]) -> str:
    return (
        f"{row.get('store')}|{row.get('seller_name')}|{row.get('external_id')}|"
        f"{row.get('listing_id') or ''}|{row.get('url') or ''}"
    )


def select_best_and_alts(
    group_rows: list[dict[str, Any]],
    *,
    near_pct: float | None = None,
    max_alts: int | None = None,
) -> dict[str, Any]:
    """
    Choose cheapest offer; within ~near_pct prefer direct/official channel.

    Attach up to max_alts alternative channels (other sellers).
    """
    near_pct = float(
        near_pct
        if near_pct is not None
        else getattr(config, "THAILAND_NEAR_PRICE_PCT", 0.02)
    )
    max_alts = int(
        max_alts
        if max_alts is not None
        else getattr(config, "THAILAND_TOP_ALT_CHANNELS_MAX", 2)
    )
    priced = [r for r in group_rows if r.get("price_rub") is not None]
    if not priced:
        best = dict(group_rows[0])
        best["alt_channels"] = []
        return best
    by_price = sorted(
        priced,
        key=lambda r: (
            int(r["price_rub"]),
            channel_preference_rank(r),
            -(r.get("international_score") or 0),
            -(r.get("international_confidence") or 0),
        ),
    )
    cheapest_rub = int(by_price[0]["price_rub"])
    band = cheapest_rub * (1.0 + max(0.0, near_pct))
    near = [r for r in by_price if int(r["price_rub"]) <= band]
    near.sort(
        key=lambda r: (
            channel_preference_rank(r),
            int(r["price_rub"]),
            -(r.get("international_score") or 0),
            -(r.get("international_confidence") or 0),
        )
    )
    best = dict(near[0])
    best_id = _row_identity(best)
    alts: list[dict[str, Any]] = []
    seen_sellers: set[str] = {
        f"{best.get('store')}|{(best.get('seller_name') or '').lower()}"
    }
    for r in by_price:
        if _row_identity(r) == best_id:
            continue
        seller_key = f"{r.get('store')}|{(r.get('seller_name') or '').lower()}"
        if seller_key in seen_sellers:
            continue
        seen_sellers.add(seller_key)
        alts.append(
            {
                "store": r.get("store"),
                "store_label": r.get("store_label"),
                "seller_name": r.get("seller_name"),
                "retailer_brand": r.get("retailer_brand"),
                "channel": r.get("channel"),
                "marketplace": r.get("marketplace"),
                "price_thb": r.get("price_thb"),
                "price_rub": r.get("price_rub"),
                "url": r.get("url"),
                "seller_trust_tier": r.get("seller_trust_tier"),
            }
        )
        if len(alts) >= max_alts:
            break
    best["alt_channels"] = alts
    best["group_size"] = len(group_rows)
    return best


def dedupe_user_facing_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """
    Collapse same configuration to one best offer.

    Returns (deduped_rows, removed_duplicate_count).
    """
    if not rows:
        return [], 0
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        key = config_group_key(row)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out: list[dict[str, Any]] = []
    removed = 0
    for key in order:
        g = groups[key]
        removed += max(0, len(g) - 1)
        out.append(select_best_and_alts(g))
    return out, removed
