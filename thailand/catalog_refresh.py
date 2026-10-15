"""Execute a C2.2a catalog decision. Queue, Telegram, and schedules stay outside.

REUSE never calls a store. REVALIDATE refreshes only the named offers: JIB by
its product page, and Advice, SpeedCom, InvadeIT, and IT City by one bounded
request. DISCOVER is the only action that runs one broad adapter pass.
A circuit breaker, Retry-After, or a disabled store is checked again immediately
before every HTTP call.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx

import config
from cross_market_comparison import comparison_view
from market_contracts import moment
from thai_catalog import (
    LATEST_NAME,
    SNAPSHOT_DIRNAME,
    ThaiCatalogError,
    _dumps,
    comparison_from_catalog,
    export_thai_catalog,
    load_thai_catalog,
    rankings_from_catalog,
)
from thailand.catalog_reuse import _source_block
from thailand.errors import RATE_LIMITED
from thailand.models import StoreScanResult
from thailand.registry import get_thailand_adapters
from thailand.source_health import record_observation
from thailand.storage import atomic_write_json

COMPLETED = "completed"
DEFERRED = "deferred"
PARTIAL = "partial"
FAILED = "failed"

POINT_CHECK_SOURCES = frozenset({"jib"})
BOUNDED_SOURCE_REFRESH_SOURCES = frozenset({"advice", "speedcom", "invadeit", "itcity"})
POLICY_DISABLED_SOURCES = frozenset({"banana", "lazada"})


@dataclass
class OfferRefresh:
    source: str
    external_id: str
    variant_id: str | None = None
    offer_id: str | None = None
    status: str = "skipped"
    reason: str | None = None
    price_thb: int | None = None
    availability_state: str | None = None
    availability_confirmed: bool = False
    observed_at: datetime | None = None
    http_calls: int = 0
    retry_after_seconds: int | None = None
    coverage: str = ""
    observed_specs: dict[str, Any] = field(default_factory=dict)
    variant_confirmed: bool = True


class LiveCatalogTransport:
    """Store calls used when a test does not inject its own transport.

    REVALIDATE never calls ``collect``. DISCOVER calls one adapter ``collect``
    per allowed source and does not receive a Russian TOP.
    """

    def __init__(self, client: httpx.Client | None = None, timeout: float = 20.0) -> None:
        self.client = client
        self.timeout = timeout
        self.calls: list[tuple[str, str, str | None]] = []

    def refresh_offer(self, item: Mapping[str, Any], *, mode: str, now: datetime) -> OfferRefresh:
        source = str(item.get("source") or "")
        external_id = str(item.get("external_id") or "")
        self.calls.append((mode, source, external_id))
        if source in POLICY_DISABLED_SOURCES or mode == "refused":
            return _skipped(item, "source_disabled", coverage="disabled source was not requested")
        if mode == "point" and source in POINT_CHECK_SOURCES:
            return _jib_point(item, client=self.client, timeout=self.timeout, now=now)
        if mode == "bounded" and source in BOUNDED_SOURCE_REFRESH_SOURCES:
            return _bounded_request(item, client=self.client, timeout=self.timeout, now=now)
        return _skipped(item, "refresh_mode_refused", coverage="broad search was not started")

    def discover_source(self, source: str, *, now: datetime) -> StoreScanResult:
        del now
        self.calls.append(("discover", source, None))
        if source in POLICY_DISABLED_SOURCES:
            raise ThaiCatalogError(f"disabled source is not discoverable: {source}")
        adapter = next(item for item in get_thailand_adapters() if item.slug == source)
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.client is not None:
            kwargs["client"] = self.client
        return adapter.collect(**kwargs)


def execute_catalog_plan(
    decision: Mapping[str, Any],
    *,
    snapshot_root: Path | str,
    now: datetime,
    instance_id: str,
    health_path: Path | str | None = None,
    russian_snapshot: Mapping[str, Any] | None = None,
    fx: Mapping[str, Any] | None = None,
    transport: Any = None,
    publish_barrier: Any = None,
    overall_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run one reuse decision. Returns the orchestration result and does not notify."""
    clock = _clock(now)
    root = Path(snapshot_root)
    actor = transport or LiveCatalogTransport()
    action = str(decision.get("action") or "")
    try:
        previous = load_thai_catalog(root, expected_instance_id=instance_id)
    except (ThaiCatalogError, OSError, ValueError) as exc:
        if action != "DISCOVER":
            return _outcome(FAILED, action=action, errors=[f"catalog_unusable:{type(exc).__name__}"], detail=str(exc))
        previous = None
    if action in {"REUSE", "DEFERRED"} or not decision.get("refresh_allowed"):
        return _reuse_or_defer(previous, action, fx, russian_snapshot, clock)
    timeout = float(
        overall_timeout_seconds
        if overall_timeout_seconds is not None
        else config.THAILAND_OVERALL_TIMEOUT_SECONDS
    )
    started = time.monotonic()
    if action == "REVALIDATE":
        return _revalidate(
            previous, decision, root, clock, instance_id, health_path, russian_snapshot, fx,
            actor, publish_barrier, started, timeout,
        )
    if action == "DISCOVER":
        return _discover(
            previous, decision, root, clock, instance_id, health_path, russian_snapshot, fx,
            actor, publish_barrier, started, timeout,
        )
    return _outcome(FAILED, action=action, errors=[f"unknown_action:{action}"], snapshot_id=_id(previous))


