"""Unified ThaiCatalogSnapshot (schema 1) for the cross-market core.

The catalog is built from store results the caller already collected. It does
not read the Russian TOP, call stores, or replace an observation time with the
export clock.

C2 field-evidence contract. Package D confirms a spec only when its evidence
method is one of the allowed methods and ``capture_state`` is not ``unknown``.
C1 therefore confirms a non-GPU field only from that field's own record:

- ``metadata["field_evidence"][field]`` with ``method`` and ``captured_at``
  taken from the parse that produced the value;
- ``metadata["screen_resolution_evidence"]["fetched_at"]`` for the built-in
  panel only;
- a search-suggestion title only when the adapter recorded
  ``field_evidence`` for the fields ``specs_from_text`` actually returned.
  The catalog does not treat every value on a suggestion card as title evidence.

``gpu_source`` applies only to ``specs.gpu``. It is not a source, method, or
timestamp for CPU, RAM, SSD, diagonal, or resolution. C2 adapters must record
``field_evidence`` while parsing. C1 does not invent a missing timestamp or
copy the GPU observation onto another field.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import config
from thailand.discovery import GPU_SEARCH_QUERIES, LINE_SEARCH_QUERIES, QUERY_BUDGET
from thailand.models import StoreScanResult, ThailandOffer
from thailand.registry import get_thailand_adapters
from thailand.storage import atomic_write_json
from thailand.verification import (
    GPU_SOURCE_CATALOG_FILTER,
    GPU_SOURCE_SEARCH_QUERY,
    GPU_SOURCE_UNKNOWN,
)
from version import get_version

SCHEMA_VERSION = 1
SNAPSHOT_DIRNAME = "snapshots"
LATEST_NAME = "latest.json"
REFERENCES_NAME = "references.json"
CONFIRMING_METHODS = frozenset({
    "title",
    "explicit_title",
    "specs_cache",
    "structured_api",
    "structured_product_page",
    "product_detail",
    "store_listing",
})
UNCONFIRMING_GPU = frozenset({
    GPU_SOURCE_SEARCH_QUERY,
    GPU_SOURCE_CATALOG_FILTER,
    GPU_SOURCE_UNKNOWN,
    "",
})
CORE_FIELDS = (
    ("cpu", "cpu"),
    ("gpu", "gpu"),
    ("ram_gb", "ram_gb"),
    ("ssd_gb", "ssd_gb"),
    ("screen_inch", "screen_size_inch"),
    ("screen_resolution", "screen_resolution"),
)


class ThaiCatalogError(RuntimeError):
    """Catalog publication failed. The previous latest pointer stays in place."""


def discovery_scope_hash() -> str:
    """Identity of the broad Thai discovery policy. Russian TOP is not an input."""
    adapters = get_thailand_adapters()
    payload = {
        # Parser/evidence changes must refresh retained catalogues without rewriting them.
        "evidence_revision": "0120-own-field-clock-identity-policy-v1",
        "gpu_queries": list(GPU_SEARCH_QUERIES),
        "line_queries": list(LINE_SEARCH_QUERIES),
        "query_budget": QUERY_BUDGET,
        "stores": [{"source": item.slug, "enabled": bool(item.enabled)} for item in adapters],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_thai_catalog(
    store_results: Sequence[StoreScanResult],
    *,
    now: datetime,
    discovery_checked_at: datetime,
    instance_id: str,
    started_at: datetime | None = None,
    parent_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """One catalog from one set of store results. No network and no Russian list."""
    moment = _utc(now)
    discovered = _utc(discovery_checked_at)
    if discovered > moment:
        raise ThaiCatalogError("discovery_checked_at is in the future")
    instance = str(instance_id or "").strip()
    if not instance:
        raise ThaiCatalogError("instance_id is required")
    by_store = {result.store: result for result in store_results}
    offers = _offers(store_results, now=moment)
    coverage = _coverage(by_store, discovered)
    variants, conflicts = _variants(offers)
    limitations = _limitations(coverage)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": str(uuid.uuid4()),
        "instance_id": instance,
        "scanner_version": get_version(),
        "parser_version": get_version(),
        "started_at": _iso(_utc(started_at or discovered)),
        "finished_at": _iso(moment),
        "created_at": _iso(moment),
        "discovery_checked_at": _iso(discovered),
        "discovery_scope_hash": discovery_scope_hash(),
        "parent_snapshot_id": parent_snapshot_id,
        "eligibility_policy_id": _eligibility_policy_id(),
        "source_coverage": coverage,
        "raw_observations": [_raw_observation(offer) for offer in offers],
        "variants": variants,
        "offers": offers,
        "exclusions": _diagnostics(offers),
        "conflicts": conflicts,
        "catalog_limitations": limitations,
        "top_model_ids": [],
        "personal_profiles": [],
    }


def export_thai_catalog(
    store_results: Sequence[StoreScanResult],
    *,
    snapshot_root: Path | str,
    now: datetime,
    discovery_checked_at: datetime,
    instance_id: str,
    started_at: datetime | None = None,
    parent_snapshot_id: str | None = None,
    publish_barrier: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Write an immutable catalog, then point latest at it."""
    root = Path(snapshot_root)
    document = build_thai_catalog(
        store_results,
        now=now,
        discovery_checked_at=discovery_checked_at,
        instance_id=instance_id,
        started_at=started_at,
        parent_snapshot_id=parent_snapshot_id,
    )
    payload = _dumps(document)
    path = root / SNAPSHOT_DIRNAME / f"{document['snapshot_id']}.json"
    atomic_write_json(path, json.loads(payload.decode("utf-8")))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    barrier = publish_barrier or (lambda _point: None)
    barrier("before_latest")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": document["snapshot_id"],
        "sha256": digest,
        "path": f"{SNAPSHOT_DIRNAME}/{document['snapshot_id']}.json",
        "instance_id": instance_id,
        "created_at": document["created_at"],
        "discovery_checked_at": document["discovery_checked_at"],
        "discovery_scope_hash": document["discovery_scope_hash"],
    }
    atomic_write_json(root / LATEST_NAME, manifest)
    return manifest


