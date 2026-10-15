"""Execute a C2.2a catalog decision. Queue, Telegram, and schedules stay outside.

REUSE never calls a store. REVALIDATE refreshes only the named offers: JIB by
its product page, and Advice, SpeedCom, InvadeIT, and IT City by one bounded
request. DISCOVER is the only action that runs one broad adapter pass.
A circuit breaker, Retry-After, or a disabled store is checked again immediately
before every HTTP call.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urljoin

import httpx

import config
from cross_market_comparison import comparison_view
from market_contracts import moment
from thai_catalog import (
    LATEST_NAME,
    SNAPSHOT_DIRNAME,
    ThaiCatalogError,
    _diagnostics,
    _dumps,
    _limitations,
    _raw_observation,
    _variants,
    build_thai_catalog,
    comparison_from_catalog,
    load_thai_catalog,
    rankings_from_catalog,
)
from thailand.discovery import QUERY_BUDGET
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
# Backstop for a runaway loop. Adapters keep their own search and detail caps.
DEFAULT_REQUEST_BUDGET = QUERY_BUDGET + 24
_FULL_ID = re.compile(r"[A-Z0-9][A-Z0-9._/\-]*")
_BARE_PLATFORM = re.compile(r"(?:G\d{3}[A-Z]{1,4}|\d{2}[A-Z]{3}\d{2}H|[A-Z]\d[A-Z]{4,6})")


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
    sku: str | None = None
    manufacturer_part_number: str | None = None
    variant_confirmed: bool = False
    transport_failure: bool = False
    store_unavailable: bool = False


class ManualClock:
    """Test clock. ``advance`` moves the instant; nothing here rewrites observations."""

    def __init__(self, instant: datetime) -> None:
        self.instant = _clock(instant)

    def now(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> None:
        self.instant = self.instant + delta


class PassTiming:
    """UTC instants for observations. Deadline uses monotonic time unless a test clock is injected."""

    def __init__(self, clock: ManualClock | None, timeout: float, started_utc: datetime) -> None:
        self.clock = clock
        self.timeout = timeout
        self.started_utc = started_utc
        self.started_mono = time.monotonic()

    def now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def expired(self) -> bool:
        if self.clock is not None:
            return self.now() >= self.started_utc + timedelta(seconds=self.timeout)
        return time.monotonic() - self.started_mono >= self.timeout

    def remaining(self) -> float:
        if self.clock is not None:
            left = (self.started_utc + timedelta(seconds=self.timeout) - self.now()).total_seconds()
        else:
            left = self.timeout - (time.monotonic() - self.started_mono)
        return max(0.0, left)


class GuardedClient:
    """One shared budget per source. Every attempt, including a redirect hop, is counted before I/O."""

    def __init__(
        self,
        inner: httpx.Client,
        *,
        source: str,
        timing: PassTiming,
        health_path,
        budget: int,
    ) -> None:
        self.inner = inner
        self.source = source
        self.timing = timing
        self.health_path = health_path
        self.budget = budget
        self.http_calls = 0
        self.successes = 0
        self.stopped = False
        self.real_429 = False
        self.blocked_reason: str | None = None
        self.retry_after_seconds: int | None = None
        self.last_transport_error: str | None = None

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        current = url
        verb = str(method or "GET").upper()
        payload = dict(kwargs)
        payload.pop("follow_redirects", None)
        for _hop in range(5):
            response = self._once(verb, current, payload)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                return response
            current = urljoin(str(current), location)
            if response.status_code in {301, 302, 303}:
                verb = "GET"
                payload = {key: value for key, value in payload.items() if key not in {"content", "data", "files", "json"}}
        return response

    def _once(self, method, url, kwargs):
        self.last_transport_error = None
        reason = self._block_reason()
        if reason:
            self.blocked_reason = self.blocked_reason or reason
            return httpx.Response(429, headers={"X-Block-Reason": reason})
        self.http_calls += 1
        timeout = self.timing.remaining()
        requested = kwargs.get("timeout")
        if requested is not None:
            try:
                timeout = min(timeout, float(requested))
            except (TypeError, ValueError):
                pass
        now = self.timing.now()
        if timeout <= 0:
            self.blocked_reason = "deadline"
            self._record(ok=False, error_code="TIMEOUT", now=now)
            return httpx.Response(429, headers={"X-Block-Reason": "deadline"})
        request = {key: value for key, value in kwargs.items() if key != "timeout"}
        if method != "GET" and not callable(getattr(self.inner, method.lower(), None)):
            raise AttributeError(method)
        try:
            parameters = inspect.signature(self.inner.get if method == "GET" else getattr(self.inner, method.lower(), self.inner.request)).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_any = any(getattr(item, "kind", None) == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        if accepts_any or "follow_redirects" in parameters:
            request["follow_redirects"] = False
        try:
            if method == "GET":
                response = self.inner.get(url, timeout=timeout, **request)
            else:
                response = getattr(self.inner, method.lower())(url, timeout=timeout, **request)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self.last_transport_error = type(exc).__name__
            self._record(ok=False, error_code=self.last_transport_error, now=self.timing.now())
            raise
        now = self.timing.now()
        if response.status_code == 429:
            self.real_429 = True
            self.stopped = True
            self.retry_after_seconds = _retry_after_value(response.headers.get("Retry-After"), now)
            self._record(ok=False, error_code=RATE_LIMITED, now=now, retry_after_seconds=self.retry_after_seconds)
            return response
        if response.status_code >= 400:
            self._record(ok=False, error_code=f"HTTP_{response.status_code}", now=now)
            return response
        if response.status_code not in {301, 302, 303, 307, 308}:
            self.successes += 1
            self._record(ok=True, error_code=None, now=now)
        return response

    def close(self) -> None:
        return None

    def _record(self, *, ok: bool, error_code: str | None, now: datetime, retry_after_seconds: int | None = None) -> None:
        record_observation(
            self.source,
            ok=ok,
            error_code=error_code,
            retry_after_seconds=retry_after_seconds,
            now=now,
            path=self.health_path,
        )

    def _block_reason(self) -> str | None:
        if self.stopped or self.real_429:
            return "stopped_after_429"
        if not _source_enabled(self.source):
            return "source_disabled"
        if self.timing.expired():
            return "deadline"
        if self.budget <= 0 or self.http_calls >= self.budget:
            return "budget"
        block, _when = _source_block(
            self.source,
            {"enabled": True, "policy_disabled": False, "state": "ok"},
            self.timing.now(),
            self.health_path,
        )
        if block:
            return block
        return None


class LiveCatalogTransport:
    """Store calls used when a test does not inject its own transport.

    REVALIDATE never calls ``collect``. DISCOVER calls one adapter ``collect``
    per allowed source and does not receive a Russian TOP. Every ``client.get``
    inside that collect passes through ``GuardedClient``.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        *,
        request_budget: int | None = None,
    ) -> None:
        self.client = client
        self.timeout = timeout
        self.request_budget = request_budget if request_budget is not None else DEFAULT_REQUEST_BUDGET
        self.clock: ManualClock | None = None
        self.health_path = None
        self.deadline_at: datetime | None = None
        self.fx_rate = None
        self.calls: list[tuple[str, str, str | None]] = []
        self.guards: dict[str, GuardedClient] = {}
        self.timing: PassTiming | None = None

    def refresh_offer(self, item: Mapping[str, Any], *, mode: str, now: datetime, clock: ManualClock | None = None) -> OfferRefresh:
        del now
        source = str(item.get("source") or "")
        external_id = str(item.get("external_id") or "")
        self.calls.append((mode, source, external_id))
        if source in POLICY_DISABLED_SOURCES or mode == "refused":
            return _skipped(item, "source_disabled", coverage="disabled source was not requested")
        guard = self._guard(source)
        active = clock or self.timing or self.clock
        if mode == "point" and source in POINT_CHECK_SOURCES:
            return _jib_point(item, client=guard, timeout=self.timeout, clock=active)
        if mode == "bounded" and source in BOUNDED_SOURCE_REFRESH_SOURCES:
            return _bounded_request(item, client=guard, timeout=self.timeout, clock=active)
        return _skipped(item, "refresh_mode_refused", coverage="broad search was not started")

    def discover_source(self, source: str, *, now: datetime) -> StoreScanResult:
        del now
        self.calls.append(("discover", source, None))
        if source in POLICY_DISABLED_SOURCES or not _source_enabled(source):
            raise ThaiCatalogError(f"disabled source is not discoverable: {source}")
        adapter = next(item for item in get_thailand_adapters() if item.slug == source)
        guard = self._guard(source)
        kwargs: dict[str, Any] = {}
        signature = inspect.signature(adapter.collect)
        accepts_any = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
        if "timeout" in signature.parameters or accepts_any:
            kwargs["timeout"] = self.timeout
        if "client" in signature.parameters or accepts_any:
            kwargs["client"] = guard
        if ("fx" in signature.parameters or accepts_any) and self.fx_rate is not None and source == "invadeit":
            kwargs["fx"] = self.fx_rate
        result = adapter.collect(**kwargs)
        return _finish_guarded_result(result, guard)

    def _guard(self, source: str) -> GuardedClient:
        existing = self.guards.get(source)
        if existing is not None:
            return existing
        if self.client is None or self.timing is None:
            raise ThaiCatalogError("live transport requires a client and pass timing")
        guard = GuardedClient(
            self.client,
            source=source,
            timing=self.timing,
            health_path=self.health_path,
            budget=self.request_budget,
        )
        self.guards[source] = guard
        return guard


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
    clock: ManualClock | None = None,
) -> dict[str, Any]:
    """Run one reuse decision. Returns the orchestration result and does not notify."""
    started_utc = clock.now() if clock is not None else _clock(now)
    root = Path(snapshot_root)
    action = str(decision.get("action") or "")
    try:
        previous = load_thai_catalog(root, expected_instance_id=instance_id)
    except (ThaiCatalogError, OSError, ValueError) as exc:
        if action != "DISCOVER":
            return _outcome(
                FAILED, action=action, errors=[f"catalog_unusable:{type(exc).__name__}"],
                detail=str(exc), pass_started_at=started_utc.isoformat(),
            )
        previous = None
    bound = decision.get("snapshot_id")
    if previous is not None and bound and bound != previous.get("snapshot_id"):
        probe = clock or ManualClock(started_utc)
        return _refuse_stale_decision(
            root, previous, probe, instance_id, health_path, russian_snapshot, fx, started_utc,
        )
    if action in {"REUSE", "DEFERRED"} or not decision.get("refresh_allowed"):
        observed = clock.now() if clock is not None else datetime.now(timezone.utc)
        return _reuse_or_defer(previous, action, fx, russian_snapshot, observed, started_utc)
    timeout = float(
        overall_timeout_seconds
        if overall_timeout_seconds is not None
        else config.THAILAND_OVERALL_TIMEOUT_SECONDS
    )
    timing = PassTiming(clock, timeout, started_utc)
    owned_client = None
    actor = transport
    if actor is None:
        owned_client = httpx.Client(follow_redirects=False)
        actor = LiveCatalogTransport(client=owned_client)
    if isinstance(actor, LiveCatalogTransport):
        actor.clock = clock
        actor.timing = timing
        actor.health_path = health_path
        actor.deadline_at = started_utc + timedelta(seconds=timeout)
        actor.fx_rate = _fx_rate(fx, started_utc)
    try:
        if action == "REVALIDATE":
            return _revalidate(
                previous, decision, root, timing, instance_id, health_path, russian_snapshot, fx,
                actor, publish_barrier, timeout, started_utc,
            )
        if action == "DISCOVER":
            return _discover(
                previous, decision, root, timing, instance_id, health_path, russian_snapshot, fx,
                actor, publish_barrier, timeout, started_utc,
            )
        return _outcome(
            FAILED, action=action, errors=[f"unknown_action:{action}"],
            snapshot_id=_id(previous), pass_started_at=started_utc.isoformat(),
        )
    finally:
        if owned_client is not None:
            owned_client.close()