def _reuse_or_defer(document, action, fx, russian_snapshot, now: datetime) -> dict[str, Any]:
    if document is None:
        return _outcome(FAILED, action=action, errors=["catalog_missing"])
    rankings, comparison, views = _views(document, fx, russian_snapshot, now)
    status = COMPLETED if action == "REUSE" else DEFERRED
    return _outcome(
        status,
        action=action,
        snapshot_id=document.get("snapshot_id"),
        parent_snapshot_id=document.get("parent_snapshot_id"),
        rankings=rankings,
        comparison=comparison,
        views=views,
        coverage=list(document.get("source_coverage") or []),
        http_calls=0,
    )


def _revalidate(previous, decision, root, now, instance_id, health_path, russian, fx, transport, barrier, started, timeout):
    if previous is None:
        return _outcome(FAILED, action="REVALIDATE", errors=["catalog_missing"])
    updated, skipped, errors, http_calls, next_at = [], [], [], 0, []
    stopped: set[str] = set()
    coverage_rows = {row.get("source"): row for row in previous.get("source_coverage") or []}
    for item in decision.get("revalidate_offers") or []:
        source = str(item.get("source") or "")
        if _timed_out(started, timeout):
            skipped.append(_skip(item, "deadline"))
            continue
        if source in stopped:
            skipped.append(_skip(item, "stopped_after_429"))
            continue
        block, when = _source_block(source, coverage_rows.get(source), now, health_path)
        if block or source in POLICY_DISABLED_SOURCES or source not in POINT_CHECK_SOURCES | BOUNDED_SOURCE_REFRESH_SOURCES:
            reason = block or "source_disabled"
            skipped.append(_skip(item, reason, when))
            if when:
                next_at.append(when)
            continue
        mode = "point" if source in POINT_CHECK_SOURCES else "bounded"
        refresh = transport.refresh_offer(item, mode=mode, now=now)
        http_calls += int(refresh.http_calls or 0)
        if refresh.status == "rate_limited":
            stopped.add(source)
            when = _remember_rate_limit(source, refresh, now, health_path)
            skipped.append(_skip(item, "retry_after", when))
            if when:
                next_at.append(when)
            continue
        kind = _apply_refresh(previous, refresh)
        row = _row(refresh, kind)
        if kind == "updated":
            updated.append(row)
        else:
            skipped.append(row)
            if refresh.reason:
                errors.append(refresh.reason)
    if http_calls == 0 and not updated:
        rankings, comparison, views = _views(previous, fx, russian, now)
        return _outcome(
            DEFERRED, action="REVALIDATE", snapshot_id=previous.get("snapshot_id"),
            updated_offers=updated, skipped_offers=skipped, errors=errors, http_calls=0,
            next_allowed_at=_earliest(next_at), rankings=rankings, comparison=comparison,
            views=views, coverage=list(previous.get("source_coverage") or []),
        )
    saved, error = _save_revalidated(previous, root, now, barrier)
    if error:
        return _outcome(
            FAILED, action="REVALIDATE", snapshot_id=previous.get("snapshot_id"),
            updated_offers=updated, skipped_offers=skipped, errors=[error], http_calls=http_calls,
            next_allowed_at=_earliest(next_at),
        )
    rankings, comparison, views = _views(saved, fx, russian, now)
    status = COMPLETED if updated and not skipped else PARTIAL
    return _outcome(
        status, action="REVALIDATE", snapshot_id=saved.get("snapshot_id"),
        parent_snapshot_id=saved.get("parent_snapshot_id"), updated_offers=updated,
        skipped_offers=skipped, errors=errors, http_calls=http_calls,
        next_allowed_at=_earliest(next_at), rankings=rankings, comparison=comparison,
        views=views, coverage=list(saved.get("source_coverage") or []),
    )


