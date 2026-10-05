from __future__ import annotations

"""On-demand Thailand market scan orchestrator."""

import inspect
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import config
from buy_opportunity import BuySignal
from deal_ranking import RankedDeal
from thailand.comparison import country_verdict
from thailand.eligibility import (
    EXCLUDED_FX_UNUSABLE,
    annotate_offer_price_scope,
    effective_thb_cap,
    max_tracked_price_rub,
    offer_user_facing_eligible,
    price_within_cap,
    sort_cheapest_rows,
    sort_value_rows,
)
from thailand.fx import fetch_cbr_thb_rate, fx_usable_for_verdict, thb_to_rub
from thailand.grouping import dedupe_user_facing_rows
from thailand.matching import group_thai_offers, match_russian_to_thai
from thailand.models import FxRate, StoreScanResult, ThailandOffer
from thailand.registry import ThailandStoreAdapter, get_thailand_adapters
from thailand.source_health import load_health, record_observation, skip_reason
from thailand.scoring import international_value_score
from thailand.seller_trust import (
    annotate_marketplace_identity,
    classify_seller_trust,
    is_banana_it_seller,
    marketplace_confidence,
    store_display_label,
)
from thailand.verification import international_confidence
from thailand.storage import write_scan_snapshot

logger = logging.getLogger(__name__)


def derive_scan_status(store_results: Sequence[StoreScanResult]) -> str:
    """
    PARTIAL only when an enabled, attempted source failed.

    Policy-disabled stores and an open circuit breaker are not failures.
    NO_RESULTS is a successful empty read (ok=True).
    """
    relevant = [
        r
        for r in store_results
        if not r.policy_disabled and r.circuit_breaker_status != "open"
    ]
    if not relevant:
        return "failed"
    failures = [
        r
        for r in relevant
        if not r.ok and r.error_code != "RATE_LIMITED" and r.collection_mode != "rate_limited"
    ]
    if not failures:
        return "ok"
    if len(failures) == len(relevant):
        return "failed"
    return "partial"


def _disabled_result(adapter: ThailandStoreAdapter) -> StoreScanResult:
    return StoreScanResult(
        store=adapter.slug,
        ok=True,
        offers=[],
        collection_mode="disabled",
        policy_disabled=True,
        circuit_breaker_status="closed",
        duration_seconds=0.0,
        discovered_count=0,
        verified_count=0,
    )


def _skipped_result(adapter: ThailandStoreAdapter) -> StoreScanResult:
    return StoreScanResult(
        store=adapter.slug,
        ok=True,
        offers=[],
        collection_mode="skipped",
        circuit_breaker_status="open",
        duration_seconds=0.0,
        discovered_count=0,
        verified_count=0,
    )


def _rate_limited_result(adapter: ThailandStoreAdapter) -> StoreScanResult:
    return StoreScanResult(
        store=adapter.slug,
        ok=True,
        offers=[],
        error_code="RATE_LIMITED",
        collection_mode="rate_limited",
        duration_seconds=0.0,
        discovered_count=0,
        verified_count=0,
    )