def _parsed_check(refresh: OfferRefresh, kind: str) -> bool:
    """A parsed page or a confirmed absence. A 500/503 or a network error is not one."""
    if refresh.transport_failure or refresh.store_unavailable:
        return False
    if kind == "conflict" or refresh.status in {"missing", "unverified", "conflict"}:
        return True
    return False


def _reuse_or_defer(document, action, fx, russian_snapshot, now: datetime, pass_started: datetime | None = None) -> dict[str, Any]:
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
        pass_started_at=(pass_started or now).isoformat(),
        published_at=None,
    )


def _refuse_stale_decision(root, previous, timer, instance_id, health_path, russian_snapshot, fx, pass_started):
    from thailand.catalog_reuse import evaluate_catalog_reuse

    replanned = evaluate_catalog_reuse(
        root,
        now=timer.now(),
        instance_id=instance_id,
        health_path=health_path,
        russian_snapshot=russian_snapshot,
        fx=fx,
    )
    return _outcome(
        FAILED,
        action=replanned.get("action"),
        snapshot_id=previous.get("snapshot_id"),
        errors=["snapshot_changed"],
        http_calls=0,
        pass_started_at=pass_started.isoformat(),
        replanned_decision=replanned,
    )


def _call_refresh(transport, item, mode: str, timer: PassTiming) -> OfferRefresh:
    kwargs: dict[str, Any] = {"mode": mode, "now": timer.now()}
    if "clock" in inspect.signature(transport.refresh_offer).parameters:
        kwargs["clock"] = timer
    return transport.refresh_offer(item, **kwargs)