def _discover(previous, decision, root, now, instance_id, health_path, russian, fx, transport, barrier, started, timeout):
    allowed = []
    skipped = []
    next_at = []
    enabled = {adapter.slug: adapter for adapter in get_thailand_adapters()}
    for source in decision.get("allowed_sources") or []:
        adapter = enabled.get(source)
        if adapter is None or not adapter.enabled or source in POLICY_DISABLED_SOURCES:
            skipped.append({"source": source, "reason": "source_disabled"})
            continue
        block, when = _source_block(source, {"enabled": True, "policy_disabled": False, "state": "ok"}, now, health_path)
        if block:
            skipped.append({"source": source, "reason": block, "next_allowed_at": when})
            if when:
                next_at.append(when)
            continue
        allowed.append(source)
    if not allowed:
        return _reuse_or_defer(previous, "DEFERRED", fx, russian, now) | {"skipped_offers": skipped, "next_allowed_at": _earliest(next_at)}
    results: list[StoreScanResult] = []
    http_calls = 0
    errors = []
    for adapter in get_thailand_adapters():
        if adapter.slug in allowed:
            continue
        results.append(_placeholder(adapter, now, health_path))
    for source in allowed:
        if _timed_out(started, timeout):
            skipped.append({"source": source, "reason": "deadline"})
            results.append(StoreScanResult(store=source, ok=False, error_code="TIMEOUT", technical_detail="deadline", collection_mode="failed"))
            continue
        block, when = _source_block(source, {"enabled": True, "policy_disabled": False, "state": "ok"}, now, health_path)
        if block:
            skipped.append({"source": source, "reason": block, "next_allowed_at": when})
            if when:
                next_at.append(when)
            results.append(_placeholder(enabled[source], now, health_path))
            continue
        try:
            result = transport.discover_source(source, now=now)
        except Exception as exc:  # noqa: BLE001 — one source must not erase last-good
            errors.append(f"{source}:{type(exc).__name__}")
            results.append(StoreScanResult(store=source, ok=False, error=type(exc).__name__, error_code="HTTP_ERROR", collection_mode="failed"))
            continue
        results.append(result)
        http_calls += _request_count(result)
        if _rate_limited(result):
            when = _remember_rate_limit(source, result, now, health_path)
            if when:
                next_at.append(when)
    if http_calls == 0 and not any(result.offers for result in results):
        outcome = _reuse_or_defer(previous, "DEFERRED", fx, russian, now)
        outcome["skipped_offers"] = skipped
        outcome["errors"] = errors
        outcome["next_allowed_at"] = _earliest(next_at)
        return outcome
    try:
        manifest = export_thai_catalog(
            results,
            snapshot_root=root,
            now=now,
            discovery_checked_at=now,
            instance_id=instance_id,
            parent_snapshot_id=_id(previous),
            publish_barrier=barrier,
        )
    except (ThaiCatalogError, OSError, ValueError) as exc:
        return _outcome(
            FAILED, action="DISCOVER", snapshot_id=_id(previous), errors=[*errors, f"publish_failed:{type(exc).__name__}"],
            http_calls=http_calls, skipped_offers=skipped, next_allowed_at=_earliest(next_at),
        )
    saved = load_thai_catalog(root, expected_instance_id=instance_id)
    rankings, comparison, views = _views(saved, fx, russian, now)
    partial = bool(skipped or errors or any(_rate_limited(result) or not result.ok for result in results if result.store in allowed))
    return _outcome(
        PARTIAL if partial else COMPLETED,
        action="DISCOVER",
        snapshot_id=manifest["snapshot_id"],
        parent_snapshot_id=saved.get("parent_snapshot_id"),
        skipped_offers=skipped,
        errors=errors,
        http_calls=http_calls,
        next_allowed_at=_earliest(next_at),
        rankings=rankings,
        comparison=comparison,
        views=views,
        coverage=list(saved.get("source_coverage") or []),
    )


