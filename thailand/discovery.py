"""Wide Thailand discovery keys.

GPU and manufacturer-line queries find laptops of any brand. A regional SKU is
an extra search key, not a filter that drops other models. Purchase TOP still
requires the shared hardware and price gate.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from laptop_eligibility import evaluate_purchase
from thailand.fx import thb_to_rub
from thailand.models import StoreScanResult, ThailandOffer

# Primary net: any notebook with the target GPU, in English and Thai.
GPU_SEARCH_QUERIES: tuple[str, ...] = (
    "notebook rtx 5070 ti",
    "notebook rtx 5080",
    "โน้ตบุ๊ค RTX 5070 Ti",
    "โน้ตบุ๊ค RTX 5080",
)

# Extra keys. They do not limit discovery to these lines.
LINE_SEARCH_QUERIES: tuple[str, ...] = (
    "ROG Strix RTX 5070",
    "TUF Gaming RTX 5080",
    "Legion RTX 5070",
    "Predator RTX 5070",
    "Omen RTX 5070",
    "AORUS RTX 5080",
    "Raider RTX 5070",
    "Stealth RTX 5070",
)

# How many line queries a keyword store adds on top of the GPU queries.
LINE_QUERY_BUDGET = 4


def search_queries(
    *,
    extra_codes: list[str] | None = None,
    line_limit: int = LINE_QUERY_BUDGET,
) -> list[str]:
    """GPU queries, then a capped set of line queries, then supplied codes."""
    found: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)

    for query in GPU_SEARCH_QUERIES:
        add(query)
    for query in LINE_SEARCH_QUERIES[: max(0, int(line_limit))]:
        add(query)
    for code in extra_codes or []:
        add(code)
    return found


def _spec_complete(offer: ThailandOffer) -> bool:
    return all(
        value is not None
        for value in (offer.gpu, offer.ram_gb, offer.ssd_gb, offer.screen_resolution)
    )


def coverage_for_offers(
    offers: list[ThailandOffer],
    *,
    fx: Any = None,
    queries: list[str] | None = None,
    search_mode: str | None = None,
    limitations: list[str] | None = None,
    rate_limited: bool = False,
    queries_completed: int | None = None,
) -> dict[str, Any]:
    """Count discovery versus the purchase gate. Incomplete offers stay in the list."""
    reasons: Counter[str] = Counter()
    details: Counter[str] = Counter()
    spec_checked = 0
    meets = 0
    for offer in offers:
        if _spec_complete(offer):
            spec_checked += 1
        price_rub = thb_to_rub(offer.price_thb, fx) if fx is not None else None
        verdict = evaluate_purchase(
            price_rub=price_rub,
            gpu=offer.gpu,
            ram_gb=offer.ram_gb,
            ssd_gb=offer.ssd_gb,
            screen_resolution=offer.screen_resolution,
        )
        if verdict.eligible:
            meets += 1
            continue
        reasons[verdict.reason or "unknown"] += 1
        for item in verdict.details:
            details[str(item)] += 1
    return {
        "discovered": len(offers),
        "spec_checked": spec_checked,
        "meets_requirements": meets,
        "exclusion_reasons": dict(reasons),
        "exclusion_details": dict(details),
        "queries": list(queries or []),
        "queries_completed": queries_completed if queries_completed is not None else len(queries or []),
        "search_mode": search_mode or "catalog",
        "limitations": list(limitations or []),
        "rate_limited": bool(rate_limited),
    }


def coverage_for_result(result: StoreScanResult, fx: Any = None) -> dict[str, Any]:
    offers = list(result.offers or []) + list(result.unverified_candidates or [])
    limitations = list(result.discovery_limitations or [])
    if result.policy_disabled or result.collection_mode == "disabled":
        limitations = limitations or ["policy_disabled"]
    if result.error_code == "RATE_LIMITED" or result.collection_mode == "rate_limited":
        limitations = limitations or ["stopped_on_429"]
    return coverage_for_offers(
        offers,
        fx=fx,
        queries=result.discovery_queries,
        search_mode=result.discovery_mode,
        limitations=limitations,
        rate_limited=bool(result.discovery_rate_limited or result.error_code == "RATE_LIMITED"),
        queries_completed=result.queries_completed,
    )