def _revalidate(previous, decision, root, timer: PassTiming, instance_id, health_path, russian, fx, transport, barrier, timeout, pass_started):
    del instance_id
    if previous is None:
        return _outcome(FAILED, action="REVALIDATE", errors=["catalog_missing"], pass_started_at=pass_started.isoformat())
    saved_id = previous.get("snapshot_id")
    updated, skipped, checked, errors, http_calls, next_at = [], [], [], [], 0, []
    transport_failures: list[str] = []
    stopped: set[str] = set()
    coverage_rows = {row.get("source"): row for row in previous.get("source_coverage") or []}
    del timeout
    for item in decision.get("revalidate_offers") or []:
        source = str(item.get("source") or "")
        if timer.expired():
            skipped.append(_skip(item, "deadline"))
            continue
        if source in stopped:
            skipped.append(_skip(item, "stopped_after_429"))
            continue
        block, when = _source_block(source, coverage_rows.get(source), timer.now(), health_path)
        if block or source in POLICY_DISABLED_SOURCES or source not in POINT_CHECK_SOURCES | BOUNDED_SOURCE_REFRESH_SOURCES:
            reason = block or "source_disabled"
            skipped.append(_skip(item, reason, when))
            if when:
                next_at.append(when)
            continue
        if not _source_enabled(source):
            skipped.append(_skip(item, "source_disabled"))
            continue
        mode = "point" if source in POINT_CHECK_SOURCES else "bounded"
        refresh = _call_refresh(transport, item, mode, timer)
        http_calls += int(refresh.http_calls or 0)
        if refresh.http_calls:
            _note_attempt(previous, source, response_at=refresh.observed_at or timer.now(), http_calls=refresh.http_calls, mode=mode)
        if refresh.status == "rate_limited":
            stopped.add(source)
            when = _remember_rate_limit(source, refresh, timer.now(), health_path)
            skipped.append(_skip(item, "retry_after", when))
            if when:
                next_at.append(when)
            continue
        if refresh.status == "failed" and not refresh.http_calls and not refresh.store_unavailable:
            skipped.append(_skip(item, refresh.reason or "failed"))
            continue
        if refresh.transport_failure or refresh.store_unavailable:
            transport_failures.append(refresh.reason or "unavailable")
            skipped.append(_skip(item, refresh.reason or "unavailable"))
            errors.append(refresh.reason or "unavailable")
            continue
        kind = _apply_refresh(previous, refresh)
        row = _row(refresh, kind)
        if kind == "updated":
            updated.append(row)
        elif _parsed_check(refresh, kind):
            checked.append(row)
            skipped.append(row)
            if refresh.reason and kind != "conflict":
                errors.append(refresh.reason)
        else:
            skipped.append(row)
            if refresh.reason:
                errors.append(refresh.reason)
    if http_calls and not updated and not checked and transport_failures:
        return _outcome(
            FAILED, action="REVALIDATE", snapshot_id=saved_id,
            updated_offers=[], skipped_offers=skipped, errors=errors,
            http_calls=http_calls, next_allowed_at=_earliest(next_at),
            pass_started_at=pass_started.isoformat(),
        )
    if http_calls == 0 and not updated:
        rankings, comparison, views = _views(previous, fx, russian, timer.now())
        return _outcome(
            DEFERRED, action="REVALIDATE", snapshot_id=saved_id,
            updated_offers=updated, skipped_offers=skipped, errors=errors, http_calls=0,
            next_allowed_at=_earliest(next_at), rankings=rankings, comparison=comparison,
            views=views, coverage=list(previous.get("source_coverage") or []),
            pass_started_at=pass_started.isoformat(),
        )
    published_at = timer.now()
    saved, error, current_id = _save_revalidated(previous, root, published_at, barrier, saved_id)
    if error:
        return _outcome(
            FAILED, action="REVALIDATE", snapshot_id=current_id,
            updated_offers=updated, skipped_offers=skipped, errors=[error], http_calls=http_calls,
            next_allowed_at=_earliest(next_at), pass_started_at=pass_started.isoformat(),
        )
    rankings, comparison, views = _views(saved, fx, russian, published_at)
    status = COMPLETED if updated and not skipped else PARTIAL
    return _outcome(
        status, action="REVALIDATE", snapshot_id=saved.get("snapshot_id"),
        parent_snapshot_id=saved.get("parent_snapshot_id"), updated_offers=updated,
        skipped_offers=skipped, errors=errors, http_calls=http_calls,
        next_allowed_at=_earliest(next_at), rankings=rankings, comparison=comparison,
        views=views, coverage=list(saved.get("source_coverage") or []),
        pass_started_at=pass_started.isoformat(), published_at=published_at.isoformat(),
    )