def _apply_refresh(document: dict[str, Any], refresh: OfferRefresh) -> str:
    diagnostics = document.setdefault("refresh_diagnostics", [])
    offer = _find_offer(document, refresh)
    entry = {
        "source": refresh.source,
        "external_id": refresh.external_id,
        "variant_id": refresh.variant_id,
        "offer_id": refresh.offer_id,
        "status": refresh.status,
        "reason": refresh.reason,
        "coverage": refresh.coverage,
    }
    if offer is None or refresh.status in {"missing", "skipped", "failed", "unverified", "rate_limited"}:
        entry["timestamps_unchanged"] = True
        diagnostics.append(entry)
        return "skipped"
    if not refresh.variant_confirmed or refresh.status == "conflict":
        entry["status"] = "conflict"
        entry["reason"] = refresh.reason or "variant_not_confirmed"
        entry["timestamps_unchanged"] = True
        diagnostics.append(entry)
        return "skipped"
    conflicts = _spec_conflicts(offer, refresh.observed_specs)
    if conflicts:
        entry["spec_conflict"] = conflicts
    if refresh.observed_at is None or (refresh.price_thb is None and not refresh.availability_confirmed):
        entry["status"] = "unverified"
        entry["timestamps_unchanged"] = True
        diagnostics.append(entry)
        return "skipped"
    observed = refresh.observed_at.isoformat()
    if refresh.price_thb is not None:
        offer["price_thb"] = int(refresh.price_thb)
        offer["price_checked_at"] = observed
        offer["price_verified_for_variant"] = True
    if refresh.availability_confirmed and refresh.availability_state:
        offer["availability_state"] = refresh.availability_state
        offer["availability_confirmed"] = True
        offer["availability_checked_at"] = observed
        evidence = offer.get("availability_evidence")
        if isinstance(evidence, dict):
            evidence["captured_at"] = observed
            evidence["availability_state"] = refresh.availability_state
            evidence["available"] = refresh.availability_state in {"in_stock", "online_available", "store_pickup_only"}
    offer["observed_at"] = observed
    entry["status"] = "updated"
    entry["timestamps_unchanged"] = False
    diagnostics.append(entry)
    return "updated"


