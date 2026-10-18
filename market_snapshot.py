from __future__ import annotations

"""Immutable full-catalog snapshot of the current Russian laptop market.

The Russian TOP, BUY rules, and alert history stay on their existing path.
This module publishes every fresh eligible offer for later country comparison.
"""

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import config
from identity_sync import SPECS_CACHE_PATH
from pipeline_lock import DEFAULT_LOCK_PATH, pipeline_lock
from product_identity import normalize_identifier
from russian_deals import (
    eligible_cluster_offers,
    prepare_russian_matches,
    rank_prepared_matches,
)
from storage import MarketReadError, MarketRows, read_market_rows_consistent
from stores.registry import enabled_slugs

SCHEMA_VERSION = 1
SNAPSHOT_DIRNAME = "snapshots"
LATEST_NAME = "latest.json"
REFERENCES_NAME = "references.json"
CORE_FIELDS = (
    ("cpu", "cpu", "cpu"),
    ("gpu", "gpu", "gpu"),
    ("ram_gb", "ram_gb", "ram_gb"),
    ("ssd_gb", "ssd_gb", "ssd_gb"),
    ("screen_inch", "screen_size_inch", "screen_inch"),
    ("screen_resolution", "screen_resolution", "screen_resolution"),
)
EXTRA_FIELDS = ("gpu_vram_gb", "screen_refresh_hz", "os")
SKU_IDENTIFIER_TYPES = frozenset({"sku"})
MPN_IDENTIFIER_TYPES = frozenset({"manufacturer_part_number", "alternative_part_number"})
STRONG_IDENTIFIER_TYPES = SKU_IDENTIFIER_TYPES | MPN_IDENTIFIER_TYPES


class RussianSnapshotError(RuntimeError):
    """Snapshot export failed. The previous latest pointer must stay in place."""


class ConsistentGenerationError(RussianSnapshotError):
    """SQLite and specs moved between reads, so no single generation could be published."""


def export_russian_market_snapshot(
    db_path: Path | str,
    *,
    specs_path: Path | str | None = None,
    snapshot_root: Path | str | None = None,
    pipeline_run_id: int | str | None = None,
    instance_id: str | None = None,
    now: datetime | None = None,
    lock_path: Path | str | None = None,
    pipeline_lock_held: bool = False,
    consistency_barrier: Callable[[str], None] | None = None,
    publish_attempts: int = 3,
) -> dict[str, Any]:
    """Write one immutable snapshot, then point ``latest`` at it.

    A failed export raises and leaves the previous ``latest`` file untouched.
    """
    root = Path(snapshot_root) if snapshot_root is not None else _default_root(db_path)
    specs = Path(specs_path) if specs_path is not None else SPECS_CACHE_PATH
    moment = _as_utc(now)
    instance = (instance_id or config.get_instance_id() or "").strip()
    if not instance:
        raise RussianSnapshotError("instance_id is required")
    barrier = consistency_barrier or (lambda _point: None)
    with _held_lock(lock_path, pipeline_lock_held):
        rows, specs_bytes = _capture_inputs(
            db_path,
            specs,
            barrier=barrier,
            attempts=max(1, int(publish_attempts)),
        )
        try:
            cache = _parse_specs(specs_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RussianSnapshotError("specs cache is not valid JSON") from exc
        document = _build_document(
            rows,
            cache,
            now=moment,
            instance_id=instance,
            pipeline_run_id=pipeline_run_id,
            specs_generation=_sha256(specs_bytes),
        )
        if not _same_generation(db_path, specs, rows, specs_bytes):
            raise ConsistentGenerationError("source changed before snapshot write")
        snapshot_path = _snapshot_path(root, document["snapshot_id"])
        payload = _dumps(document)
        _atomic_write(snapshot_path, payload)
        barrier("before_latest")
        written = snapshot_path.read_bytes()
        if written != payload or not _same_generation(db_path, specs, rows, specs_bytes):
            raise ConsistentGenerationError("source changed before latest publication")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": document["snapshot_id"],
            "sha256": _sha256(written),
            "path": f"{SNAPSHOT_DIRNAME}/{document['snapshot_id']}.json",
            "instance_id": instance,
            "pipeline_run_id": document["pipeline_run_id"],
            "database_generation": document["database_generation"],
            "specs_generation": document["specs_generation"],
            "created_at": document["created_at"],
        }
        _atomic_write(root / LATEST_NAME, _dumps(manifest))
        return manifest