def _collect_store(adapter, timeout: float, target_codes: list[str] | None = None) -> StoreScanResult:
    kwargs: dict[str, Any] = {"timeout": timeout}
    try:
        params = inspect.signature(adapter.collect).parameters
        if "target_codes" in params and target_codes:
            kwargs["target_codes"] = target_codes
    except (TypeError, ValueError):
        pass
    try:
        return adapter.collect(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thailand store %s failed: %s", adapter.slug, type(exc).__name__)
        from thailand.errors import classify_store_failure

        code, detail = classify_store_failure(type(exc).__name__)
        return StoreScanResult(
            store=adapter.slug,
            ok=False,
            offers=[],
            error=code,
            error_code=code,
            technical_detail=detail,
            collection_mode="failed",
        )


def collect_thailand_offers(
    *,
    overall_timeout: float | None = None,
    per_store_timeout: float | None = None,
    parallel: bool = True,
    target_codes: list[str] | None = None,
) -> tuple[list[StoreScanResult], list[ThailandOffer], list[ThailandOffer], float]:
    overall_timeout = float(
        overall_timeout
        if overall_timeout is not None
        else config.THAILAND_OVERALL_TIMEOUT_SECONDS
    )
    per_store_timeout = float(
        per_store_timeout
        if per_store_timeout is not None
        else config.THAILAND_PER_STORE_TIMEOUT_SECONDS
    )
    all_adapters = get_thailand_adapters()
    placeholders = [_disabled_result(a) for a in all_adapters if not a.enabled]
    adapters = []
    for adapter in all_adapters:
        if not adapter.enabled:
            continue
        reason = skip_reason(adapter.slug)
        if reason == "circuit":
            placeholders.append(_skipped_result(adapter))
            continue
        if reason == "rate_limit":
            placeholders.append(_rate_limited_result(adapter))
            continue
        adapters.append(adapter)
    started = time.perf_counter()
    results: list[StoreScanResult] = []
    if not adapters and not placeholders:
        return [], [], [], 0.0

    # Playwright sync API is not safe in worker threads — keep browser stores
    # on the main thread; parallelize HTTP-only retailers.
    browser_slugs = {"lazada", "banana"}
    http_adapters = [a for a in adapters if a.slug not in browser_slugs]
    browser_adapters = [a for a in adapters if a.slug in browser_slugs]

    if parallel and len(http_adapters) > 1:
        with ThreadPoolExecutor(max_workers=len(http_adapters)) as pool:
            futs = {
                pool.submit(_collect_store, a, per_store_timeout, target_codes): a
                for a in http_adapters
            }
            try:
                for fut in as_completed(futs, timeout=overall_timeout):
                    results.append(fut.result())
            except TimeoutError:
                logger.warning("Thailand overall scan timeout (http stores)")
                done_stores = {r.store for r in results}
                for a in http_adapters:
                    if a.slug not in done_stores:
                        results.append(
                            StoreScanResult(
                                store=a.slug,
                                ok=False,
                                offers=[],
                                error="TIMEOUT",
                                error_code="TIMEOUT",
                                technical_detail="timeout",
                            )
                        )
    else:
        for a in http_adapters:
            if time.perf_counter() - started > overall_timeout:
                results.append(
                    StoreScanResult(
                        store=a.slug,
                        ok=False,
                        offers=[],
                        error="TIMEOUT",
                        error_code="TIMEOUT",
                        technical_detail="timeout",
                    )
                )
                continue
            results.append(_collect_store(a, per_store_timeout, target_codes))

    for a in browser_adapters:
        if time.perf_counter() - started > overall_timeout:
            results.append(
                StoreScanResult(
                    store=a.slug,
                    ok=False,
                    offers=[],
                    error="TIMEOUT",
                    error_code="TIMEOUT",
                    technical_detail="timeout",
                )
            )
            continue
        results.append(_collect_store(a, per_store_timeout, target_codes))

    for result in results:
        if result.policy_disabled or result.collection_mode in {"skipped", "rate_limited"}:
            continue
        try:
            record_observation(
                result.store,
                ok=bool(result.ok),
                error_code=result.error_code,
                offer_count=result.count,
                runtime_seconds=result.duration_seconds,
                retry_after_seconds=result.retry_after_seconds,
            )
        except Exception:  # noqa: BLE001
            logger.warning("thailand health write failed for %s", result.store)
    results.extend(placeholders)
    # Stable order. Browser stores stay on this thread (Playwright is not thread-safe);
    # THAILAND_BROWSER_MAX_CONCURRENCY documents that limit.
    _ = config.THAILAND_BROWSER_MAX_CONCURRENCY
    order = {a.slug: i for i, a in enumerate(all_adapters)}
    results.sort(key=lambda r: order.get(r.store, 99))
    offers: list[ThailandOffer] = []
    unverified: list[ThailandOffer] = []
    for r in results:
        offers.extend(r.offers)
        unverified.extend(getattr(r, "unverified_candidates", None) or [])
    return results, offers, unverified, time.perf_counter() - started


def _offer_to_top_row(
    o: ThailandOffer, *, price_rub: int | None
) -> dict[str, Any]:
    score = international_value_score(
        price_rub=price_rub,
        gpu=o.gpu,
        cpu=o.cpu,
        ram_gb=o.ram_gb,
        ssd_gb=o.ssd_gb,
        screen_inch=o.screen_size_inch,
        screen_resolution=o.screen_resolution,
    )
    if o.marketplace:
        annotate_marketplace_identity(o)
    tier = o.seller_trust_tier or (
        classify_seller_trust(o) if o.marketplace else None
    )
    mconf = o.marketplace_confidence
    if o.marketplace and mconf is None:
        mconf = marketplace_confidence(o, tier=tier)
    return {
        "store": o.store,
        "store_label": store_display_label(o),
        "external_id": o.external_id,
        "name": o.name,
        "gpu": o.gpu,
        "gpu_source": o.gpu_source,
        "cpu": o.cpu,
        "ram_gb": o.ram_gb,
        "ssd_gb": o.ssd_gb,
        "screen_size_inch": o.screen_size_inch,
        "screen_resolution": o.screen_resolution,
        "sku": o.sku,
        "manufacturer_part_number": o.manufacturer_part_number,
        "price_thb": o.price_thb,
        "price_rub": price_rub,
        "price_source": o.price_source,
        "availability_status": o.availability_status,
        "availability_source": o.availability_source,
        "verification_status": o.verification_status,
        "warranty": o.warranty,
        "url": o.url,
        "international_score": score.score,
        "international_confidence": international_confidence(o),
        "international_raw": score.raw,
        "score_breakdown": score.breakdown,
        "marketplace": bool(o.marketplace),
        "channel": o.channel or ("lazada" if o.marketplace else "direct"),
        "retailer_brand": o.retailer_brand,
        "seller_name": o.seller_name,
        "listing_id": o.listing_id,
        "seller_trust_tier": tier,
        "marketplace_confidence": mconf,
        "official_store": bool(o.official_store),
        "mall": bool(o.mall),
        "in_tracking_scope": True,
        "excluded_reason": None,
    }


def _eligible_rows(
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None,
    verified_only: bool = True,
) -> list[dict[str, Any]]:
    if not fx_usable_for_verdict(fx):
        return []
    rows: list[dict[str, Any]] = []
    for o in offers:
        ok, _reason, rub = offer_user_facing_eligible(
            o, fx=fx, require_verified=verified_only
        )
        if not ok:
            continue
        rows.append(_offer_to_top_row(o, price_rub=rub))
    return rows


def build_thai_top(
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None,
    limit: int | None = None,
    verified_only: bool = True,
) -> list[dict[str, Any]]:
    """User-facing TOP: cheapest RUB first among eligible offers (deduped)."""
    limit = int(limit if limit is not None else config.THAILAND_TOP_LIMIT)
    rows = _eligible_rows(offers, fx=fx, verified_only=verified_only)
    rows, _removed = dedupe_user_facing_rows(rows)
    rows = sort_cheapest_rows(rows)
    return rows[:limit]


def build_cheapest_eligible(
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Alias of user-facing cheapest TOP (diagnostic name)."""
    return build_thai_top(offers, fx=fx, limit=limit, verified_only=True)


def build_value_ranked_eligible(
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Diagnostic value ranking (score DESC); not default Telegram order."""
    limit = int(limit if limit is not None else config.THAILAND_TOP_LIMIT)
    rows = _eligible_rows(offers, fx=fx, verified_only=True)
    rows, _removed = dedupe_user_facing_rows(rows)
    rows = sort_value_rows(rows)
    return rows[:limit]


def summarize_price_cap(
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None,
) -> dict[str, Any]:
    usable = fx_usable_for_verdict(fx)
    verified = 0
    eligible = 0
    over_cap = 0
    for o in offers:
        from thailand.verification import is_verified_for_ranking

        if is_verified_for_ranking(o):
            verified += 1
        if not usable:
            continue
        rub = thb_to_rub(o.price_thb, fx)
        if rub is None:
            continue
        if price_within_cap(rub):
            ok, _, _ = offer_user_facing_eligible(o, fx=fx, require_verified=True)
            if ok:
                eligible += 1
        else:
            over_cap += 1
    return {
        "max_tracked_price_rub": max_tracked_price_rub(),
        "fx_usable": usable,
        "effective_thb_cap": effective_thb_cap(fx),
        "verified_count": verified,
        "eligible_count": eligible,
        "over_cap_count": over_cap,
        "cap_unavailable_reason": None if usable else EXCLUDED_FX_UNUSABLE,
    }


def run_thailand_scan(
    *,
    trigger_type: str,
    russian_deals: Sequence[RankedDeal] | None = None,
    trigger_signals: Sequence[BuySignal] | None = None,
    snapshot_dir: Path | str | None = None,
    write_snapshot: bool = True,
    target_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Best-effort Thailand scan. Never raises to caller for store failures.

    Returns structured result for Telegram / diagnostics.
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    result: dict[str, Any] = {
        "scan_id": started_at.strftime("%Y%m%dT%H%M%SZ"),
        "started_at": started_at.isoformat(),
        "trigger_type": trigger_type,
        "status": "failed",
        "russian_status_unchanged": True,
    }
    try:
        fx = fetch_cbr_thb_rate()
        fx_runtime = time.perf_counter() - t0
        codes = [str(c) for c in (target_model or {}).get("model_codes") or [] if c][:1]
        store_results, offers, unverified, collect_runtime = collect_thailand_offers(
            target_codes=codes or None
        )
        match_t0 = time.perf_counter()

        price_rub_map: dict[str, int | None] = {}
        for o in list(offers) + list(unverified):
            if o.marketplace:
                annotate_marketplace_identity(o)
            elif not o.channel:
                o.channel = "direct"
            annotate_offer_price_scope(o, fx=fx)
            if o.marketplace and not o.seller_trust_tier:
                o.seller_trust_tier = classify_seller_trust(o)
                o.marketplace_confidence = marketplace_confidence(
                    o, tier=o.seller_trust_tier
                )
            key = f"{o.store}:{o.external_id}"
            price_rub_map[key] = thb_to_rub(o.price_thb, fx) if fx else None

        primary_deal = None
        if trigger_signals:
            # Find matching ranked deal by model key / name
            sig = trigger_signals[0]
            for d in russian_deals or []:
                if d.cluster_name == sig.cluster_name or d.price == sig.price:
                    primary_deal = d
                    break
        if primary_deal is None and russian_deals:
            primary_deal = russian_deals[0]

        matches = []
        comparison = None
        match_over_cap_note = None
        if primary_deal is not None:
            # Prefer verified offers for matching; fall back to all discovered.
            match_pool = offers or unverified
            matches = match_russian_to_thai(
                primary_deal, match_pool, thai_price_rub=price_rub_map
            )
            best_match = None
            for m in matches:
                if m.level in {"EXACT", "EQUIVALENT", "SAME_FAMILY"}:
                    # User-facing purchase recommendation respects price cap.
                    if m.thai_price_rub is not None and not price_within_cap(
                        m.thai_price_rub
                    ):
                        if match_over_cap_note is None:
                            match_over_cap_note = (
                                "Та же/эквивалентная модель найдена, "
                                "но цена выше установленного лимита "
                                f"{max_tracked_price_rub():,} ₽.".replace(",", " ")
                            )
                        continue
                    best_match = m
                    break
            comparison = country_verdict(best_match, fx=fx)
        else:
            best_match = None

        top = build_thai_top(offers, fx=fx, verified_only=True)
        cheapest = build_cheapest_eligible(offers, fx=fx)
        value_ranked = build_value_ranked_eligible(offers, fx=fx)
        # Count duplicates removed for report
        raw_eligible_rows = _eligible_rows(offers, fx=fx, verified_only=True)
        _deduped, duplicates_removed = dedupe_user_facing_rows(raw_eligible_rows)
        cap_summary = summarize_price_cap(offers, fx=fx)
        eligible_offers = []
        over_cap_offers = []
        for o in offers:
            rub = price_rub_map.get(f"{o.store}:{o.external_id}")
            if rub is not None and not price_within_cap(rub):
                over_cap_offers.append(o)
            ok, _, _ = offer_user_facing_eligible(o, fx=fx, require_verified=True)
            if ok:
                eligible_offers.append(o)
        direct_banana = [o for o in offers if o.store == "banana" and not o.marketplace]
        lazada_banana = [
            o
            for o in offers
            if o.marketplace
            and (
                o.retailer_brand == "BaNANA"
                or is_banana_it_seller(o.seller_name)
            )
        ]
        lazada_jib = [
            o
            for o in offers
            if o.marketplace and (o.retailer_brand == "JIB" or (o.seller_name or "").upper().find("JIB") >= 0)
        ]
        official_brand = [
            o for o in offers if o.marketplace and (o.official_store or o.mall)
        ]
        groups = group_thai_offers(offers)
        match_runtime = time.perf_counter() - match_t0

        status = derive_scan_status(store_results)
        target_search = None
        if target_model:
            from thailand.target_model import search_collected_offers

            skipped = [
                {"store": r.store, "reason": r.error_code or r.collection_mode}
                for r in store_results
                if r.collection_mode in {"rate_limited", "skipped", "disabled"} or r.error_code == "RATE_LIMITED"
            ]
            target_search = search_collected_offers(
                target_model,
                list(offers) + list(unverified),
                fx=fx,
                stores_skipped=skipped,
            )

        finished_at = datetime.now(timezone.utc)
        result.update(
            {
                "finished_at": finished_at.isoformat(),
                "status": status,
                "fx": fx.to_dict() if fx else {"error": "unavailable", "stale": True},
                "fx_usable_for_verdict": fx_usable_for_verdict(fx),
                "stores": [
                    {
                        "store": r.store,
                        "ok": r.ok,
                        "count": r.count,
                        "error": r.error_code or r.error,
                        "error_code": r.error_code,
                        "technical_detail": r.technical_detail,
                        "duration_seconds": r.duration_seconds,
                        "collection_mode": r.collection_mode,
                        "offers_discovered": r.discovered_count,
                        "offers_verified": r.verified_count,
                        "circuit_breaker_status": r.circuit_breaker_status,
                        "policy_disabled": r.policy_disabled,
                        "request_count": r.request_count,
                    }
                    for r in store_results
                ],
                "source_health": load_health().get("stores") or {},
                "target_model": target_model,
                "target_search": target_search,
                "request_counts": {
                    r.store: r.request_count for r in store_results if r.request_count is not None
                },
                "all_verified_offers": [o.to_dict() for o in offers],
                "eligible_offers": [o.to_dict() for o in eligible_offers],
                "over_cap_offers_count": len(over_cap_offers),
                "verified_offers": [o.to_dict() for o in offers],
                "unverified_candidates": [o.to_dict() for o in unverified],
                "offers": [o.to_dict() for o in offers],
                "price_cap": cap_summary,
                "trigger_models": [
                    s.cluster_name for s in (trigger_signals or [])
                ],
                "russian_deals_snapshot": [
                    {
                        "name": d.cluster_name,
                        "price": d.price,
                        "store": d.store,
                        "gpu": d.gpu,
                        "score": d.score,
                        "confidence": d.confidence,
                        "historical_min": d.historical_min,
                    }
                    for d in (russian_deals or [])[:10]
                ],
                "matching": {
                    "exact": [m.level for m in matches if m.level == "EXACT"],
                    "same_family": [
                        m.level for m in matches if m.level == "SAME_FAMILY"
                    ],
                    "equivalent": [
                        m.level for m in matches if m.level == "EQUIVALENT"
                    ],
                    "alternatives": [
                        m.level for m in matches if m.level == "ALTERNATIVE"
                    ],
                    "best_level": best_match.level if best_match else None,
                    "best_thai_name": (
                        best_match.thai_offer.name if best_match else None
                    ),
                    "differences": best_match.differences if best_match else [],
                    "over_cap_note": match_over_cap_note,
                },
                "country_comparison": {
                    "verdict": comparison.verdict if comparison else None,
                    "reasons": comparison.reasons if comparison else [],
                },
                "thailand_top": top,
                "cheapest_eligible_thailand": cheapest,
                "value_ranked_eligible_thailand": value_ranked,
                "duplicates_removed_from_top": duplicates_removed,
                "direct_banana_offers": len(direct_banana),
                "lazada_banana_offers": len(lazada_banana),
                "lazada_jib_offers": len(lazada_jib),
                "official_brand_store_offers": len(official_brand),
                "unverified_count": len(unverified),
                "thai_groups": len(groups),
                "runtimes": {
                    "fx_seconds": round(fx_runtime, 3),
                    "collect_seconds": round(collect_runtime, 3),
                    "matching_seconds": round(match_runtime, 3),
                    "overall_seconds": round(time.perf_counter() - t0, 3),
                    "per_store_seconds": {
                        r.store: r.duration_seconds for r in store_results
                    },
                },
                "_store_results": store_results,
                "_fx": fx,
                "_best_match": best_match,
                "_comparison": comparison,
                "_top": top,
                "_match_over_cap_note": match_over_cap_note,
                "_lazada_banana_offers": lazada_banana,
            }
        )

        if write_snapshot:
            snap_payload = {
                k: v for k, v in result.items() if not str(k).startswith("_")
            }
            path = write_scan_snapshot(
                snap_payload,
                directory=snapshot_dir
                if snapshot_dir is not None
                else Path("data") / "thailand_scans",
                when=started_at,
            )
            result["snapshot_path"] = str(path)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thailand scan failed: %s", type(exc).__name__)
        result["status"] = "failed"
        result["error"] = type(exc).__name__
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["runtimes"] = {"overall_seconds": round(time.perf_counter() - t0, 3)}
        return result