def _discover(previous, decision, root, timer: PassTiming, instance_id, health_path, russian, fx, transport, barrier, timeout, pass_started):
    allowed = []
    skipped = []
    next_at = []
    enabled = {adapter.slug: adapter for adapter in get_thailand_adapters()}
    for source in decision.get("allowed_sources") or []:
        adapter = enabled.get(source)
        if adapter is None or not adapter.enabled or source in POLICY_DISABLED_SOURCES:
            skipped.append({"source": source, "reason": "source_disabled"})
            continue
        block, when = _source_block(source, {"enabled": True, "policy_disabled": False, "state": "ok"}, timer.now(), health_path)
        if block:
            skipped.append({"source": source, "reason": block, "next_allowed_at": when})
            if when:
                next_at.append(when)
            continue
        allowed.append(source)
    if not allowed:
        outcome = _reuse_or_defer(previous, "DEFERRED", fx, russian, timer.now(), pass_started)
        outcome["skipped_offers"] = skipped
        outcome["next_allowed_at"] = _earliest(next_at)
        return outcome
    results: list[StoreScanResult] = []
    http_calls = 0
    errors = []
    del timeout
    for adapter in get_thailand_adapters():
        if adapter.slug in allowed:
            continue
        results.append(_placeholder(adapter, timer.now(), health_path))
    for source in allowed:
        if timer.expired():
            skipped.append({"source": source, "reason": "deadline"})
            results.append(StoreScanResult(store=source, ok=False, error_code="TIMEOUT", technical_detail="deadline", collection_mode="skipped"))
            continue
        block, when = _source_block(source, {"enabled": True, "policy_disabled": False, "state": "ok"}, timer.now(), health_path)
        if block or not _source_enabled(source):
            reason = block or "source_disabled"
            skipped.append({"source": source, "reason": reason, "next_allowed_at": when})
            if when:
                next_at.append(when)
            results.append(_placeholder(enabled[source], timer.now(), health_path))
            continue
        try:
            result = transport.discover_source(source, now=timer.now())
        except Exception as exc:  # noqa: BLE001 — one source must not erase last-good
            errors.append(f"{source}:{type(exc).__name__}")
            results.append(StoreScanResult(store=source, ok=False, error=type(exc).__name__, error_code="HTTP_ERROR", collection_mode="failed"))
            continue
        results.append(result)
        http_calls += _request_count(result)
        if _rate_limited(result):
            when = _remember_rate_limit(source, result, timer.now(), health_path)
            if when:
                next_at.append(when)
    attempted = [result for result in results if result.store in allowed]
    if attempted and all(_attempt_kind(result) == "failed" for result in attempted):
        return _outcome(
            FAILED, action="DISCOVER", snapshot_id=_id(previous),
            errors=errors or ["transport_failed"], http_calls=http_calls,
            skipped_offers=skipped, next_allowed_at=_earliest(next_at),
            pass_started_at=pass_started.isoformat(),
        )
    if http_calls == 0 and not any(result.offers for result in results):
        outcome = _reuse_or_defer(previous, "DEFERRED", fx, russian, timer.now(), pass_started)
        outcome["skipped_offers"] = skipped
        outcome["errors"] = errors
        outcome["next_allowed_at"] = _earliest(next_at)
        return outcome
    if previous is None:
        previous = {"snapshot_id": None, "offers": [], "source_coverage": [], "discovery_checked_at": None, "schema_version": 1}
    response_at = timer.now()
    published_at = timer.now()
    try:
        document = _merge_discover(previous, results, published_at=published_at, response_at=response_at, instance_id=instance_id)
        _publish_document(root, document, barrier)
        saved = load_thai_catalog(root, expected_instance_id=instance_id)
    except (ThaiCatalogError, OSError, ValueError) as exc:
        return _outcome(
            FAILED, action="DISCOVER", snapshot_id=_id(previous),
            errors=[*errors, f"publish_failed:{type(exc).__name__}"],
            http_calls=http_calls, skipped_offers=skipped, next_allowed_at=_earliest(next_at),
            pass_started_at=pass_started.isoformat(),
        )
    rankings, comparison, views = _views(saved, fx, russian, published_at)
    partial = any(_attempt_kind(result) != "checked" for result in attempted) or bool(skipped or errors)
    return _outcome(
        PARTIAL if partial else COMPLETED,
        action="DISCOVER",
        snapshot_id=saved.get("snapshot_id"),
        parent_snapshot_id=saved.get("parent_snapshot_id"),
        skipped_offers=skipped,
        errors=errors,
        http_calls=http_calls,
        next_allowed_at=_earliest(next_at),
        rankings=rankings,
        comparison=comparison,
        views=views,
        coverage=list(saved.get("source_coverage") or []),
        pass_started_at=pass_started.isoformat(),
        published_at=published_at.isoformat(),
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
        "purchase_quote_updated": False,
    }
    if offer is None or refresh.status in {"missing", "skipped", "failed", "unverified", "rate_limited"}:
        entry["timestamps_unchanged"] = True
        diagnostics.append(entry)
        return "skipped"
    problem = _quote_blocked(offer, refresh)
    if problem:
        _store_separate_observation(document, offer, refresh, problem)
        entry["status"] = "conflict"
        entry["reason"] = problem["reason"]
        entry["timestamps_unchanged"] = True
        entry["purchase_quote_updated"] = False
        diagnostics.append(entry)
        return "conflict"
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
    entry["purchase_quote_updated"] = True
    diagnostics.append(entry)
    return "updated"