def load_russian_market_snapshot(
    snapshot_root: Path | str,
    *,
    snapshot_id: str | None = None,
    expected_instance_id: str | None = None,
) -> dict[str, Any]:
    """Load a published snapshot. ``snapshot_id=None`` follows the latest pointer."""
    root = Path(snapshot_root)
    manifest: dict[str, Any] | None = None
    if snapshot_id is None:
        manifest = json.loads((root / LATEST_NAME).read_text(encoding="utf-8"))
        snapshot_id = str(manifest["snapshot_id"])
        relative = str(manifest["path"])
        expected_hash = str(manifest["sha256"])
    else:
        relative = f"{SNAPSHOT_DIRNAME}/{snapshot_id}.json"
        expected_hash = None
    path = _path_inside(root, relative)
    raw = path.read_bytes()
    digest = _sha256(raw)
    if expected_hash is not None and digest != expected_hash:
        raise RussianSnapshotError("latest snapshot hash does not match its file")
    document = json.loads(raw.decode("utf-8"))
    if document.get("schema_version") != SCHEMA_VERSION:
        raise RussianSnapshotError("unsupported Russian snapshot schema")
    if document.get("snapshot_id") != snapshot_id:
        raise RussianSnapshotError("snapshot_id does not match the requested file")
    if expected_instance_id is not None and document.get("instance_id") != expected_instance_id:
        raise RussianSnapshotError("snapshot instance_id does not match")
    if manifest is not None and manifest.get("instance_id") not in (None, document.get("instance_id")):
        raise RussianSnapshotError("latest manifest instance_id does not match the snapshot")
    return document


def retain_russian_market_snapshots(snapshot_root: Path | str) -> list[str]:
    """Delete snapshot files that latest and references.json do not name."""
    root = Path(snapshot_root)
    protected = _referenced_snapshot_ids(root)
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


def _build_document(
    rows: MarketRows,
    specs_cache: Mapping[str, Any],
    *,
    now: datetime,
    instance_id: str,
    pipeline_run_id: int | str | None,
    specs_generation: str,
) -> dict[str, Any]:
    ambiguous: list[dict[str, Any]] = []
    matches, specs_by_key = prepare_russian_matches(
        rows.products,
        rows.identifiers,
        specs_cache,
        ambiguous=ambiguous,
    )
    fresh = _fresh_stores(rows.latest_store_runs, now)
    ranked = rank_prepared_matches(matches, specs_by_key, rows.price_history, fresh)
    products_by_key = {
        (str(row["store"]), str(row["external_id"])): row for row in rows.products
    }
    identifiers_by_product: dict[int, list[dict]] = {}
    for row in rows.identifiers:
        identifiers_by_product.setdefault(int(row["product_id"]), []).append(dict(row))
    match_by_offer: dict[tuple[str, str], Any] = {}
    for match in matches:
        for offer in match.offers:
            match_by_offer[(offer.store, str(offer.external_id))] = match

    grouped: list[tuple[Any, list[Any]]] = []
    seen_matches: set[int] = set()
    for deal in ranked:
        winner = (deal.store, str((deal.offer or {}).get("external_id") or ""))
        match = match_by_offer.get(winner)
        if match is None or id(match) in seen_matches:
            continue
        seen_matches.add(id(match))
        grouped.append((deal, eligible_cluster_offers(match, specs_by_key, fresh)))

    offer_records: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    current_keys: set[tuple[str, str]] = set()
    published_rank = 0

    def publish(deal: Any, records: Sequence[dict[str, Any]], *, scored: bool) -> None:
        nonlocal published_rank
        if not records:
            return
        published_rank += 1
        model = _model_record(records, deal, published_rank, ambiguous, scored=scored)
        for record in records:
            record["model_id"] = model["model_id"]
            current_keys.add((record["store"], record["external_id"]))
        offer_records.extend(records)
        models.append(model)

    scored_groups: list[tuple[Any, list[dict[str, Any]]]] = []
    other_groups: list[tuple[Any, list[dict[str, Any]]]] = []
    for deal, candidates in grouped:
        built = _current_offer_records(
            candidates,
            products_by_key,
            specs_by_key,
            identifiers_by_product,
            rows,
            now,
        )
        winner = (deal.store, str((deal.offer or {}).get("external_id") or ""))
        for group in _groups_by_configuration(built):
            keys = {(row["store"], row["external_id"]) for row in group}
            if winner in keys:
                scored_groups.append((deal, group))
            else:
                other_groups.append((deal, group))
    for deal, group in scored_groups:
        publish(deal, group, scored=True)
    for deal, group in other_groups:
        publish(deal, group, scored=False)

    exclusions = _exclusions(
        rows,
        products_by_key,
        current_keys,
        fresh,
        now,
        specs_by_key,
    )
    coverage = _source_coverage(rows, now)
    created_at = _iso(now)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": str(uuid.uuid4()),
        "instance_id": instance_id,
        "pipeline_run_id": _id_or_none(pipeline_run_id),
        "captured_at": _captured_at(coverage),
        "created_at": created_at,
        "database_generation": rows.generation,
        "specs_generation": specs_generation,
        "eligibility_policy_id": eligibility_policy_id(),
        "ranking_policy_id": ranking_policy_id(),
        "source_coverage": coverage,
        "offers": sorted(offer_records, key=lambda row: (row["store"], row["external_id"], row["offer_id"])),
        "models": models,
        "top_model_ids": [
            model["model_id"]
            for model in models
            if model.get("score") is not None
        ][: int(config.TOP_DEALS_LIMIT)],
        "personal_profiles": _personal_profiles(models, offer_records),
        "excluded_count": len(exclusions),
        "exclusions": exclusions,
    }


