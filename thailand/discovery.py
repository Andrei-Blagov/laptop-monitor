"""Wide Thailand discovery keys.

GPU and manufacturer-line queries find laptops of any brand. A regional SKU is
an extra search key, not a filter that drops other models. Purchase TOP still
requires the shared hardware and price gate.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from laptop_eligibility import evaluate_purchase
from thailand.fx import thb_to_rub
from thailand.models import StoreScanResult, ThailandOffer
from thailand.verification import is_verified_for_ranking

# Primary net: any notebook with the target GPU, in English and Thai.
# "RTX 5070" without Ti is a different GPU and is not used as a substitute.
GPU_SEARCH_QUERIES: tuple[str, ...] = (
    "notebook rtx 5070 ti",
    "notebook rtx 5080",
    "โน้ตบุ๊ค RTX 5070 Ti",
    "โน้ตบุ๊ค RTX 5080",
)

# Extra keys. They do not limit discovery to these lines.
LINE_SEARCH_QUERIES: tuple[str, ...] = (
    "ROG Strix RTX 5070 Ti",
    "TUF Gaming RTX 5070 Ti",
    "Legion RTX 5070 Ti",
    "Predator RTX 5070 Ti",
    "Omen RTX 5070 Ti",
    "AORUS RTX 5080",
    "Raider RTX 5070 Ti",
    "Stealth RTX 5070 Ti",
)

# One store pass: every GPU query, then supplied codes, then as many
# rotating line queries as still fit.
QUERY_BUDGET = 12


def context_gpu(query: str) -> str | None:
    """Return a target GPU only when the query names it. RTX 5070 is not Ti."""
    folded = "".join(str(query or "").lower().split())
    if "5080" in folded:
        return "RTX 5080"
    if "5070ti" in folded:
        return "RTX 5070 Ti"
    return None


def rotating_line_queries(now: datetime | None = None) -> list[str]:
    """Every line is used over time. A single pass still has a request budget."""
    lines = LINE_SEARCH_QUERIES
    if not lines:
        return []
    moment = now or datetime.now(timezone.utc)
    start = moment.toordinal() % len(lines)
    return list(lines[start:] + lines[:start])


def search_queries(
    *,
    extra_codes: list[str] | None = None,
    now: datetime | None = None,
    budget: int = QUERY_BUDGET,
) -> list[str]:
    """GPU queries first, then regional codes, then rotating line queries."""
    found: list[str] = []
    limit = max(len(GPU_SEARCH_QUERIES), int(budget))

    def add(value: object) -> bool:
        text = str(value or "").strip()
        if not text or text in found:
            return True
        if len(found) >= limit:
            return False
        found.append(text)
        return True

    for query in GPU_SEARCH_QUERIES:
        add(query)
    for code in extra_codes or []:
        if not add(code):
            break
    for query in rotating_line_queries(now):
        if not add(query):
            break
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
    hardware_and_price = 0
    purchasable = 0
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
            hardware_and_price += 1
            if is_verified_for_ranking(offer):
                purchasable += 1
            continue
        reasons[verdict.reason or "unknown"] += 1
        for item in verdict.details:
            details[str(item)] += 1
    planned = list(queries or [])
    completed = (
        len(planned)
        if queries_completed is None
        else max(0, min(len(planned), int(queries_completed)))
    )
    skipped = planned[completed:]
    skip_reason = None
    if skipped and rate_limited:
        skip_reason = "stopped_on_429"
    elif skipped:
        skip_reason = "request_budget"
    return {
        "discovered": len(offers),
        "spec_checked": spec_checked,
        "specs_confirmed": spec_checked,
        "hardware_and_price": hardware_and_price,
        "meets_requirements": hardware_and_price,
        "purchasable": purchasable,
        "exclusion_reasons": dict(reasons),
        "exclusion_details": dict(details),
        "queries": planned,
        "queries_completed": completed,
        "queries_not_completed": skipped,
        "query_skip_reason": skip_reason,
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