def _quote_blocked(offer: Mapping[str, Any], refresh: OfferRefresh) -> dict[str, Any] | None:
    """Return why this observation must not replace the stored purchase quote."""
    stored_variant = offer.get("variant_id")
    observed_variant = refresh.variant_id
    shared = _shared_full_ids(offer, refresh)
    if stored_variant and observed_variant and stored_variant != observed_variant:
        reason = "variant_mismatch"
    elif not (stored_variant and observed_variant and stored_variant == observed_variant) and not shared:
        # variant_id None on either side is not proof, and neither is a bare external_id.
        reason = "variant_unconfirmed"
    elif _identifier_conflict(offer, refresh):
        reason = "identifier_conflict"
    else:
        reason = None
    conflicts = _spec_conflicts(offer, refresh.observed_specs)
    if conflicts:
        reason = "spec_conflict"
    if reason is None:
        return None
    return {"reason": reason, "conflicts": conflicts, "shared_identifiers": sorted(shared)}


def _store_separate_observation(document: dict[str, Any], offer: Mapping[str, Any], refresh: OfferRefresh, problem: dict[str, Any]) -> None:
    document.setdefault("conflicting_observations", []).append({
        "source": refresh.source,
        "external_id": refresh.external_id,
        "variant_id": refresh.variant_id,
        "offer_id": offer.get("offer_id"),
        "sku": refresh.sku,
        "manufacturer_part_number": refresh.manufacturer_part_number,
        "price_thb": refresh.price_thb,
        "availability_state": refresh.availability_state,
        "availability_confirmed": refresh.availability_confirmed,
        "observed_at": refresh.observed_at.isoformat() if refresh.observed_at else None,
        "observed_specs": dict(refresh.observed_specs),
        "reason": problem["reason"],
        "conflicts": problem["conflicts"],
        "purchase_quote": False,
        "stored_price_thb": offer.get("price_thb"),
        "stored_price_checked_at": offer.get("price_checked_at"),
    })


def _spec_conflicts(offer: Mapping[str, Any], observed: Mapping[str, Any]) -> dict[str, Any]:
    conflicts = {}
    specs = offer.get("specs") or {}
    for name, value in observed.items():
        if value is None or name not in {"cpu", "gpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution"}:
            continue
        current = (specs.get(name) or {}).get("value")
        if current is None:
            continue
        if _same_spec(name, current, value):
            continue
        conflicts[name] = {"stored": current, "observed": value}
    return conflicts


def _same_spec(name: str, stored, observed) -> bool:
    if name in {"ram_gb", "ssd_gb"}:
        return str(stored) == str(observed)
    if name == "screen_inch":
        return str(stored) == str(observed)
    return " ".join(str(stored).upper().split()) == " ".join(str(observed).upper().split())


def _full_identifier(value: object) -> str | None:
    if value is None:
        return None
    code = "".join(str(value).upper().split())
    if not code or _BARE_PLATFORM.fullmatch(code):
        return None
    if _FULL_ID.fullmatch(code) and len(code) >= 6 and any(c.isdigit() for c in code) and any(c.isalpha() for c in code):
        return code
    return None


def _id_set(offer: Mapping[str, Any], refresh: OfferRefresh | None = None) -> set[str]:
    values = [
        offer.get("sku"),
        offer.get("manufacturer_part_number"),
        *(offer.get("sku_candidates") or []),
        *(offer.get("mpn_candidates") or []),
    ]
    if refresh is not None:
        values.extend([refresh.sku, refresh.manufacturer_part_number])
    return {code for code in (_full_identifier(value) for value in values) if code}


