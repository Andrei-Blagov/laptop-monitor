from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from enrichment import _headers, enrich_andpro_product, enrich_regard_product
from product_identity import ProductIdentity, normalize_identifier
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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _product_enriched(identifiers: list[dict], store: str) -> bool:
    sources = [str(row.get("source") or "") for row in identifiers]
    if store == "regard":
        return any(s.startswith("regard.api") for s in sources)
    if store == "andpro":
        return any(s.startswith("andpro.card") for s in sources)
    return True


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


def _save_spec(cache: dict[str, Any], identity: ProductIdentity) -> None:
    key = f"{identity.store}:{identity.external_id}"
    cache[key] = identity.to_dict()


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
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


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
    - повторный запуск не создаёт дублей (UNIQUE).
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

                if enrich and not already and store in {"regard", "andpro"}:
                    if store == "regard":
                        if regard_client is None:
                            regard_client = httpx.Client(
                                headers=_headers(True),
                                follow_redirects=True,
                                timeout=30.0,
                            )
                        identity = enrich_regard_product(product, client=regard_client)
                    else:
                        if andpro_client is None:
                            andpro_client = httpx.Client(
                                headers=_headers(False),
                                follow_redirects=True,
                                timeout=40.0,
                            )
                        identity = enrich_andpro_product(product, client=andpro_client)
                    totals["enriched"] += 1
                    _save_spec(specs_cache, identity)
                else:
                    totals["skipped_enrich"] += 1

                if identity is not None:
                    _save_spec(specs_cache, identity)
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