def eligibility_policy_id() -> str:
    payload = {
        "max_tracked_price_rub": config.MAX_TRACKED_PRICE_RUB,
        "min_installed_ram_gb": config.MIN_INSTALLED_RAM_GB,
        "min_ssd_gb": config.MIN_SSD_GB,
        "min_screen_width_exclusive": config.MIN_SCREEN_WIDTH_EXCLUSIVE,
        "min_screen_height": config.MIN_SCREEN_HEIGHT,
        "min_screen_pixels": config.MIN_SCREEN_PIXELS,
        "store_freshness_max_minutes": config.STORE_FRESHNESS_MAX_MINUTES,
    }
    return "sha256:" + _sha256(_dumps(payload))


def ranking_policy_id() -> str:
    keys = sorted(
        name
        for name in vars(config)
        if name.startswith("SCORE_") or name in {"TOP_DEALS_LIMIT", "CROSS_STORE_DIFFERENCE_RUB"}
    )
    payload = {name: getattr(config, name) for name in keys}
    payload["target_prices"] = dict(config.TARGET_PRICES)
    return "sha256:" + _sha256(_dumps(payload))


def _offer_record(
    offer: Any,
    product: Mapping[str, Any],
    identity: Any,
    identifier_rows: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    specs_by_key: Mapping[tuple[str, str], Any],
) -> dict[str, Any] | None:
    price = _store_price(product.get("price"))
    if price is None:
        return None
    price_checked_at = _price_checked_at(product, history, price)
    availability_checked_at = _text_or_none(product.get("last_checked_at"))
    observed_at = availability_checked_at or price_checked_at
    specs, extras = _field_evidence(
        offer,
        identity,
        listing_observed_at=observed_at,
        specs_by_key=specs_by_key,
    )
    variant_id = _variant_id(specs)
    sku_candidates, mpn_candidates = _identifier_candidates(offer, identity, identifier_rows)
    return {
        "offer_id": _stable_id("ru", [offer.store, str(offer.external_id), variant_id or ""]),
        "store": offer.store,
        "external_id": str(offer.external_id),
        "variant_id": variant_id,
        "source_url": offer.url,
        "observed_at": observed_at,
        "price_checked_at": price_checked_at,
        "price_changed_at": _price_changed_at(history),
        "availability_checked_at": availability_checked_at,
        "stock_state": "in_stock" if product.get("available") else "out_of_stock",
        "availability_evidence": {
            "source": offer.store,
            "external_id": str(offer.external_id),
            "captured_at": availability_checked_at,
            "method": "store_listing",
            "available": bool(product.get("available")),
        },
        "public_price_rub": price,
        "product_name": str(product.get("name") or offer.name or ""),
        "sku_candidates": sku_candidates,
        "mpn_candidates": mpn_candidates,
        "quote_state": "current",
        "specs": specs,
        "known_extras": extras,
        "identity_evidence": _identity_evidence(offer, identity),
    }


def _current_offer_records(
    candidates: Sequence[Any],
    products_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    specs_by_key: Mapping[tuple[str, str], Any],
    identifiers_by_product: Mapping[int, Sequence[Mapping[str, Any]]],
    rows: MarketRows,
    now: datetime,
) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []
    for offer in candidates:
        key = (offer.store, str(offer.external_id))
        product = products_by_key.get(key)
        if product is None or _store_state(rows.latest_store_runs.get(key[0]), now) != "ok":
            continue
        history = rows.price_history.get(int(product["id"]), ())
        if _observation_state(product, history, now) != "current":
            continue
        record = _offer_record(
            offer,
            product,
            specs_by_key.get(key),
            identifiers_by_product.get(int(product["id"]), []),
            history,
            specs_by_key,
        )
        if record is not None:
            built.append(record)
    return built