def _shared_full_ids(offer: Mapping[str, Any], refresh: OfferRefresh) -> set[str]:
    stored = _id_set(offer)
    observed = {code for code in (_full_identifier(value) for value in (refresh.sku, refresh.manufacturer_part_number)) if code}
    return stored & observed


def _identifier_conflict(offer: Mapping[str, Any], refresh: OfferRefresh) -> bool:
    stored = _id_set(offer)
    observed = {code for code in (_full_identifier(value) for value in (refresh.sku, refresh.manufacturer_part_number)) if code}
    return bool(stored and observed and not (stored & observed))


def _find_offer(document: Mapping[str, Any], refresh: OfferRefresh) -> dict[str, Any] | None:
    rows = [
        offer for offer in document.get("offers") or []
        if offer.get("source") == refresh.source and offer.get("external_id") == refresh.external_id
    ]
    if refresh.offer_id:
        exact = [offer for offer in rows if offer.get("offer_id") == refresh.offer_id]
        if exact:
            rows = exact
    if refresh.variant_id:
        same = [offer for offer in rows if offer.get("variant_id") == refresh.variant_id]
        if same:
            return same[0]
    return rows[0] if rows else None


def _save_revalidated(document: dict[str, Any], root: Path, published_at: datetime, barrier, saved_id: str) -> tuple[dict | None, str | None, str]:
    try:
        current = load_thai_catalog(root, expected_instance_id=document.get("instance_id"))
    except (ThaiCatalogError, OSError, ValueError):
        current = None
    if current is not None and current.get("snapshot_id") != saved_id:
        return None, "snapshot_changed", str(current.get("snapshot_id"))
    document["snapshot_id"] = str(uuid.uuid4())
    document["parent_snapshot_id"] = saved_id
    document["created_at"] = published_at.isoformat()
    document["finished_at"] = published_at.isoformat()
    limitations = list(document.get("catalog_limitations") or [])
    if "revalidate_not_broad_discovery" not in limitations:
        limitations.append("revalidate_not_broad_discovery")
    document["catalog_limitations"] = limitations
    try:
        _publish_document(root, document, barrier)
        saved = load_thai_catalog(root, expected_instance_id=document.get("instance_id"))
    except (ThaiCatalogError, OSError, ValueError) as exc:
        return None, f"publish_failed:{type(exc).__name__}", saved_id
    return saved, None, saved.get("snapshot_id") or saved_id


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


def _jib_point(item, *, client, timeout: float, clock: ManualClock | None) -> OfferRefresh:
    from thailand.jib import fetch_read_product

    owns = client is None
    http = client or httpx.Client(follow_redirects=False, timeout=timeout)
    before = getattr(http, "http_calls", None)
    try:
        detail = fetch_read_product(http, str(item.get("external_id") or ""), timeout=timeout)
    finally:
        if owns:
            http.close()
    after = getattr(http, "http_calls", None)
    calls = (after - before) if before is not None and after is not None else 1
    refresh = _base(item, coverage="point_check:jib_read_product", http_calls=calls)
    refresh.retry_after_seconds = getattr(http, "retry_after_seconds", None)
    error = str(detail.get("error") or "")
    if getattr(http, "blocked_reason", None) and not getattr(http, "real_429", False):
        refresh.status = "skipped"
        refresh.reason = http.blocked_reason
        refresh.http_calls = calls
        return refresh
    if error == "HTTP_429" or getattr(http, "real_429", False):
        refresh.status = "rate_limited"
        refresh.reason = "retry_after"
        refresh.observed_at = None
        return refresh
    transport_error = getattr(http, "last_transport_error", None)
    if transport_error:
        refresh.status = "failed"
        refresh.reason = transport_error
        refresh.transport_failure = True
        refresh.observed_at = None
        return refresh
    if error == "HTTP_404":
        refresh.status = "missing"
        refresh.reason = "product_absent"
        return refresh
    if error in {"HTTP_500", "HTTP_503"}:
        refresh.status = "failed"
        refresh.reason = error
        refresh.store_unavailable = True
        refresh.observed_at = None
        return refresh
    if error:
        refresh.status = "failed"
        refresh.reason = error
        refresh.observed_at = None
        return refresh
    refresh.observed_specs = {
        name: detail.get(name)
        for name in ("cpu", "gpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")
        if detail.get(name) is not None
    }
    if detail.get("screen_size_inch") is not None:
        refresh.observed_specs["screen_inch"] = detail.get("screen_size_inch")
    refresh.sku = detail.get("sku")
    refresh.manufacturer_part_number = detail.get("manufacturer_part_number")
    refresh.variant_id = detail.get("variant_id")
    refresh.price_thb = detail.get("price_thb")
    refresh.availability_state = detail.get("availability_status")
    refresh.availability_confirmed = bool(detail.get("availability_confirmed"))
    if clock is not None and (refresh.price_thb or refresh.availability_confirmed):
        refresh.status = "updated"
        refresh.observed_at = clock.now()
    elif refresh.price_thb or refresh.availability_confirmed:
        refresh.status = "unverified"
        refresh.reason = "response_time_missing"
    else:
        refresh.status = "unverified"
        refresh.reason = "price_and_availability_unconfirmed"
    return refresh


