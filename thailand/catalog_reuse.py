"""Decide whether a saved Thai catalog can be reused without calling stores.

C2.2b later performs the HTTP named by REVALIDATE and DISCOVER. This module
only reads the saved catalog, explains the decision, and recomputes views.
It does not change offer timestamps or write a new snapshot.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from market_contracts import age_valid, moment
from thai_catalog import ThaiCatalogError, discovery_scope_hash, load_thai_catalog
from thailand.source_health import skip_reason

REUSE = "REUSE"
REVALIDATE = "REVALIDATE"
DISCOVER = "DISCOVER"
DEFERRED = "DEFERRED"
QUOTE_HOURS = 6
CATALOG_HOURS = 26
DAILY_MIN_HOURS = 18


def _utc(value: datetime) -> datetime:
    parsed = moment(value)
    if parsed is None:
        raise ValueError("explicit UTC timestamp required")
    return parsed


def evaluate_catalog_reuse(
    snapshot_root: Path | str,
    *,
    now: datetime,
    instance_id: str,
    expected_scope_hash: str | None = None,
    trigger: str = "manual",
    russian_snapshot: Mapping[str, Any] | None = None,
    fx: Mapping[str, Any] | None = None,
    health_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return a reuse decision. Store adapters are never called."""
    clock = _utc(now)
    requested_scope = expected_scope_hash or discovery_scope_hash()
    try:
        document = load_thai_catalog(snapshot_root, expected_instance_id=instance_id)
    except (ThaiCatalogError, OSError, ValueError) as exc:
        return _result(
            DISCOVER,
            reasons=[f"catalog_unusable:{type(exc).__name__}"],
            discover_reasons=[str(exc)],
        )
    stored_scope = str(document.get("discovery_scope_hash") or "")
    if stored_scope != requested_scope:
        return _result(
            DISCOVER,
            reasons=["discovery_scope_incompatible"],
            discover_reasons=[f"stored:{stored_scope}", f"requested:{requested_scope}"],
            snapshot_id=document.get("snapshot_id"),
            warnings=_coverage_warnings(document),
            document=document,
            clock=clock,
            health_path=health_path,
        )
    discovered = moment(document.get("discovery_checked_at"))
    if not age_valid(discovered, clock, CATALOG_HOURS):
        return _result(
            _defer_or(DISCOVER, document, clock, health_path),
            reasons=["broad_catalog_expired"],
            discover_reasons=["discovery_checked_at_outside_26h"],
            snapshot_id=document.get("snapshot_id"),
            warnings=_coverage_warnings(document),
            document=document,
            clock=clock,
            health_path=health_path,
        )
    if trigger == "daily" and discovered is not None and clock - discovered >= timedelta(hours=DAILY_MIN_HOURS):
        return _result(
            _defer_or(DISCOVER, document, clock, health_path),
            reasons=["daily_broad_pass_allowed"],
            discover_reasons=["compatible_catalog_at_least_18h"],
            snapshot_id=document.get("snapshot_id"),
            warnings=_coverage_warnings(document),
            document=document,
            clock=clock,
            health_path=health_path,
        )
    stale = _stale_offers(document, clock)
    blocked, open_items = _split_blocks(stale, document, clock, health_path)
    warnings = _coverage_warnings(document)
    if not stale:
        rankings, comparison = _views(document, fx, russian_snapshot, clock)
        return _result(
            REUSE,
            reasons=["broad_catalog_current", "quotes_current"],
            snapshot_id=document.get("snapshot_id"),
            warnings=warnings,
            rankings=rankings,
            comparison=comparison,
        )
    action = REVALIDATE if open_items else DEFERRED
    rankings, comparison = _views(document, fx, russian_snapshot, clock)
    reasons = ["broad_catalog_current", "quotes_need_recheck"]
    if blocked:
        reasons.append("source_refresh_deferred")
    return _result(
        action,
        reasons=reasons,
        snapshot_id=document.get("snapshot_id"),
        warnings=warnings,
        revalidate_offers=open_items,
        deferred_offers=blocked,
        rankings=rankings,
        comparison=comparison,
    )