def _groups_by_configuration(records: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep offers together only when no confirmed core field disagrees.

    Compatibility is checked against every member. An offer with an unknown
    CPU must not pull two different known CPUs into one configuration.
    """
    groups: list[list[dict[str, Any]]] = []
    for record in records:
        for group in groups:
            if all(_configuration_compatible(member, record) for member in group):
                group.append(record)
                break
        else:
            groups.append([record])
    return groups


def _configuration_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    for name, _identity_name, _title_name in CORE_FIELDS:
        left_value = (left.get("specs") or {}).get(name, {}).get("value")
        right_value = (right.get("specs") or {}).get(name, {}).get("value")
        if left_value is not None and right_value is not None and left_value != right_value:
            return False
    for name in EXTRA_FIELDS:
        left_extra = (left.get("known_extras") or {}).get(name, {}).get("value")
        right_extra = (right.get("known_extras") or {}).get(name, {}).get("value")
        if name == "gpu_vram_gb" and left_extra is not None and right_extra is not None and left_extra != right_extra:
            return False
    return True


def _model_record(
    offers: Sequence[Mapping[str, Any]],
    deal: Any,
    rank: int,
    ambiguous: Sequence[Mapping[str, Any]],
    *,
    scored: bool,
) -> dict[str, Any]:
    member_keys = {(str(offer["store"]), str(offer["external_id"])) for offer in offers}
    conflicts = _conflicts_for(member_keys, ambiguous)
    if _members_disagree(offers):
        conflicts = [
            *conflicts,
            {
                "status": "AMBIGUOUS",
                "reason": "confirmed_fields_disagree",
                "shared_identifiers": [],
                "different": _disagreeing_fields(offers),
            },
        ]
    strong = _unique(
        item
        for offer in offers
        for item in list(offer["sku_candidates"]) + list(offer["mpn_candidates"])
    )
    variant_ids = {offer.get("variant_id") for offer in offers}
    shared_ids = _shared_normalized_identifiers(offers)
    proven = (
        not conflicts
        and len(variant_ids) == 1
        and None not in variant_ids
        and bool(shared_ids or len(offers) == 1)
    )
    if conflicts:
        state = "conflict"
    elif proven:
        state = "proven"
    else:
        state = "incomplete"
    if proven and shared_ids:
        identity_parts = ["proven", *sorted(shared_ids), str(next(iter(variant_ids)))]
    elif proven:
        identity_parts = ["proven", str(next(iter(variant_ids))), *_member_tokens(offers)]
    else:
        identity_parts = ["listing", *_member_tokens(offers)]
    names = [str(offer.get("product_name") or "") for offer in offers]
    return {
        "model_id": _stable_id("model", identity_parts),
        "member_offer_ids": [str(offer["offer_id"]) for offer in sorted(offers, key=lambda row: row["offer_id"])],
        "state": state,
        "strong_identifiers": strong,
        "conflicts": conflicts,
        "rank": rank,
        "score": deal.score if scored else None,
        "cluster_key": deal.cluster_key,
        "product_names": names,
    }


def _exclusions(
    rows: MarketRows,
    products_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    current_keys: set[tuple[str, str]],
    fresh: set[str],
    now: datetime,
    specs_by_key: Mapping[tuple[str, str], Any],
) -> list[dict[str, Any]]:
    from laptop_eligibility import product_is_eligible

    found: list[dict[str, Any]] = []
    for key, product in sorted(products_by_key.items()):
        if key in current_keys:
            continue
        store, external_id = key
        observation = _observation_state(product, rows.price_history.get(int(product["id"]), ()), now)
        store_state = _store_state(rows.latest_store_runs.get(store), now)
        price = _store_price(product.get("price"))
        if store_state == "stale" or observation == "stale":
            quote_state, reason = "stale", "stale_observation" if observation == "stale" else "store_stale"
        elif store_state != "ok" or observation == "unverified" or price is None:
            quote_state = "unverified"
            reason = "store_not_fresh" if store_state != "ok" else "unverified_observation"
            if price is None and observation == "current" and store_state == "ok":
                reason = "unverified_price"
        elif not product.get("available"):
            quote_state, reason = "excluded", "not_available"
        elif not config.is_price_in_tracking_scope(price):
            quote_state, reason = "excluded", "price_over_cap"
        elif not product_is_eligible(
            {"name": product.get("name"), "price": price, "available": True},
            specs_by_key.get(key),
        ):
            quote_state, reason = "excluded", "not_eligible"
        elif store not in fresh:
            quote_state, reason = "unverified", "store_not_fresh"
        else:
            quote_state, reason = "excluded", "not_selected"
        price_checked_at = _price_checked_at(product, rows.price_history.get(int(product["id"]), ()), price)
        found.append(
            {
                "store": store,
                "external_id": external_id,
                "product_name": product.get("name"),
                "observed_at": _text_or_none(product.get("last_checked_at")),
                "price_checked_at": price_checked_at,
                "price_changed_at": _price_changed_at(rows.price_history.get(int(product["id"]), ())),
                "availability_checked_at": _text_or_none(product.get("last_checked_at")),
                "public_price_rub": price,
                "quote_state": quote_state,
                "reason": reason,
                "current": False,
            }
        )
    return found


def _source_coverage(rows: MarketRows, now: datetime) -> list[dict[str, Any]]:
    names = set(enabled_slugs()) | set(rows.latest_store_runs) | {str(row["store"]) for row in rows.products}
    coverage: list[dict[str, Any]] = []
    for store in sorted(names):
        run = rows.latest_store_runs.get(store)
        state = _store_state(run, now)
        limitations: list[str] = []
        if run is None:
            limitations.append("not_attempted")
        elif state == "failed":
            limitations.append("latest_attempt_failed")
        elif state == "stale":
            limitations.append("latest_attempt_older_than_ttl")
        elif state == "partial":
            limitations.append("partial_attempt")
        if run is not None and _is_future(_text_or_none(run.get("finished_at") or run.get("attempted_at")), now):
            limitations.append("future_timestamp_not_fresh")
        products_count = None if run is None else run.get("products_count")
        if state == "ok" and products_count in (0, None):
            limitations.append("empty_result_is_not_proof_of_absence")
        coverage.append(
            {
                "store": store,
                "latest_attempt_at": None if run is None else _text_or_none(run.get("finished_at") or run.get("attempted_at")),
                "state": state,
                "completed_work": {
                    "status": None if run is None else run.get("status"),
                    "products_count": products_count,
                    "finished_at": None if run is None else _text_or_none(run.get("finished_at")),
                    "pipeline_run_id": None if run is None else run.get("pipeline_run_id"),
                },
                "limitations": limitations,
            }
        )
    return coverage


def _personal_profiles(
    models: Sequence[Mapping[str, Any]],
    offers: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    offers_by_id = {str(offer["offer_id"]): offer for offer in offers}
    profiles: list[dict[str, Any]] = []
    for profile in config.PERSONAL_TOP3:
        exact = {
            token
            for token in (normalize_identifier(value) for value in profile.get("exact_skus") or ())
            if token
        }
        series = [
            token
            for token in (normalize_identifier(value) for value in profile.get("series_codes") or ())
            if token
        ]
        linked: list[str] = []
        for model in models:
            identifiers = [normalize_identifier(value) for value in model.get("strong_identifiers") or ()]
            identifiers = [token for token in identifiers if token]
            names = " ".join(str(name) for name in model.get("product_names") or ())
            offer_names = " ".join(
                str(offers_by_id[offer_id].get("product_name") or "")
                for offer_id in model.get("member_offer_ids") or ()
                if offer_id in offers_by_id
            )
            haystack = " ".join(identifiers + [normalize_identifier(names) or "", normalize_identifier(offer_names) or ""])
            exact_hit = any(token in exact for token in identifiers)
            series_hit = any(token in haystack for token in series)
            if exact and exact_hit:
                linked.append(str(model["model_id"]))
            elif not exact and series_hit:
                linked.append(str(model["model_id"]))
            elif exact and series_hit and not exact_hit:
                linked.append(str(model["model_id"]))
        kind = "series" if not exact else ("exact_or_series" if series else "exact_sku")
        profiles.append(
            {
                "key": profile.get("key"),
                "label": profile.get("model"),
                "match_kind": kind,
                "model_ids": linked,
            }
        )
    return profiles


def _field_evidence(
    offer: Any,
    identity: Any,
    *,
    listing_observed_at: str | None,
    specs_by_key: Mapping[tuple[str, str], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evidence for one offer. A specs-cache value keeps its own capture time.

    The cache records a source label in ``field_provenance`` and, only when a
    caller stored one, a timestamp in ``field_captured_at``. A later price or
    stock check does not become that timestamp. Title text is dated by the
    listing observation because that text was seen then.
    """
    from comparison import offer_identity
    from identity_sync import _values_equivalent
    from stores.common import extract_specs_from_name

    title = extract_specs_from_name(str(getattr(offer, "name", "") or ""))
    proven = offer_identity(offer, specs_by_key or {})
    specs: dict[str, Any] = {}
    for public_name, identity_name, title_name in CORE_FIELDS:
        cached = None if identity is None else getattr(identity, identity_name, None)
        title_value = title.get(title_name)
        own_cache_time = _known_spec_time(identity, (identity_name, public_name))
        title_conflicts = title_value is not None and not _values_equivalent(identity_name, cached, title_value)
        if cached is not None and (own_cache_time or title_value is None or title_conflicts):
            value = _json_value(getattr(proven, identity_name, None))
            evidence = _spec_evidence(
                offer,
                identity,
                method="specs_cache",
                field_names=(identity_name, public_name),
                listing_observed_at=listing_observed_at,
            )
        elif title.get(title_name) is not None:
            value = _json_value(title.get(title_name))
            evidence = _spec_evidence(
                offer,
                identity,
                method="title",
                field_names=(public_name, title_name),
                listing_observed_at=listing_observed_at,
            )
        else:
            value, evidence = None, None
        specs[public_name] = {"value": value, "evidence": evidence}
    extras: dict[str, Any] = {}
    if identity is not None:
        for name in EXTRA_FIELDS:
            cached = getattr(identity, name, None)
            if cached is None:
                continue
            extras[name] = {
                "value": _json_value(cached),
                "evidence": _spec_evidence(
                    offer,
                    identity,
                    method="specs_cache",
                    field_names=(name,),
                    listing_observed_at=listing_observed_at,
                ),
            }
    return specs, extras


def _spec_evidence(
    offer: Any,
    identity: Any,
    *,
    method: str,
    field_names: tuple[str, ...],
    listing_observed_at: str | None,
) -> dict[str, Any]:
    if method == "specs_cache":
        captured_at = _known_spec_time(identity, field_names)
        capture_state = "known" if captured_at else "unknown"
    elif method == "title" and listing_observed_at:
        captured_at = listing_observed_at
        capture_state = "listing"
    else:
        captured_at = None
        capture_state = "unknown"
    return {
        "source": offer.store,
        "external_id": str(offer.external_id),
        "captured_at": captured_at,
        "capture_state": capture_state,
        "method": method,
        "provenance": _provenance_label(identity, field_names),
    }


def _identity_evidence(offer: Any, identity: Any) -> dict[str, Any]:
    """Pass documented manufacturer identity through B to D without guessing it."""
    found = {}
    for public, attr in (("brand", "brand"), ("manufacturer_family", "model_family"),
                         ("manufacturer_generation", "manufacturer_generation")):
        value = getattr(identity, attr, None)
        if value is None:
            continue
        found[public] = {"value": value, "evidence": _spec_evidence(
            offer, identity, method="specs_cache", field_names=(attr, public),
            listing_observed_at=None)}
    return found


def _source_identifiers(identity: Any) -> dict[str, Any]:
    raw = getattr(identity, "source_identifiers", None)
    return raw if isinstance(raw, dict) else {}


def _known_spec_time(identity: Any, field_names: Sequence[str]) -> str | None:
    """Return a timestamp only when the cache stored one for this field."""
    captured = _source_identifiers(identity).get("field_captured_at")
    if not isinstance(captured, dict):
        return None
    for name in field_names:
        moment = _parse_time(_text_or_none(captured.get(name)))
        if moment is not None:
            return _iso(moment)
    return None


def _provenance_label(identity: Any, field_names: Sequence[str]) -> str | None:
    raw = _source_identifiers(identity).get("field_provenance")
    if not isinstance(raw, dict):
        return None
    for name in field_names:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _members_disagree(offers: Sequence[Mapping[str, Any]]) -> bool:
    for index, left in enumerate(offers):
        for right in offers[index + 1 :]:
            if not _configuration_compatible(left, right):
                return True
    return False


def _disagreeing_fields(offers: Sequence[Mapping[str, Any]]) -> list[str]:
    found: list[str] = []
    names = [name for name, _identity_name, _title_name in CORE_FIELDS]
    if any(
        (left.get("known_extras") or {}).get("gpu_vram_gb", {}).get("value") is not None
        and (right.get("known_extras") or {}).get("gpu_vram_gb", {}).get("value") is not None
        and (left.get("known_extras") or {}).get("gpu_vram_gb", {}).get("value")
        != (right.get("known_extras") or {}).get("gpu_vram_gb", {}).get("value")
        for left in offers
        for right in offers
    ):
        names.append("gpu_vram_gb")
    for name in names:
        values = []
        for offer in offers:
            if name == "gpu_vram_gb":
                value = (offer.get("known_extras") or {}).get(name, {}).get("value")
            else:
                value = (offer.get("specs") or {}).get(name, {}).get("value")
            if value is not None:
                values.append(value)
        if len(set(map(str, values))) > 1 and name not in found:
            found.append(name)
    return found


def _variant_id(specs: Mapping[str, Mapping[str, Any]]) -> str | None:
    parts: list[str] = []
    for name, _identity_name, _title_name in CORE_FIELDS:
        value = specs[name]["value"]
        if value is None:
            return None
        parts.append(f"{name}={value}")
    return _stable_id("variant", parts)


def _identifier_candidates(
    offer: Any,
    identity: Any,
    identifier_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    skus: list[str] = []
    mpns: list[str] = []
    if getattr(offer, "sku", None):
        skus.append(str(offer.sku))
    if identity is not None:
        if identity.sku:
            skus.append(str(identity.sku))
        if identity.manufacturer_part_number:
            mpns.append(str(identity.manufacturer_part_number))
        for item in identity.alternative_part_numbers or []:
            if item:
                mpns.append(str(item))
    for row in identifier_rows:
        kind = str(row.get("identifier_type") or "")
        raw = row.get("value")
        if not raw:
            continue
        if kind in SKU_IDENTIFIER_TYPES:
            skus.append(str(raw))
        elif kind in MPN_IDENTIFIER_TYPES:
            mpns.append(str(raw))
    return _unique(skus), _unique(mpns)


def _conflicts_for(
    member_keys: set[tuple[str, str]],
    ambiguous: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for record in ambiguous:
        different = list(record.get("different") or [])
        if not different or not (_ambiguous_keys(record) & member_keys):
            continue
        found.append(
            {
                "status": record.get("status") or "AMBIGUOUS",
                "reason": record.get("reason"),
                "shared_identifiers": list(record.get("shared_identifiers") or []),
                "different": different,
            }
        )
    return found


def _ambiguous_keys(record: Mapping[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for side in ("left", "right"):
        summary = record.get(side) or {}
        if not isinstance(summary, Mapping):
            continue
        for offer in summary.get("offers") or []:
            if isinstance(offer, Mapping) and offer.get("store") and offer.get("external_id"):
                keys.add((str(offer["store"]), str(offer["external_id"])))
    product = record.get("product") or {}
    if isinstance(product, Mapping) and product.get("store") and product.get("external_id"):
        keys.add((str(product["store"]), str(product["external_id"])))
    return keys


def _shared_normalized_identifiers(offers: Sequence[Mapping[str, Any]]) -> set[str]:
    shared: set[str] | None = None
    for offer in offers:
        tokens = {
            token
            for token in (
                normalize_identifier(value)
                for value in list(offer.get("sku_candidates") or []) + list(offer.get("mpn_candidates") or [])
            )
            if token
        }
        shared = tokens if shared is None else shared & tokens
    return shared or set()


def _member_tokens(offers: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(f"{offer['store']}:{offer['external_id']}" for offer in offers)


def _observation_state(
    product: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    now: datetime,
) -> str:
    price = _store_price(product.get("price"))
    checked = (
        _parse_time(_price_checked_at(product, history, price)),
        _parse_time(_text_or_none(product.get("last_checked_at"))),
    )
    if any(moment is None for moment in checked):
        return "unverified"
    limit = int(config.STORE_FRESHNESS_MAX_MINUTES) * 60
    for moment in checked:
        assert moment is not None
        age = (now - moment).total_seconds()
        if age < 0:
            return "unverified"
        if age > limit:
            return "stale"
    return "current"


def _store_state(run: Mapping[str, Any] | None, now: datetime) -> str:
    if run is None:
        return "failed"
    status = str(run.get("status") or "").lower()
    if status not in {"ok", "failed", "partial"}:
        return "partial" if status else "failed"
    if status != "ok":
        return "failed" if status == "failed" else "partial"
    finished = _parse_time(_text_or_none(run.get("finished_at") or run.get("attempted_at")))
    if finished is None:
        return "failed"
    age = (now - finished).total_seconds()
    if age < 0:
        return "failed"
    if age > int(config.STORE_FRESHNESS_MAX_MINUTES) * 60:
        return "stale"
    return "ok"


def _fresh_stores(latest_runs: Mapping[str, Mapping[str, Any]], now: datetime) -> set[str]:
    from store_freshness import get_fresh_store_slugs

    return get_fresh_store_slugs(latest_runs, now=now)


def _price_checked_at(
    product: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    price: int | None,
) -> str | None:
    if price is None:
        return None
    # Both fields originate in a successfully persisted own store observation.
    # Old schemas are read without migration; their last_checked_at has that meaning.
    return _text_or_none(product.get("price_checked_at") if "price_checked_at" in product else product.get("last_checked_at"))


def _price_changed_at(history: Sequence[Mapping[str, Any]]) -> str | None:
    previous, changed = None, None
    for row in history:
        price = _store_price(row.get("price"))
        if price != previous:
            changed = _text_or_none(row.get("checked_at"))
        previous = price
    return changed


def _captured_at(coverage: Sequence[Mapping[str, Any]]) -> str | None:
    moments = [
        _parse_time(_text_or_none(row.get("latest_attempt_at")))
        for row in coverage
        if row.get("state") == "ok"
    ]
    known = [moment for moment in moments if moment is not None]
    if not known:
        return None
    return _iso(max(known))


def _capture_inputs(
    db_path: Path | str,
    specs_path: Path,
    *,
    barrier: Callable[[str], None],
    attempts: int,
) -> tuple[MarketRows, bytes]:
    last_error: Exception | None = None
    for _attempt in range(attempts):
        specs_before = _read_bytes(specs_path)
        barrier("after_specs_before")
        try:
            rows = read_market_rows_consistent(db_path)
        except MarketReadError as exc:
            last_error = exc
            continue
        barrier("after_db")
        specs_after = _read_bytes(specs_path)
        try:
            rows_again = read_market_rows_consistent(db_path)
        except MarketReadError as exc:
            last_error = exc
            continue
        if specs_before == specs_after and rows.generation == rows_again.generation:
            return rows, specs_before
        last_error = ConsistentGenerationError("SQLite and specs generations diverged during export")
    raise ConsistentGenerationError(str(last_error) if last_error else "no consistent generation")


def _same_generation(db_path: Path | str, specs_path: Path, rows: MarketRows, specs_bytes: bytes) -> bool:
    try:
        again = read_market_rows_consistent(db_path)
    except MarketReadError:
        return False
    return again.generation == rows.generation and _read_bytes(specs_path) == specs_bytes


def _referenced_snapshot_ids(root: Path) -> set[str]:
    protected: set[str] = set()
    latest = root / LATEST_NAME
    if latest.is_file():
        try:
            manifest = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if manifest.get("snapshot_id"):
            protected.add(str(manifest["snapshot_id"]))
    references = root / REFERENCES_NAME
    if references.is_file():
        try:
            payload = json.loads(references.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        protected.update(str(item) for item in payload.get("snapshot_ids") or [] if item)
    return protected


def _parse_specs(raw: bytes) -> dict[str, Any]:
    if not raw.strip():
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RussianSnapshotError("specs cache must be a JSON object")
    return data


def _read_bytes(path: Path) -> bytes:
    if not path.exists():
        return b""
    return path.read_bytes()


def _default_root(db_path: Path | str) -> Path:
    return Path(db_path).resolve().parent / "russian_market"


def _snapshot_path(root: Path, snapshot_id: str) -> Path:
    if not snapshot_id or any(char not in "0123456789abcdef-" for char in snapshot_id.lower()):
        raise RussianSnapshotError("snapshot_id is not a safe file name")
    return _path_inside(root, f"{SNAPSHOT_DIRNAME}/{snapshot_id}.json")


def _path_inside(root: Path, relative: str) -> Path:
    base = root.resolve()
    path = (base / relative).resolve()
    if path != base and base not in path.parents:
        raise RussianSnapshotError("snapshot path escapes the snapshot root")
    return path


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _held_lock(lock_path: Path | str | None, pipeline_lock_held: bool) -> Iterator[None]:
    if pipeline_lock_held:
        yield
        return
    with pipeline_lock(DEFAULT_LOCK_PATH if lock_path is None else lock_path):
        yield


def _dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable_id(prefix: str, parts: Sequence[str]) -> str:
    raw = "\n".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _store_price(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _json_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    return str(value)


def _unique(values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in found:
            found.append(text)
    return found


def _id_or_none(value: int | str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_time(value: str | None) -> datetime | None:
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


def _is_future(value: str | None, now: datetime) -> bool:
    moment = _parse_time(value)
    return moment is not None and moment > now


def _as_utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat()