def _bounded_request(item, *, client, timeout: float, clock: ManualClock | None) -> OfferRefresh:
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
    http = client or httpx.Client(follow_redirects=False, timeout=timeout)
    before = getattr(http, "http_calls", None)
    try:
        response = http.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        after = getattr(http, "http_calls", None)
        used = (after - before) if before is not None and after is not None else 1
        refresh = _base(item, coverage=coverage, http_calls=used)
        refresh.status = "failed"
        refresh.reason = type(exc).__name__
        refresh.transport_failure = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
        return refresh
    finally:
        if owns:
            http.close()
    after = getattr(http, "http_calls", None)
    calls = (after - before) if before is not None and after is not None else 1
    refresh = _base(item, coverage=coverage, http_calls=calls)
    refresh.retry_after_seconds = getattr(http, "retry_after_seconds", None) or _retry_after(response, clock.now() if clock else None)
    if getattr(http, "blocked_reason", None) and not getattr(http, "real_429", False):
        refresh.status = "skipped"
        refresh.reason = http.blocked_reason
        refresh.http_calls = calls
        return refresh
    if response.status_code == 429 or getattr(http, "real_429", False):
        refresh.status = "rate_limited"
        refresh.reason = "retry_after"
        return refresh
    if response.status_code == 404:
        refresh.status = "missing"
        refresh.reason = "product_absent"
        return refresh
    if response.status_code in {500, 503}:
        refresh.status = "failed"
        refresh.reason = f"HTTP_{response.status_code}"
        refresh.store_unavailable = True
        return refresh
    if response.status_code >= 400:
        refresh.status = "failed"
        refresh.reason = f"HTTP_{response.status_code}"
        return refresh
    observed = _parse_bounded(source, response, item, clock.now() if clock else datetime.now())
    if observed is None:
        refresh.status = "missing"
        refresh.reason = "not_observed_on_recheck"
        return refresh
    refresh.price_thb = observed.price_thb
    refresh.availability_state = observed.availability_state
    refresh.availability_confirmed = observed.availability_confirmed
    refresh.observed_specs = observed.observed_specs
    refresh.sku = observed.sku
    refresh.manufacturer_part_number = observed.manufacturer_part_number
    refresh.variant_id = observed.variant_id
    if clock is not None and (observed.price_thb or observed.availability_confirmed):
        refresh.status = "updated"
        refresh.observed_at = clock.now()
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
            sku=offer.sku,
            manufacturer_part_number=offer.manufacturer_part_number,
            price_thb=offer.price_thb,
            availability_state=offer.availability_status,
            availability_confirmed=bool(offer.availability_confirmed),
            observed_specs={
                "cpu": offer.cpu,
                "gpu": offer.gpu,
                "ram_gb": offer.ram_gb,
                "ssd_gb": offer.ssd_gb,
                "screen_inch": offer.screen_size_inch,
                "screen_resolution": offer.screen_resolution,
            },
        )
    return None


def _source_enabled(source: str) -> bool:
    for adapter in get_thailand_adapters():
        if adapter.slug == source:
            return bool(adapter.enabled) and source not in POLICY_DISABLED_SOURCES
    return False


def _finish_guarded_result(result: StoreScanResult, guard: GuardedClient) -> StoreScanResult:
    result.request_count = guard.http_calls
    if guard.real_429:
        result.retry_after_seconds = guard.retry_after_seconds
        result.discovery_rate_limited = True
        result.error_code = RATE_LIMITED
        result.ok = False
        return result
    if guard.blocked_reason and guard.http_calls == 0:
        result.ok = False
        result.discovery_rate_limited = False
        result.error_code = "TIMEOUT" if guard.blocked_reason == "deadline" else "BLOCKED"
        result.technical_detail = guard.blocked_reason
        result.collection_mode = "skipped"
        return result
    if guard.blocked_reason in {"deadline", "budget"}:
        result.discovery_rate_limited = False
        if result.error_code == RATE_LIMITED:
            result.error_code = "TIMEOUT" if guard.blocked_reason == "deadline" else "BUDGET"
            result.ok = False
        result.technical_detail = guard.blocked_reason
        limitations = list(result.discovery_limitations or [])
        limitations.append(guard.blocked_reason)
        result.discovery_limitations = limitations
    if guard.http_calls and guard.successes == 0 and not guard.real_429:
        result.ok = False
        result.error_code = "HTTP_ERROR"
        result.collection_mode = "failed"
        result.discovery_rate_limited = False
    return result


def _offer_identity(row: Mapping[str, Any]) -> tuple:
    return (row.get("source"), row.get("external_id"), row.get("variant_id"))


def _policy_row(source: str, *, disabled: bool, skipped: bool = False) -> dict[str, Any]:
    if disabled:
        return {
            "source": source,
            "store": source,
            "enabled": False,
            "policy_disabled": True,
            "state": "deferred",
            "status": "deferred",
            "checked_at": None,
            "latest_attempt_at": None,
            "request_count": 0,
            "completed_keys": [],
            "not_completed_keys": [],
            "limitations": ["policy_disabled"],
            "retry_after_seconds": None,
        }
    return {
        "source": source,
        "store": source,
        "enabled": True,
        "policy_disabled": False,
        "state": "skipped" if skipped else "failed",
        "status": "skipped" if skipped else "failed",
        "checked_at": None,
        "latest_attempt_at": None,
        "request_count": 0,
        "completed_keys": [],
        "not_completed_keys": [],
        "limitations": ["not_attempted"],
        "retry_after_seconds": None,
    }