def _spec_conflicts(offer: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    conflicts = {}
    specs = offer.get("specs") or {}
    for name, value in observed.items():
        if value is None:
            continue
        current = (specs.get(name) or {}).get("value")
        if current is not None and str(current) != str(value):
            conflicts[name] = {"stored": current, "observed": value}
    return conflicts


def _find_offer(document: Mapping[str, Any], refresh: OfferRefresh) -> dict[str, Any] | None:
    for offer in document.get("offers") or []:
        if offer.get("source") != refresh.source or offer.get("external_id") != refresh.external_id:
            continue
        if refresh.variant_id is not None and offer.get("variant_id") != refresh.variant_id:
            continue
        if refresh.offer_id and offer.get("offer_id") not in {None, refresh.offer_id}:
            continue
        return offer
    return None


def _save_revalidated(document: dict[str, Any], root: Path, now: datetime, barrier) -> tuple[dict | None, str | None]:
    parent = document.get("snapshot_id")
    document["snapshot_id"] = str(uuid.uuid4())
    document["parent_snapshot_id"] = parent
    document["created_at"] = now.isoformat()
    document["finished_at"] = now.isoformat()
    limitations = list(document.get("catalog_limitations") or [])
    if "revalidate_not_broad_discovery" not in limitations:
        limitations.append("revalidate_not_broad_discovery")
    document["catalog_limitations"] = limitations
    try:
        _publish_document(root, document, barrier)
        saved = load_thai_catalog(root, expected_instance_id=document.get("instance_id"))
    except (ThaiCatalogError, OSError, ValueError) as exc:
        return None, f"publish_failed:{type(exc).__name__}"
    return saved, None


def _publish_document(root: Path, document: Mapping[str, Any], barrier) -> None:
    import json

    payload = json.loads(_dumps(document).decode("utf-8"))
    relative = f"{SNAPSHOT_DIRNAME}/{document['snapshot_id']}.json"
    path = root / relative
    atomic_write_json(path, payload)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (barrier or (lambda _point: None))("before_latest")
    atomic_write_json(root / LATEST_NAME, {
        "schema_version": document["schema_version"],
        "snapshot_id": document["snapshot_id"],
        "sha256": digest,
        "path": relative,
        "instance_id": document["instance_id"],
        "created_at": document["created_at"],
        "discovery_checked_at": document["discovery_checked_at"],
        "discovery_scope_hash": document["discovery_scope_hash"],
    })


def _jib_point(item, *, client, timeout: float, now: datetime) -> OfferRefresh:
    from thailand.jib import fetch_read_product

    owns = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        detail = fetch_read_product(http, str(item.get("external_id") or ""), timeout=timeout)
    finally:
        if owns:
            http.close()
    refresh = _base(item, coverage="point_check:jib_read_product", http_calls=1)
    error = str(detail.get("error") or "")
    if error == "HTTP_429":
        refresh.status = "rate_limited"
        refresh.reason = "retry_after"
        return refresh
    if error:
        refresh.status = "failed"
        refresh.reason = error
        refresh.observed_at = None
        return refresh
    refresh.observed_specs = {
        name: detail.get(name)
        for name in ("cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")
        if detail.get(name) is not None
    }
    if detail.get("screen_size_inch") is not None:
        refresh.observed_specs["screen_inch"] = detail.get("screen_size_inch")
    refresh.price_thb = detail.get("price_thb")
    refresh.availability_state = detail.get("availability_status")
    refresh.availability_confirmed = bool(detail.get("availability_confirmed"))
    if refresh.price_thb or refresh.availability_confirmed:
        refresh.status = "updated"
        refresh.observed_at = now
    else:
        refresh.status = "unverified"
        refresh.reason = "price_and_availability_unconfirmed"
    return refresh


def _bounded_request(item, *, client, timeout: float, now: datetime) -> OfferRefresh:
    source = str(item.get("source") or "")
    url = _bounded_url(source, item)
    coverage = f"bounded_source_refresh:{source}:one_request"
    if url is None:
        return _skipped(
            item,
            "bounded_source_refresh_unavailable",
            coverage="itcity has no single-product request; broad search was not started",
        )
    owns = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        response = http.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        refresh = _base(item, coverage=coverage, http_calls=1)
        refresh.status = "failed"
        refresh.reason = type(exc).__name__
        return refresh
    finally:
        if owns:
            http.close()
    refresh = _base(item, coverage=coverage, http_calls=1)
    if response.status_code == 429:
        refresh.status = "rate_limited"
        refresh.reason = "retry_after"
        refresh.retry_after_seconds = _retry_after(response)
        return refresh
    if response.status_code >= 400:
        refresh.status = "failed"
        refresh.reason = f"HTTP_{response.status_code}"
        return refresh
    observed = _parse_bounded(source, response, item, now)
    if observed is None:
        refresh.status = "missing"
        refresh.reason = "not_observed_on_recheck"
        return refresh
    refresh.price_thb = observed.price_thb
    refresh.availability_state = observed.availability_state
    refresh.availability_confirmed = observed.availability_confirmed
    refresh.observed_specs = observed.observed_specs
    refresh.variant_confirmed = observed.variant_confirmed
    if observed.price_thb or observed.availability_confirmed:
        refresh.status = "updated"
        refresh.observed_at = now
    else:
        refresh.status = "unverified"
        refresh.reason = "bounded_response_did_not_confirm_quote"
    return refresh


def _bounded_url(source: str, item: Mapping[str, Any]) -> str | None:
    identity = str(item.get("external_id") or item.get("variant_id") or "").strip()
    if not identity:
        return None
    if source == "advice":
        from thailand.advice import BASE

        return f"{BASE}/search?keyword={quote(identity)}"
    if source == "speedcom":
        from thailand.speedcom import BASE

        return (
            f"{BASE}/search/suggest.json?q={quote(identity)}"
            "&resources[type]=product&resources[limit]=5"
        )
    if source == "invadeit":
        from thailand.invadeit import BASE

        return f"{BASE}/api/products/{quote(identity)}/specifications"
    return None


def _parse_bounded(source: str, response, item, now: datetime) -> OfferRefresh | None:
    if source == "advice":
        from thailand.advice import parse_advice_html, parse_advice_json_payload

        content = (response.headers.get("content-type") or "").lower()
        try:
            offers = parse_advice_json_payload(response.json(), collected_at=now) if "json" in content else []
        except ValueError:
            offers = []
        if not offers:
            offers = parse_advice_html(response.text, collected_at=now)
        return _match_offer(offers, item)
    if source == "speedcom":
        from thailand.speedcom import parse_speedcom_product

        try:
            payload = response.json()
        except ValueError:
            return None
        products = ((payload.get("resources") or {}).get("results") or {}).get("products") or payload.get("products") or []
        offers = []
        for product in products:
            if isinstance(product, dict):
                offers.extend(parse_speedcom_product(product, collected_at=now))
        return _match_offer(offers, item)
    if source == "invadeit":
        return OfferRefresh(source="invadeit", external_id=str(item.get("external_id") or ""), status="unverified")
    return None


def _match_offer(offers, item) -> OfferRefresh | None:
    wanted = str(item.get("external_id") or "")
    variant = item.get("variant_id")
    for offer in offers:
        if offer.external_id != wanted:
            continue
        if variant is not None and offer.variant_id not in {None, variant}:
            continue
        return OfferRefresh(
            source=offer.store,
            external_id=offer.external_id,
            variant_id=offer.variant_id,
            price_thb=offer.price_thb,
            availability_state=offer.availability_status,
            availability_confirmed=bool(offer.availability_confirmed),
            observed_specs={
                "cpu": offer.cpu,
                "ram_gb": offer.ram_gb,
                "ssd_gb": offer.ssd_gb,
                "screen_inch": offer.screen_size_inch,
                "screen_resolution": offer.screen_resolution,
            },
            variant_confirmed=True,
        )
    return None


def _placeholder(adapter, now: datetime, health_path) -> StoreScanResult:
    if not adapter.enabled:
        return StoreScanResult(store=adapter.slug, ok=True, policy_disabled=True, collection_mode="disabled", circuit_breaker_status="closed")
    reason, _when = _source_block(adapter.slug, {"enabled": True, "policy_disabled": False, "state": "ok"}, now, health_path)
    if reason == "circuit":
        return StoreScanResult(store=adapter.slug, ok=True, collection_mode="skipped", circuit_breaker_status="open")
    if reason in {"retry_after", "rate_limit"}:
        return StoreScanResult(store=adapter.slug, ok=True, error_code=RATE_LIMITED, collection_mode="rate_limited")
    return StoreScanResult(store=adapter.slug, ok=False, error_code="NOT_ATTEMPTED", collection_mode="skipped")


def _remember_rate_limit(source: str, refresh, now: datetime, health_path) -> str | None:
    seconds = getattr(refresh, "retry_after_seconds", None)
    record_observation(
        source,
        ok=False,
        error_code=RATE_LIMITED,
        retry_after_seconds=seconds if isinstance(seconds, int) and seconds > 0 else None,
        now=now,
        path=health_path,
    )
    return _source_block(source, {"enabled": True, "policy_disabled": False, "state": "ok"}, now, health_path)[1]


def _views(document, fx, russian_snapshot, now: datetime):
    rankings = rankings_from_catalog(document, fx, now=now)
    comparison = None
    views = None
    if russian_snapshot is not None and fx is not None:
        comparison = comparison_from_catalog(document, russian_snapshot, fx, now=now)
        views = {
            "all": list(comparison_view(comparison, "all")),
            "general_top": list(comparison_view(comparison, "general_top")),
            "personal_top3": list(comparison_view(comparison, "personal_top3")),
        }
    return rankings, comparison, views


def _outcome(status: str, **kwargs) -> dict[str, Any]:
    result = {
        "status": status,
        "action": kwargs.get("action"),
        "snapshot_id": kwargs.get("snapshot_id"),
        "parent_snapshot_id": kwargs.get("parent_snapshot_id"),
        "http_calls": int(kwargs.get("http_calls") or 0),
        "updated_offers": list(kwargs.get("updated_offers") or []),
        "skipped_offers": list(kwargs.get("skipped_offers") or []),
        "coverage": list(kwargs.get("coverage") or []),
        "rankings": kwargs.get("rankings"),
        "comparison": kwargs.get("comparison"),
        "views": kwargs.get("views"),
        "errors": list(kwargs.get("errors") or []),
        "next_allowed_at": kwargs.get("next_allowed_at"),
    }
    if kwargs.get("detail"):
        result["errors"].append(kwargs["detail"])
    return result


def _base(item, *, coverage: str, http_calls: int) -> OfferRefresh:
    return OfferRefresh(
        source=str(item.get("source") or ""),
        external_id=str(item.get("external_id") or ""),
        variant_id=item.get("variant_id"),
        offer_id=item.get("offer_id"),
        coverage=coverage,
        http_calls=http_calls,
    )


def _skipped(item, reason: str, when: str | None = None, coverage: str = "") -> OfferRefresh:
    del when
    refresh = _base(item, coverage=coverage, http_calls=0)
    refresh.status = "skipped"
    refresh.reason = reason
    return refresh


def _skip(item, reason: str, when: str | None = None) -> dict[str, Any]:
    return {
        "source": item.get("source"),
        "external_id": item.get("external_id"),
        "variant_id": item.get("variant_id"),
        "offer_id": item.get("offer_id"),
        "reason": reason,
        "next_allowed_at": when,
    }


def _row(refresh: OfferRefresh, kind: str) -> dict[str, Any]:
    return {
        "source": refresh.source,
        "external_id": refresh.external_id,
        "variant_id": refresh.variant_id,
        "offer_id": refresh.offer_id,
        "reason": refresh.reason or kind,
        "coverage": refresh.coverage,
    }


def _clock(now: datetime) -> datetime:
    parsed = moment(now)
    if parsed is None:
        raise ValueError("explicit UTC timestamp required")
    return parsed


def _id(document) -> str | None:
    return None if document is None else document.get("snapshot_id")


def _earliest(values: Sequence[str]) -> str | None:
    parsed = [item for item in (moment(value) for value in values) if item is not None]
    if not parsed:
        return None
    return min(parsed).isoformat()


def _timed_out(started: float, timeout: float) -> bool:
    return time.monotonic() - started >= timeout


def _request_count(result: StoreScanResult) -> int:
    if isinstance(result.request_count, int) and not isinstance(result.request_count, bool):
        return result.request_count
    return 1 if result.collection_mode == "http" else 0


def _rate_limited(result: StoreScanResult) -> bool:
    return result.error_code == RATE_LIMITED or result.collection_mode == "rate_limited" or bool(result.discovery_rate_limited)


def _retry_after(response) -> int | None:
    raw = response.headers.get("Retry-After") if response.headers else None
    if not raw:
        return None
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return None