def _views(document, fx, russian_snapshot, now: datetime):
    from thai_catalog import comparison_from_catalog, rankings_from_catalog

    rankings = rankings_from_catalog(document, fx, now=now)
    comparison = None
    if russian_snapshot is not None and fx is not None:
        comparison = comparison_from_catalog(document, russian_snapshot, fx, now=now)
    return rankings, comparison


def _stale_offers(document: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    found = []
    for offer in document.get("offers") or []:
        if offer.get("record_role") == "candidate" or offer.get("purchase_excluded_reason") == "unverified_candidate":
            continue
        price_at = moment(offer.get("price_checked_at"))
        stock_at = moment(offer.get("availability_checked_at"))
        reasons = []
        if not age_valid(price_at, now, QUOTE_HOURS):
            reasons.append("price_not_current")
        if not age_valid(stock_at, now, QUOTE_HOURS) or offer.get("availability_confirmed") is not True:
            reasons.append("availability_not_current")
        if not reasons:
            continue
        found.append({
            "source": offer.get("source"),
            "external_id": offer.get("external_id"),
            "variant_id": offer.get("variant_id"),
            "offer_id": offer.get("offer_id"),
            "reason": "+".join(reasons),
            "price_checked_at": offer.get("price_checked_at"),
            "availability_checked_at": offer.get("availability_checked_at"),
        })
    return found


def _split_blocks(items, document, now: datetime, health_path):
    coverage = {row.get("source"): row for row in document.get("source_coverage") or []}
    blocked, open_items = [], []
    for item in items:
        reason, nxt = _source_block(str(item.get("source") or ""), coverage.get(item.get("source")), now, health_path)
        if reason:
            blocked.append({**item, "block_reason": reason, "next_allowed_at": nxt})
        else:
            open_items.append(item)
    return blocked, open_items


def _defer_or(action: str, document, now: datetime, health_path) -> str:
    coverage = document.get("source_coverage") or []
    enabled = [row for row in coverage if row.get("enabled") and not row.get("policy_disabled")]
    if enabled and all(_source_block(str(row.get("source") or ""), row, now, health_path)[0] for row in enabled):
        return DEFERRED
    return action


def _source_block(source: str, row: Mapping[str, Any] | None, now: datetime, health_path) -> tuple[str | None, str | None]:
    if health_path is not None:
        reason = skip_reason(source, now=now, path=health_path)
        if reason == "circuit":
            return "circuit", None
        if reason == "rate_limit":
            return "retry_after", None
    if not row or row.get("policy_disabled"):
        return None, None
    if row.get("state") == "deferred":
        return "circuit", None
    retry = row.get("retry_after_seconds")
    checked = moment(row.get("latest_attempt_at") or row.get("checked_at"))
    if isinstance(retry, int) and not isinstance(retry, bool) and retry > 0 and checked is not None:
        nxt = checked + timedelta(seconds=retry)
        if now < nxt:
            return "retry_after", nxt.isoformat()
    return None, None


def _coverage_warnings(document: Mapping[str, Any]) -> list[str]:
    warnings = [str(item) for item in document.get("catalog_limitations") or []]
    for row in document.get("source_coverage") or []:
        state = row.get("state")
        if state in {"partial", "429", "failed"}:
            warnings.append(f"{row.get('source')}:{state}_is_not_absence")
    return warnings


def _result(
    action: str,
    *,
    reasons: list[str],
    snapshot_id: str | None = None,
    warnings: list[str] | None = None,
    discover_reasons: list[str] | None = None,
    revalidate_offers: list[dict] | None = None,
    deferred_offers: list[dict] | None = None,
    rankings: dict | None = None,
    comparison: dict | None = None,
    document: Mapping[str, Any] | None = None,
    clock: datetime | None = None,
    health_path=None,
    http_planned: bool = True,
) -> dict[str, Any]:
    if action == DISCOVER and document is not None and clock is not None:
        action = _defer_or(DISCOVER, document, clock, health_path)
    return {
        "action": action,
        "reasons": reasons,
        "discover_reasons": discover_reasons or [],
        "warnings": warnings or [],
        "revalidate_offers": revalidate_offers or [],
        "deferred_offers": deferred_offers or [],
        "snapshot_id": snapshot_id,
        "rankings": rankings,
        "comparison": comparison,
        "http_calls": 0,
        "refresh_allowed": action in {REVALIDATE, DISCOVER} and http_planned,
    }