def _attempt_kind(result: StoreScanResult) -> str:
    if result.policy_disabled or result.collection_mode == "disabled":
        return "disabled"
    if result.technical_detail in {"deadline", "budget", "circuit", "source_disabled"} and not result.offers:
        return "not_attempted"
    if result.collection_mode == "skipped" and not result.offers:
        return "not_attempted"
    if result.error_code == "NO_RESULTS" or (result.ok and not _rate_limited(result)):
        return "checked"
    if _rate_limited(result) or result.technical_detail in {"deadline", "budget"}:
        return "partial"
    if not result.ok:
        return "failed"
    return "checked"


def _note_attempt(document: dict[str, Any], source: str, *, response_at: datetime, http_calls: int, mode: str) -> None:
    for row in document.get("source_coverage") or []:
        if row.get("source") != source:
            continue
        row["request_count"] = int(row.get("request_count") or 0) + int(http_calls)
        row["latest_attempt_at"] = response_at.isoformat()
        limitations = list(row.get("limitations") or [])
        mark = f"{mode}_revalidate"
        if mark not in limitations:
            limitations.append(mark)
        row["limitations"] = limitations
        return


def _merge_discover(previous: Mapping[str, Any], results: Sequence[StoreScanResult], *, published_at: datetime, response_at: datetime, instance_id: str) -> dict[str, Any]:
    kinds = {result.store: _attempt_kind(result) for result in results}
    broad_complete = bool(kinds) and all(kind == "checked" for kind in kinds.values() if kind != "disabled")
    discovered = moment(previous.get("discovery_checked_at")) if not broad_complete else None
    discovery_at = discovered if discovered is not None and not broad_complete else response_at
    if discovery_at > published_at:
        discovery_at = published_at
    document = build_thai_catalog(
        results,
        now=published_at,
        discovery_checked_at=discovery_at,
        instance_id=instance_id,
        parent_snapshot_id=previous.get("snapshot_id"),
        started_at=response_at,
    )
    old_by_source: dict[str, list] = {}
    for offer in previous.get("offers") or []:
        old_by_source.setdefault(offer.get("source"), []).append(offer)
    new_by_source: dict[str, list] = {}
    for offer in document.get("offers") or []:
        new_by_source.setdefault(offer.get("source"), []).append(offer)
    merged: list = []
    seen_sources = set()
    for result in results:
        source = result.store
        seen_sources.add(source)
        kind = kinds[source]
        fresh = new_by_source.get(source, [])
        previous_rows = old_by_source.get(source, [])
        if kind == "checked":
            merged.extend(fresh)
        elif kind == "partial":
            seen_ids = {_offer_identity(row) for row in fresh}
            merged.extend(fresh)
            merged.extend(row for row in previous_rows if _offer_identity(row) not in seen_ids)
        else:
            merged.extend(previous_rows)
    for source, rows in old_by_source.items():
        if source not in seen_sources:
            merged.extend(rows)
    document["offers"] = merged
    document["variants"], document["conflicts"] = _variants(merged)
    document["exclusions"] = _diagnostics(merged)
    document["raw_observations"] = [_raw_observation(offer) for offer in merged]
    coverage = []
    for row in document.get("source_coverage") or []:
        source = row.get("source")
        kind = kinds.get(source, "not_attempted")
        if kind == "disabled":
            coverage.append(_policy_row(str(source), disabled=True))
            continue
        if kind == "not_attempted":
            coverage.append(_policy_row(str(source), disabled=False, skipped=True))
            continue
        updated = dict(row)
        if kind == "failed":
            updated["state"] = "failed"
            updated["status"] = "failed"
            updated["latest_attempt_at"] = response_at.isoformat()
            updated["checked_at"] = response_at.isoformat()
            notes = list(updated.get("limitations") or [])
            notes.append("attempt_failed_previous_observations_kept")
            updated["limitations"] = notes
        elif kind == "partial":
            notes = list(updated.get("limitations") or [])
            notes.append("partial_previous_observations_kept")
            updated["limitations"] = notes
            updated["latest_attempt_at"] = response_at.isoformat()
        elif kind == "checked":
            updated["latest_attempt_at"] = response_at.isoformat()
        coverage.append(updated)
    document["source_coverage"] = coverage
    document["catalog_limitations"] = _limitations(coverage)
    document["created_at"] = published_at.isoformat()
    document["finished_at"] = published_at.isoformat()
    return document


def _fx_rate(fx: Mapping[str, Any] | None, now: datetime):
    if not fx:
        return None
    from thailand.models import FxRate

    try:
        fetched = moment(fx.get("fetched_at")) or now
        return FxRate(
            source=str(fx.get("source") or "CBR"),
            currency=str(fx.get("currency") or "THB"),
            nominal=int(fx.get("nominal") or 1),
            official_rate=float(fx.get("rub_per_thb") or 0),
            rub_per_thb=float(fx.get("rub_per_thb") or 0),
            published_date=fx.get("published_date"),
            fetched_at=fetched,
            error=fx.get("error"),
        )
    except (TypeError, ValueError):
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
        "pass_started_at": kwargs.get("pass_started_at"),
        "published_at": kwargs.get("published_at"),
    }
    if kwargs.get("detail"):
        result["errors"].append(kwargs["detail"])
    if kwargs.get("replanned_decision") is not None:
        result["replanned_decision"] = kwargs["replanned_decision"]
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


def _retry_after(response, now: datetime | None = None) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    return _retry_after_value(raw, now)


def _retry_after_value(raw: object, now: datetime | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return max(1, int(text))
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if now is None:
        return None
    seconds = int((parsed.astimezone(timezone.utc) - now).total_seconds())
    return max(1, seconds)
