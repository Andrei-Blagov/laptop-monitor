from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from enrichment import _headers, enrich_andpro_product, enrich_regard_product
from product_identity import (
    ProductIdentity,
    identity_from_collected_product,
    normalize_identifier,
)
from storage import (
    DEFAULT_DB_PATH,
    STRONG_IDENTIFIER_TYPES,
    count_product_identifiers,
    count_products,
    get_all_products,
    get_product_identifiers,
    init_db,
    open_db,
    upsert_product_identifier,
)

logger = logging.getLogger(__name__)

SPECS_CACHE_PATH = Path("data") / "product_specs.json"

# Fields merged in product_specs.json (incomplete snapshot must not erase known).
_MERGE_SPEC_FIELDS = (
    "brand",
    "model_family",
    "model",
    "manufacturer_part_number",
    "cpu",
    "gpu",
    "gpu_vram_gb",
    "ram_gb",
    "ram_type",
    "ssd_gb",
    "screen_size_inch",
    "screen_resolution",
    "screen_refresh_hz",
    "os",
    "color",
    "ean",
    "gtin",
)

_STRUCTURED_SOURCES = (
    "regard.api",
    "andpro.card",
    "structured",
    "product_page",
    "json_ld",
    "catalog",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _product_enriched(identifiers: list[dict], store: str) -> bool:
    sources = [str(row.get("source") or "") for row in identifiers]
    if store == "regard":
        return any(s.startswith("regard.api") for s in sources)
    if store == "andpro":
        return any(s.startswith("andpro.card") for s in sources)
    return True


def _identity_has_core_specs(identity: ProductIdentity | None) -> bool:
    if identity is None:
        return False
    return any(
        (
            identity.gpu,
            identity.cpu,
            identity.ram_gb is not None,
            identity.ssd_gb is not None,
            identity.screen_size_inch is not None,
        )
    )


def _stamp_structured_provenance(
    identity: ProductIdentity, source: str
) -> ProductIdentity:
    """Mark known fields as structured store data (authoritative over title)."""
    prov = dict(_field_provenance(identity))
    for field in (
        "gpu",
        "cpu",
        "ram_gb",
        "ssd_gb",
        "screen_size_inch",
        "screen_resolution",
        "screen_refresh_hz",
        "gpu_vram_gb",
    ):
        if getattr(identity, field, None) is not None:
            prov[field] = source
    src = dict(identity.source_identifiers or {})
    src["field_provenance"] = prov
    identity.source_identifiers = src
    return identity


def identifiers_from_product_row(product: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Базовый SKU из таблицы products."""
    sku = product.get("sku")
    if not sku:
        return []
    return [("sku", str(sku), "products.sku")]


def identifiers_from_identity(
    identity: ProductIdentity,
) -> list[tuple[str, str, str]]:
    """
    Strong identifiers из enrichment.

    Regard:
      vendorcode -> sku
      alternative_pn[] -> alternative_part_number

    ANDPRO:
      products.sku / identity.sku -> sku
      mpn -> manufacturer_part_number
    """
    rows: list[tuple[str, str, str]] = []
    if identity.store == "regard":
        vendor = identity.source_identifiers.get("vendorcode") or identity.sku
        if vendor:
            rows.append(("sku", str(vendor), "regard.api.vendorcode"))
        for alt in identity.alternative_part_numbers:
            if alt:
                rows.append(
                    ("alternative_part_number", str(alt), "regard.api.alternative_pn")
                )
    elif identity.store == "andpro":
        if identity.sku:
            rows.append(("sku", str(identity.sku), "andpro.card.sku"))
        mpn = identity.manufacturer_part_number
        if mpn:
            rows.append(
                ("manufacturer_part_number", str(mpn), "andpro.card.mpn")
            )
    return rows


def _field_provenance(identity: ProductIdentity) -> dict[str, str]:
    raw = identity.source_identifiers.get("field_provenance")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {}


def _source_rank(source: str | None) -> int:
    """Higher = more authoritative."""
    if not source:
        return 0
    s = source.lower()
    if any(s.startswith(p) or p in s for p in _STRUCTURED_SOURCES):
        return 40
    if s in {"collected_metadata", "metadata", "product_metadata"}:
        return 30
    if s in {"product_title", "title", "title_parser"}:
        return 20
    return 10


def _values_equivalent(field: str, left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    if field in {"gpu", "cpu", "screen_resolution", "os", "brand", "model"}:
        return str(left).upper().replace(" ", "") == str(right).upper().replace(" ", "")
    if field in {"ram_gb", "ssd_gb", "gpu_vram_gb", "screen_refresh_hz"}:
        try:
            return int(left) == int(right)
        except (TypeError, ValueError):
            return left == right
    if field == "screen_size_inch":
        try:
            return abs(float(left) - float(right)) < 0.05
        except (TypeError, ValueError):
            return left == right
    return left == right


def merge_identity_into_cache(
    cache: dict[str, Any],
    identity: ProductIdentity,
) -> dict[str, Any]:
    """
    Merge identity into specs cache with non-destructive semantics.

    - known + incoming None → keep known
    - None + incoming known → use incoming
    - same normalized → keep/update harmlessly
    - conflicting known → keep existing trusted; record SPEC_CONFLICT
    """
    key = f"{identity.store}:{identity.external_id}"
    incoming = identity.to_dict()
    existing = cache.get(key)
    conflicts: list[dict[str, Any]] = []

    if not isinstance(existing, dict):
        cache[key] = incoming
        return {"key": key, "status": "created", "conflicts": []}

    merged = dict(existing)
    existing_prov = {}
    if isinstance(existing.get("source_identifiers"), dict):
        existing_prov = dict(
            existing["source_identifiers"].get("field_provenance") or {}
        )
    incoming_prov = _field_provenance(identity)

    for field in _MERGE_SPEC_FIELDS:
        old = existing.get(field)
        new = incoming.get(field)
        if new is None or new == "":
            continue
        if old is None or old == "":
            merged[field] = new
            if field in incoming_prov:
                existing_prov[field] = incoming_prov[field]
            continue
        if _values_equivalent(field, old, new):
            merged[field] = new
            # Prefer higher-authority provenance label when equivalent.
            if _source_rank(incoming_prov.get(field)) >= _source_rank(
                existing_prov.get(field)
            ):
                if field in incoming_prov:
                    existing_prov[field] = incoming_prov[field]
            continue

        # Conflict: do not silently overwrite.
        old_rank = _source_rank(existing_prov.get(field))
        new_rank = _source_rank(incoming_prov.get(field))
        keep = old
        kept_source = existing_prov.get(field)
        if new_rank > old_rank:
            # Only replace when incoming is strictly more authoritative.
            keep = new
            kept_source = incoming_prov.get(field)
            merged[field] = new
            if kept_source:
                existing_prov[field] = kept_source
        conflicts.append(
            {
                "status": "SPEC_CONFLICT",
                "key": key,
                "field": field,
                "existing": old,
                "incoming": new,
                "kept": keep,
                "existing_source": existing_prov.get(field),
                "incoming_source": incoming_prov.get(field),
            }
        )
        logger.warning(
            "SPEC_CONFLICT %s.%s existing=%r incoming=%r kept=%r",
            key,
            field,
            old,
            new,
            keep,
        )

    # Always refresh identity envelope fields from the latest collection.
    for envelope in (
        "store",
        "external_id",
        "sku",
        "name",
        "price",
        "available",
        "url",
    ):
        if incoming.get(envelope) is not None:
            merged[envelope] = incoming[envelope]

    # Merge raw_characteristics / alternative_part_numbers conservatively.
    old_raw = existing.get("raw_characteristics")
    new_raw = incoming.get("raw_characteristics")
    if isinstance(old_raw, dict) or isinstance(new_raw, dict):
        raw_merged = dict(old_raw or {})
        for rk, rv in dict(new_raw or {}).items():
            if rk not in raw_merged or raw_merged[rk] in (None, ""):
                raw_merged[rk] = rv
        merged["raw_characteristics"] = raw_merged

    old_alts = existing.get("alternative_part_numbers") or []
    new_alts = incoming.get("alternative_part_numbers") or []
    if isinstance(old_alts, list) or isinstance(new_alts, list):
        seen: set[str] = set()
        alts: list[str] = []
        for item in list(old_alts) + list(new_alts):
            token = str(item)
            if token and token not in seen:
                seen.add(token)
                alts.append(token)
        merged["alternative_part_numbers"] = alts

    src = dict(existing.get("source_identifiers") or {})
    inc_src = dict(incoming.get("source_identifiers") or {})
    src.update({k: v for k, v in inc_src.items() if k != "field_provenance"})
    if existing_prov:
        src["field_provenance"] = existing_prov
    if conflicts:
        prev = src.get("spec_conflicts")
        bucket = list(prev) if isinstance(prev, list) else []
        bucket.extend(conflicts)
        src["spec_conflicts"] = bucket[-20:]
    merged["source_identifiers"] = src

    cache[key] = merged
    return {
        "key": key,
        "status": "merged" if not conflicts else "conflict",
        "conflicts": conflicts,
    }


def _save_spec(cache: dict[str, Any], identity: ProductIdentity) -> dict[str, Any]:
    return merge_identity_into_cache(cache, identity)


def load_specs_cache(path: Path | str = SPECS_CACHE_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_specs_cache(
    cache: dict[str, Any],
    path: Path | str = SPECS_CACHE_PATH,
) -> None:
    """Atomically write product_specs.json (temp → fsync → replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cache, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, p)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def identity_from_cache(
    cache: dict[str, Any],
    store: str,
    external_id: str,
) -> ProductIdentity | None:
    raw = cache.get(f"{store}:{external_id}")
    if not isinstance(raw, dict):
        return None
    try:
        return ProductIdentity(**{
            k: raw[k]
            for k in ProductIdentity.__dataclass_fields__
            if k in raw
        })
    except TypeError:
        return None


def update_specs_cache_from_products(
    products: Sequence[Any],
    specs_path: Path | str = SPECS_CACHE_PATH,
    *,
    cache: dict[str, Any] | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """
    Persist collected Product specs into product_specs.json before metadata is lost.

    Optional enrichment failure must not fail the caller — returns stats only.
    """
    working = cache if cache is not None else load_specs_cache(specs_path)
    stats: dict[str, Any] = {
        "products": 0,
        "updated": 0,
        "created": 0,
        "conflicts": 0,
        "conflict_details": [],
        "stores": {},
    }
    for product in products:
        store = str(getattr(product, "store", "") or "")
        external_id = str(getattr(product, "external_id", "") or "")
        if not store or not external_id:
            continue
        stats["products"] += 1
        try:
            identity = identity_from_collected_product(product)
            before = f"{store}:{external_id}" in working
            result = merge_identity_into_cache(working, identity)
            if result["status"] == "created" or not before:
                stats["created"] += 1
            else:
                stats["updated"] += 1
            if result["conflicts"]:
                stats["conflicts"] += len(result["conflicts"])
                stats["conflict_details"].extend(result["conflicts"])
            stats["stores"][store] = stats["stores"].get(store, 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "specs cache update skipped for %s:%s: %s",
                store,
                external_id,
                type(exc).__name__,
            )
    if save:
        try:
            save_specs_cache(working, specs_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("specs cache atomic write failed: %s", type(exc).__name__)
            raise
    return stats


def upsert_identifier_rows(
    conn,
    product_id: int,
    rows: list[tuple[str, str, str]],
    *,
    seen_at: str | None = None,
) -> dict[str, int]:
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    now = seen_at or _iso_now()
    for identifier_type, value, source in rows:
        if identifier_type not in STRONG_IDENTIFIER_TYPES:
            stats["skipped"] += 1
            continue
        normalized = normalize_identifier(value)
        if not normalized:
            stats["skipped"] += 1
            continue
        action = upsert_product_identifier(
            conn,
            product_id=product_id,
            identifier_type=identifier_type,
            value=str(value).strip(),
            normalized_value=normalized,
            source=source,
            seen_at=now,
        )
        stats[action] = stats.get(action, 0) + 1
    return stats


def sync_product_identifiers(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    enrich: bool = True,
    specs_path: Path | str = SPECS_CACHE_PATH,
) -> dict[str, Any]:
    """
    Синхронизирует product_identifiers для всех products.

    - всегда пишет products.sku как type=sku;
    - обогащает товары без store-specific identifiers;
    - повторный запуск не создаёт дублей (UNIQUE);
    - Regard/ANDPRO re-enrich при cache miss (specs не теряются).
    """
    products = get_all_products(db_path)
    specs_cache = load_specs_cache(specs_path)
    totals = {
        "products": len(products),
        "enriched": 0,
        "skipped_enrich": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "identifiers_before": 0,
        "identifiers_after": 0,
        "spec_conflicts": 0,
    }

    regard_client: httpx.Client | None = None
    andpro_client: httpx.Client | None = None

    try:
        with open_db(db_path) as conn:
            init_db(conn)
            totals["identifiers_before"] = count_product_identifiers(conn)

            for product in products:
                product_id = int(product["id"])
                store = str(product["store"])
                existing = get_product_identifiers(conn, product_id)
                already = _product_enriched(existing, store)

                identity: ProductIdentity | None = identity_from_cache(
                    specs_cache, store, str(product["external_id"])
                )

                needs_structured = (
                    enrich
                    and store in {"regard", "andpro"}
                    and (not already or not _identity_has_core_specs(identity))
                )
                if needs_structured:
                    try:
                        if store == "regard":
                            if regard_client is None:
                                regard_client = httpx.Client(
                                    headers=_headers(True),
                                    follow_redirects=True,
                                    timeout=30.0,
                                )
                            identity = _stamp_structured_provenance(
                                enrich_regard_product(
                                    product, client=regard_client
                                ),
                                "regard.api",
                            )
                        else:
                            if andpro_client is None:
                                andpro_client = httpx.Client(
                                    headers=_headers(False),
                                    follow_redirects=True,
                                    timeout=40.0,
                                )
                            identity = _stamp_structured_provenance(
                                enrich_andpro_product(
                                    product, client=andpro_client
                                ),
                                "andpro.card",
                            )
                        totals["enriched"] += 1
                        merge_result = _save_spec(specs_cache, identity)
                        totals["spec_conflicts"] += len(merge_result.get("conflicts") or [])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "enrich failed for %s:%s: %s",
                            store,
                            product.get("external_id"),
                            type(exc).__name__,
                        )
                        totals["skipped_enrich"] += 1
                else:
                    totals["skipped_enrich"] += 1

                if identity is not None:
                    merge_result = _save_spec(specs_cache, identity)
                    totals["spec_conflicts"] += len(merge_result.get("conflicts") or [])
                    rows = identifiers_from_identity(identity)
                    # Fallback: always keep products.sku even if enrichment omitted it.
                    skus = {normalize_identifier(v) for t, v, _ in rows if t == "sku"}
                    for t, v, s in identifiers_from_product_row(product):
                        if normalize_identifier(v) not in skus:
                            rows.append((t, v, s))
                    row_stats = upsert_identifier_rows(conn, product_id, rows)
                elif not already:
                    row_stats = upsert_identifier_rows(
                        conn,
                        product_id,
                        identifiers_from_product_row(product),
                    )
                else:
                    # Identifiers already in DB; refresh last_seen only via no-op path.
                    row_stats = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}

                for key in ("inserted", "updated", "unchanged", "skipped"):
                    totals[key] += row_stats[key]

            totals["identifiers_after"] = count_product_identifiers(conn)
            totals["products_in_db"] = count_products(conn)
    finally:
        if regard_client is not None:
            regard_client.close()
        if andpro_client is not None:
            andpro_client.close()

    save_specs_cache(specs_cache, specs_path)
    return totals


def main() -> None:
    print("Синхронизация product_identifiers...")
    stats = sync_product_identifiers(DEFAULT_DB_PATH)
    print(f"Products: {stats['products']}")
    print(f"Обогащено из магазинов: {stats['enriched']}")
    print(f"Пропущено (уже есть): {stats['skipped_enrich']}")
    print(
        "Identifiers: "
        f"{stats['identifiers_before']} -> {stats['identifiers_after']} "
        f"(+{stats['inserted']} new, {stats['updated']} updated, "
        f"{stats['unchanged']} unchanged)"
    )


if __name__ == "__main__":
    main()