def load_thai_catalog(
    snapshot_root: Path | str,
    *,
    snapshot_id: str | None = None,
    expected_instance_id: str | None = None,
) -> dict[str, Any]:
    root = Path(snapshot_root)
    expected_hash = None
    if snapshot_id is None:
        manifest = json.loads((root / LATEST_NAME).read_text(encoding="utf-8"))
        snapshot_id = str(manifest["snapshot_id"])
        relative = str(manifest["path"])
        expected_hash = str(manifest["sha256"])
        if expected_instance_id is None:
            expected_instance_id = manifest.get("instance_id")
    else:
        relative = f"{SNAPSHOT_DIRNAME}/{snapshot_id}.json"
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise ThaiCatalogError("snapshot path escapes the catalog root")
    raw = path.read_bytes()
    if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ThaiCatalogError("latest catalog hash does not match its file")
    document = json.loads(raw.decode("utf-8"))
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ThaiCatalogError("unsupported Thai catalog schema")
    if document.get("snapshot_id") != snapshot_id:
        raise ThaiCatalogError("snapshot_id does not match the requested file")
    if expected_instance_id is not None and document.get("instance_id") != expected_instance_id:
        raise ThaiCatalogError("catalog instance_id does not match")
    return document


def retain_thai_catalogs(snapshot_root: Path | str) -> list[str]:
    """Delete catalog files that latest and references.json do not name."""
    root = Path(snapshot_root)
    protected: set[str] = set()
    latest = root / LATEST_NAME
    if latest.is_file():
        try:
            protected.add(str(json.loads(latest.read_text(encoding="utf-8")).get("snapshot_id") or ""))
        except (OSError, json.JSONDecodeError):
            pass
    references = root / REFERENCES_NAME
    if references.is_file():
        try:
            payload = json.loads(references.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        protected.update(str(item) for item in payload.get("snapshot_ids") or [] if item)
    protected.discard("")
    folder = root / SNAPSHOT_DIRNAME
    if not folder.is_dir():
        return []
    deleted: list[str] = []
    for path in sorted(folder.glob("*.json")):
        if path.stem in protected:
            continue
        path.unlink()
        deleted.append(path.stem)
    return deleted


def as_comparison_snapshot(document: Mapping[str, Any]):
    """Adapter into the package D core. The document is not rewritten."""
    from market_contracts import thai_snapshot

    from thailand.specs_parse import is_non_laptop_listing
    cleaned = dict(document)
    cleaned["offers"] = [row for row in document.get("offers", ())
                         if not is_non_laptop_listing(str(row.get("name") or row.get("product_name") or ""))]
    return thai_snapshot(cleaned)


def rankings_from_catalog(document: Mapping[str, Any], fx: Mapping[str, Any] | None, *, now: datetime) -> dict[str, Any]:
    from market_contracts import fx_observation
    from thai_market_ranking import build_thai_rankings

    rate = fx_observation(fx) if fx is not None else None
    return build_thai_rankings(as_comparison_snapshot(document), rate, now=_utc(now))


def comparison_from_catalog(
    document: Mapping[str, Any],
    russian: Mapping[str, Any] | None,
    fx: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    from cross_market_comparison import compare_markets
    from market_contracts import fx_observation, russian_snapshot

    ru = russian_snapshot(russian) if russian is not None else None
    rate = fx_observation(fx) if fx is not None else None
    comparison = compare_markets(as_comparison_snapshot(document), ru, rate, now=_utc(now))
    ru_rows, ru_candidates = russian_offer_views(russian, now=now)
    comparison["ru_eligible_offers"] = ru_rows
    comparison["ru_information_candidates"] = ru_candidates
    from collections import Counter
    comparison["ru_input_summary"] = {
        "offer_count": len(russian.get("offers") or []),
        "model_count": len(russian.get("models") or []),
        "exclusion_reasons": dict(Counter(str(row.get("reason") or "unknown")
                                          for row in russian.get("exclusions") or [])),
    } if russian is not None else None
    return comparison


def russian_offer_views(russian: Mapping[str, Any] | None, *, now: datetime) -> tuple[list[dict], list[dict]]:
    """One evidence gate for daily and the interactive Russian TOP."""
    from market_contracts import fx_observation, russian_snapshot
    ru = russian_snapshot(russian) if russian is not None else None
    from thai_market_ranking import assess_quote, value_score
    from market_contracts import ComparisonPolicy
    policy = ComparisonPolicy()
    ru_rows = []
    ru_candidates = []
    if ru is not None:
        for offer in ru.offers:
            assessment = assess_quote(offer, ru, None, now=_utc(now), policy=policy)
            if assessment.eligible:
                score, breakdown, confidence = value_score(offer, assessment.price_rub, now=_utc(now), policy=policy)
                ru_rows.append({"model_id": offer.model_id or offer.offer_id,
                    "variant_id": offer.variant_id or offer.offer_id,
                    "offers": [offer.to_dict()], "best_offer_id": offer.offer_id,
                    "name": offer.name, "price_rub": str(assessment.price_rub),
                    "score": str(score), "confidence": confidence, "score_breakdown": breakdown,
                    "in_personal_top3": any(offer.model_id in p["model_ids"] for p in ru.personal_profiles)})
            elif (assessment.price_rub is not None and assessment.price_rub <= policy.max_price_rub
                  and offer.availability_state in {"in_stock", "online_available", "store_pickup_only"}
                  and not any(reason in assessment.reasons for reason in
                              ("price_stale_or_unknown", "availability_stale_or_unknown",
                               "availability_unconfirmed", "ru_source_not_current"))):
                ru_candidates.append({"offer": offer.to_dict(), "reasons": list(assessment.reasons)})
    from decimal import Decimal
    ru_rows.sort(key=lambda row: (-Decimal(row["score"]), -row["confidence"], Decimal(row["price_rub"]), row["best_offer_id"]))
    return ru_rows, ru_candidates


def _offers(store_results: Sequence[StoreScanResult], *, now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in store_results:
        for offer in list(result.offers) + list(result.unverified_candidates or []):
            from thailand.specs_parse import is_non_laptop_listing
            if is_non_laptop_listing(offer.name):
                continue
            record = _offer_record(offer, result, now=now, candidate=offer in (result.unverified_candidates or []))
            if record["offer_id"] in seen:
                continue
            seen.add(record["offer_id"])
            rows.append(record)
    rows.sort(key=lambda row: (row["source"], row["external_id"], row["offer_id"]))
    return rows


def _offer_record(
    offer: ThailandOffer,
    result: StoreScanResult,
    *,
    now: datetime,
    candidate: bool,
) -> dict[str, Any]:
    observed = _utc(offer.collected_at)
    observed_iso = _iso(observed)
    usable_time = observed <= now
    price = _price(offer.price_thb)
    price_known = price is not None and (offer.price_source or "") not in {"", "unknown"}
    availability_state = str(offer.availability_status or "unknown")
    availability_known = bool(offer.availability_confirmed) and availability_state in {
        "in_stock", "online_available", "store_pickup_only", "out_of_stock", "preorder",
    } and (offer.availability_source or "") not in {"", "unknown", "search_suggestion"}
    availability_at = observed_iso if availability_known and usable_time else None
    price_at = observed_iso if price_known and usable_time else None
    trust = _seller_trust(offer)
    specs = {name: _spec_slot(offer, name, attr, now=now) for name, attr in CORE_FIELDS}
    record = {
        "offer_id": _stable("th-offer", ["th", offer.store, str(offer.external_id), str(offer.variant_id or "")]),
        "source": offer.store,
        "external_id": str(offer.external_id),
        "variant_id": offer.variant_id,
        "sku": offer.sku,
        "manufacturer_part_number": offer.manufacturer_part_number,
        "sku_candidates": _texts(offer.sku),
        "mpn_candidates": _texts(offer.manufacturer_part_number),
        "price_thb": price,
        "public_price_source": offer.price_source,
        "price_checked_at": price_at,
        "price_verified_for_variant": bool(price_known and offer.price_verified_for_variant and usable_time),
        "availability_state": availability_state,
        "availability_confirmed": availability_known and usable_time,
        "availability_checked_at": availability_at,
        "availability_evidence": _availability_evidence(offer, availability_at, availability_known and usable_time),
        "seller_channel": "marketplace" if offer.marketplace else (offer.channel or "direct"),
        "seller_trust": trust,
        "seller_name": offer.seller_name,
        "product_name": offer.name,
        "name": offer.name,
        "source_url": offer.url,
        "url": offer.url,
        "observed_at": observed_iso,
        "record_role": "candidate" if candidate else "observation",
        "specs": specs,
        "known_extras": {},
        "identity_evidence": _identity(offer, now=now),
    }
    if candidate:
        return _withhold_purchase_confirmation(record)
    return record


def _withhold_purchase_confirmation(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the candidate's own facts, but do not present them as a purchase quote.

    Package D does not read ``record_role``. The fields it does read — confirmed
    availability, variant price, and spec evidence — are therefore withheld.
    """
    record["candidate_observation"] = {
        "price_thb": record["price_thb"],
        "price_checked_at": record["price_checked_at"],
        "price_verified_for_variant": record["price_verified_for_variant"],
        "availability_state": record["availability_state"],
        "availability_confirmed": record["availability_confirmed"],
        "availability_checked_at": record["availability_checked_at"],
        "availability_evidence": json.loads(json.dumps(record["availability_evidence"])),
        "seller_trust": record["seller_trust"],
        "specs": json.loads(json.dumps(record["specs"])),
        "identity_evidence": json.loads(json.dumps(record["identity_evidence"])),
    }
    record["price_verified_for_variant"] = False
    record["availability_confirmed"] = False
    record["availability_checked_at"] = None
    evidence = dict(record["availability_evidence"])
    evidence.update(captured_at=None, capture_state="unknown", available=None, method=GPU_SOURCE_UNKNOWN)
    record["availability_evidence"] = evidence
    for slot in record["specs"].values():
        if isinstance(slot.get("evidence"), dict):
            slot["evidence"]["captured_at"] = None
            slot["evidence"]["capture_state"] = "unknown"
    for slot in record["identity_evidence"].values():
        if isinstance(slot.get("evidence"), dict):
            slot["evidence"]["captured_at"] = None
            slot["evidence"]["capture_state"] = "unknown"
    record["purchase_excluded_reason"] = "unverified_candidate"
    return record


def _spec_slot(offer: ThailandOffer, field: str, attr: str, *, now: datetime) -> dict[str, Any]:
    value = getattr(offer, attr, None)
    basis = _own_field_basis(offer, field)
    if field == "gpu" and (basis is None or basis[0] in UNCONFIRMING_GPU):
        method = str(offer.gpu_source or GPU_SOURCE_UNKNOWN)
        return {
            "value": None,
            "evidence": {
                "source": offer.store,
                "external_id": str(offer.external_id),
                "captured_at": None,
                "capture_state": "unknown",
                "method": method,
                "provenance": method,
                "candidate_value": offer.candidate_gpu or value,
            },
        }
    if value is None:
        return {"value": None, "evidence": None}
    if basis is None:
        return {
            "value": value,
            "evidence": {
                "source": offer.store,
                "external_id": str(offer.external_id),
                "captured_at": None,
                "capture_state": "unknown",
                "method": GPU_SOURCE_UNKNOWN,
                "provenance": GPU_SOURCE_UNKNOWN,
            },
        }
    method, captured = basis
    moment = _parse(captured)
    confirmed = method in CONFIRMING_METHODS and moment is not None and moment <= now
    return {
        "value": value,
        "evidence": {
            "source": offer.store,
            "external_id": str(offer.external_id),
            "captured_at": _iso(moment) if confirmed and moment is not None else None,
            "capture_state": "known" if confirmed else "unknown",
            "method": method if method in CONFIRMING_METHODS else GPU_SOURCE_UNKNOWN,
            "provenance": method,
        },
    }


def _own_field_basis(offer: ThailandOffer, field: str) -> tuple[str, str] | None:
    """Return (method, captured_at) only for evidence that belongs to this field."""
    meta = offer.metadata or {}
    if field == "screen_resolution" and isinstance(meta.get("screen_resolution_evidence"), dict):
        fetched = meta["screen_resolution_evidence"].get("fetched_at")
        if fetched:
            return "structured_api", str(fetched)
    recorded = meta.get("field_evidence")
    if isinstance(recorded, dict) and isinstance(recorded.get(field), dict):
        entry = recorded[field]
        method = str(entry.get("method") or "")
        captured = entry.get("captured_at")
        if method in CONFIRMING_METHODS and _parse(None if captured is None else str(captured)) is not None:
            return method, str(captured)
        return None
    if field == "gpu":
        method = str(offer.gpu_source or GPU_SOURCE_UNKNOWN)
        if method not in CONFIRMING_METHODS:
            return None
        return method, _iso(_utc(offer.collected_at))
    return None


def _identity(offer: ThailandOffer, *, now: datetime) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for public, field in (("brand", "brand"), ("manufacturer_family", "model_family"),
                          ("manufacturer_generation", "manufacturer_generation")):
        value = getattr(offer, field, None)
        if value is None:
            value = (offer.metadata or {}).get(field)
        basis = _own_field_basis(offer, field)
        if value is None or basis is None:
            continue
        method, captured = basis
        moment = _parse(captured)
        confirmed = method in CONFIRMING_METHODS and moment is not None and moment <= now
        found[public] = {"value": value, "evidence": {
            "source": offer.store, "external_id": str(offer.external_id),
            "captured_at": _iso(moment) if confirmed else None,
            "capture_state": "known" if confirmed else "unknown",
            "method": method if confirmed else GPU_SOURCE_UNKNOWN, "provenance": method}}
    return found


def _availability_evidence(offer: ThailandOffer, captured_at: str | None, confirmed: bool) -> dict[str, Any]:
    method = {
        "structured_api": "structured_api",
        "product_detail": "product_detail",
        "search_suggestion": "store_listing",
    }.get(str(offer.availability_source or ""), GPU_SOURCE_UNKNOWN)
    return {
        "source": offer.store,
        "external_id": str(offer.external_id),
        "captured_at": captured_at if confirmed else None,
        "capture_state": "known" if confirmed else "unknown",
        "method": method if confirmed and method in CONFIRMING_METHODS else GPU_SOURCE_UNKNOWN,
        "available": bool(offer.available) if confirmed else None,
        "availability_state": offer.availability_status or "unknown",
    }


def _seller_trust(offer: ThailandOffer) -> str:
    from thailand.seller_trust import TIER_A, TIER_B, classify_seller_trust

    if not offer.marketplace and (offer.channel in {None, "", "direct"}):
        return "direct_retailer"
    tier = offer.seller_trust_tier or classify_seller_trust(offer)
    if tier == TIER_A:
        return "A"
    if tier == TIER_B:
        return "B"
    return "unknown"


def _coverage(by_store: Mapping[str, StoreScanResult], discovered: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for adapter in get_thailand_adapters():
        result = by_store.get(adapter.slug)
        if not adapter.enabled:
            rows.append({
                "source": adapter.slug,
                "store": adapter.slug,
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
            })
            continue
        if result is None:
            rows.append({
                "source": adapter.slug,
                "store": adapter.slug,
                "enabled": True,
                "policy_disabled": False,
                "state": "failed",
                "status": "failed",
                "checked_at": None,
                "latest_attempt_at": None,
                "request_count": 0,
                "completed_keys": [],
                "not_completed_keys": list(GPU_SEARCH_QUERIES),
                "limitations": ["not_attempted"],
                "retry_after_seconds": None,
            })
            continue
        finished = [str(item) for item in (result.queries_finished or [])]
        planned = [str(item) for item in (result.discovery_queries or [])]
        missing = [item for item in planned if item not in finished]
        limited = (
            result.error_code == "RATE_LIMITED"
            or result.collection_mode == "rate_limited"
            or bool(result.discovery_rate_limited)
        )
        if result.policy_disabled:
            state = "deferred"
        elif result.circuit_breaker_status == "open":
            state = "deferred"
        elif limited:
            state = "429"
        elif not result.ok:
            state = "failed"
        elif missing or result.discovery_limitations:
            state = "partial"
        else:
            state = "ok"
        checked = _iso(discovered) if state in {"ok", "partial", "429", "failed"} else None
        limitations = list(result.discovery_limitations or [])
        if limited:
            limitations.append("rate_limited")
        if result.error:
            limitations.append(str(result.error_code or "store_error"))
        rows.append({
            "source": adapter.slug,
            "store": adapter.slug,
            "enabled": True,
            "policy_disabled": False,
            "state": state,
            "status": state,
            "checked_at": checked,
            "latest_attempt_at": checked,
            "request_count": result.request_count,
            "completed_keys": finished,
            "not_completed_keys": missing,
            "limitations": limitations,
            "retry_after_seconds": result.retry_after_seconds,
        })
    return rows


def _variants(offers: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: list[list[Mapping[str, Any]]] = []
    for offer in offers:
        for group in groups:
            if all(_same_proven_config(member, offer) for member in group):
                group.append(offer)
                break
        else:
            groups.append([offer])
    variants: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for group in groups:
        fingerprint = _fingerprint(group[0]) if all(_complete(row) for row in group) and _shared_identifier(group) else None
        variant_id = (
            _stable("th-variant", ["proven", *_shared_identifier(group), fingerprint or ""])
            if fingerprint
            else _stable("th-variant", ["listing", group[0]["source"], group[0]["external_id"]])
        )
        identifiers = _unique(
            item
            for row in group
            for item in [row.get("sku"), row.get("manufacturer_part_number"), *(row.get("sku_candidates") or [])]
            if item
        )
        if len(group) > 1 and not fingerprint:
            conflicts.append({
                "status": "conflict",
                "reason": "configuration_not_proven",
                "member_offer_ids": [row["offer_id"] for row in group],
                "identifiers": identifiers,
            })
        variants.append({
            "variant_id": variant_id,
            "state": "proven" if fingerprint else "candidate",
            "member_offer_ids": [row["offer_id"] for row in group],
            "sku": group[0].get("sku"),
            "manufacturer_part_number": group[0].get("manufacturer_part_number"),
            "strong_identifiers": identifiers,
            "brand": ((group[0].get("identity_evidence") or {}).get("brand") or {}).get("value"),
            "specs": {name: group[0]["specs"][name]["value"] for name, _attr in CORE_FIELDS},
        })
    return variants, conflicts


def _same_proven_config(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if not _complete(left) or not _complete(right):
        return False
    shared = set(_norm_ids(left)) & set(_norm_ids(right))
    if not shared:
        return False
    return _fingerprint(left) == _fingerprint(right)


def _complete(offer: Mapping[str, Any]) -> bool:
    for name, _attr in CORE_FIELDS:
        slot = offer["specs"][name]
        evidence = slot.get("evidence") or {}
        if slot.get("value") is None or evidence.get("capture_state") != "known":
            return False
    return True


def _fingerprint(offer: Mapping[str, Any]) -> str:
    return "|".join(f"{name}={offer['specs'][name]['value']}" for name, _attr in CORE_FIELDS)


def _shared_identifier(group: Sequence[Mapping[str, Any]]) -> list[str]:
    shared: set[str] | None = None
    for offer in group:
        tokens = set(_norm_ids(offer))
        shared = tokens if shared is None else shared & tokens
    return sorted(shared or [])


def _norm_ids(offer: Mapping[str, Any]) -> list[str]:
    values = [offer.get("sku"), offer.get("manufacturer_part_number"), *(offer.get("sku_candidates") or []), *(offer.get("mpn_candidates") or [])]
    return ["".join(str(value).upper().split()) for value in values if value]


def _diagnostics(offers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offer in offers:
        if offer["record_role"] == "candidate":
            reason = "unverified_candidate"
        elif not offer["availability_confirmed"]:
            reason = "availability_unconfirmed"
        elif offer["availability_state"] in {"out_of_stock", "preorder", "unknown"}:
            reason = str(offer["availability_state"])
        else:
            continue
        rows.append({
            "offer_id": offer["offer_id"],
            "source": offer["source"],
            "external_id": offer["external_id"],
            "reason": reason,
            "current_purchase": False,
        })
    return rows


def _raw_observation(offer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "offer_id": offer["offer_id"],
        "source": offer["source"],
        "external_id": offer["external_id"],
        "price_thb": offer["price_thb"],
        "price_checked_at": offer["price_checked_at"],
        "availability_state": offer["availability_state"],
        "availability_checked_at": offer["availability_checked_at"],
        "observed_at": offer["observed_at"],
        "record_role": offer["record_role"],
    }


def _limitations(coverage: Sequence[Mapping[str, Any]]) -> list[str]:
    found: list[str] = []
    for row in coverage:
        if row["state"] == "429":
            found.append(f"{row['source']}:429")
        elif row["state"] == "partial":
            found.append(f"{row['source']}:partial")
        elif row["state"] == "failed":
            found.append(f"{row['source']}:not_attempted_or_failed")
        elif row.get("policy_disabled"):
            found.append(f"{row['source']}:policy_disabled")
    if any(row["state"] != "ok" for row in coverage if row.get("enabled")):
        found.append("partial_coverage_is_not_market_absence")
    return found


def _eligibility_policy_id() -> str:
    payload = {
        "max_tracked_price_rub": config.MAX_TRACKED_PRICE_RUB,
        "min_ram_gb": config.MIN_INSTALLED_RAM_GB,
        "min_ssd_gb": config.MIN_SSD_GB,
        "min_screen_width_exclusive": config.MIN_SCREEN_WIDTH_EXCLUSIVE,
        "min_screen_height": config.MIN_SCREEN_HEIGHT,
        "min_screen_pixels": config.MIN_SCREEN_PIXELS,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _price(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _texts(*values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in found:
            found.append(text)
    return found


def _unique(values: Any) -> list[str]:
    return _texts(*list(values))


def _stable(prefix: str, parts: Sequence[Any]) -> str:
    raw = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"), default=str)
    return prefix + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _dumps(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _utc(moment).isoformat()
