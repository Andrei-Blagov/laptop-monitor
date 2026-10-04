from __future__ import annotations

"""On-demand Thailand market scan orchestrator."""

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
from thailand.fx import fetch_cbr_thb_rate, fx_usable_for_verdict, thb_to_rub
from thailand.matching import group_thai_offers, match_russian_to_thai
from thailand.models import FxRate, StoreScanResult, ThailandOffer
from thailand.registry import get_thailand_adapters
from thailand.scoring import international_value_score
from thailand.specs_parse import is_purchasable_for_best
from thailand.storage import write_scan_snapshot

logger = logging.getLogger(__name__)


def _collect_store(adapter, timeout: float) -> StoreScanResult:
    try:
        return adapter.collect(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thailand store %s failed: %s", adapter.slug, type(exc).__name__)
        return StoreScanResult(
            store=adapter.slug, ok=False, offers=[], error=type(exc).__name__
        )


def collect_thailand_offers(
    *,
    overall_timeout: float | None = None,
    per_store_timeout: float | None = None,
    parallel: bool = True,
) -> tuple[list[StoreScanResult], list[ThailandOffer], float]:
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
    adapters = [a for a in get_thailand_adapters() if a.enabled]
    started = time.perf_counter()
    results: list[StoreScanResult] = []
    if not adapters:
        return [], [], 0.0

    if parallel and len(adapters) > 1:
        with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
            futs = {
                pool.submit(_collect_store, a, per_store_timeout): a for a in adapters
            }
            try:
                for fut in as_completed(futs, timeout=overall_timeout):
                    results.append(fut.result())
            except TimeoutError:
                logger.warning("Thailand overall scan timeout")
                done_stores = {r.store for r in results}
                for a in adapters:
                    if a.slug not in done_stores:
                        results.append(
                            StoreScanResult(
                                store=a.slug,
                                ok=False,
                                offers=[],
                                error="timeout",
                            )
                        )
    else:
        for a in adapters:
            if time.perf_counter() - started > overall_timeout:
                results.append(
                    StoreScanResult(
                        store=a.slug, ok=False, offers=[], error="timeout"
                    )
                )
                continue
            results.append(_collect_store(a, per_store_timeout))

    # Stable order
    order = {a.slug: i for i, a in enumerate(adapters)}
    results.sort(key=lambda r: order.get(r.store, 99))
    offers: list[ThailandOffer] = []
    for r in results:
        offers.extend(r.offers)
    return results, offers, time.perf_counter() - started


def build_thai_top(
    offers: Sequence[ThailandOffer],
    *,
    fx: FxRate | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    limit = int(limit if limit is not None else config.THAILAND_TOP_LIMIT)
    rows: list[dict[str, Any]] = []
    for o in offers:
        if not is_purchasable_for_best(o.availability_status, o.available):
            continue
        if o.price_thb is None:
            continue
        rub = thb_to_rub(o.price_thb, fx) if fx_usable_for_verdict(fx) else thb_to_rub(
            o.price_thb, fx if fx and not fx.error else None
        )
        # For scoring use RUB if available; else skip price component via None
        score = international_value_score(
            price_rub=rub,
            gpu=o.gpu,
            cpu=o.cpu,
            ram_gb=o.ram_gb,
            ssd_gb=o.ssd_gb,
            screen_inch=o.screen_size_inch,
            screen_resolution=o.screen_resolution,
        )
        rows.append(
            {
                "store": o.store,
                "external_id": o.external_id,
                "name": o.name,
                "gpu": o.gpu,
                "cpu": o.cpu,
                "ram_gb": o.ram_gb,
                "ssd_gb": o.ssd_gb,
                "screen_size_inch": o.screen_size_inch,
                "screen_resolution": o.screen_resolution,
                "price_thb": o.price_thb,
                "price_rub": rub,
                "availability_status": o.availability_status,
                "warranty": o.warranty,
                "url": o.url,
                "international_score": score.score,
                "international_raw": score.raw,
                "score_breakdown": score.breakdown,
            }
        )
    rows.sort(
        key=lambda r: (
            -(r["international_score"] or 0),
            r["price_thb"] or 10**12,
        )
    )
    return rows[:limit]


def run_thailand_scan(
    *,
    trigger_type: str,
    russian_deals: Sequence[RankedDeal] | None = None,
    trigger_signals: Sequence[BuySignal] | None = None,
    snapshot_dir: Path | str | None = None,
    write_snapshot: bool = True,
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
        store_results, offers, collect_runtime = collect_thailand_offers()
        match_t0 = time.perf_counter()

        price_rub_map: dict[str, int | None] = {}
        for o in offers:
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
        if primary_deal is not None:
            matches = match_russian_to_thai(
                primary_deal, offers, thai_price_rub=price_rub_map
            )
            best_match = None
            for m in matches:
                if m.level in {"EXACT", "EQUIVALENT", "SAME_FAMILY"}:
                    best_match = m
                    break
            comparison = country_verdict(best_match, fx=fx)
        else:
            best_match = None

        top = build_thai_top(offers, fx=fx)
        groups = group_thai_offers(offers)
        match_runtime = time.perf_counter() - match_t0

        ok_stores = [r for r in store_results if r.ok]
        if not store_results:
            status = "failed"
        elif not ok_stores:
            status = "failed"
        elif len(ok_stores) < len(store_results):
            status = "partial"
        else:
            status = "ok"

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
                        "error": r.error,
                        "duration_seconds": r.duration_seconds,
                    }
                    for r in store_results
                ],
                "offers": [o.to_dict() for o in offers],
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
                },
                "country_comparison": {
                    "verdict": comparison.verdict if comparison else None,
                    "reasons": comparison.reasons if comparison else [],
                },
                "thailand_top": top,
                "thai_groups": len(groups),
                "runtimes": {
                    "fx_seconds": round(fx_runtime, 3),
                    "collect_seconds": round(collect_runtime, 3),
                    "matching_seconds": round(match_runtime, 3),
                    "overall_seconds": round(time.perf_counter() - t0, 3),
                },
                "_store_results": store_results,
                "_fx": fx,
                "_best_match": best_match,
                "_comparison": comparison,
                "_top": top,
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
