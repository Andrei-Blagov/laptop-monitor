from __future__ import annotations

"""Read-only specification coverage diagnostic for Deal Ranking v2."""

import argparse
import sys
from pathlib import Path
from typing import Any

import config
from comparison import match_products
from deal_ranking import (
    collect_match_product_ids,
    historical_mins_from_rows,
    rank_clusters,
)
from identity_sync import identity_from_cache, load_specs_cache
from product_identity import ProductIdentity
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
)
from store_freshness import get_fresh_store_slugs
from stores.common import extract_specs_from_name


FIELDS = ("gpu", "cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution", "history")


def _print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(enc, errors="replace"))


def _specs_map(cache: dict[str, Any]) -> dict[tuple[str, str], ProductIdentity]:
    out: dict[tuple[str, str], ProductIdentity] = {}
    for key, raw in cache.items():
        if not isinstance(raw, dict) or ":" not in key:
            continue
        store, external_id = key.split(":", 1)
        identity = identity_from_cache(cache, store, external_id)
        if identity is not None:
            out[(store, external_id)] = identity
    return out


def _field_source(
    deal_field: str,
    offers: list[Any],
    specs_by_key: dict[tuple[str, str], ProductIdentity],
    value: Any,
) -> str:
    if value is None or value == "":
        return "unknown"
    attr = "screen_size_inch" if deal_field == "screen_inch" else deal_field
    for offer in offers:
        identity = specs_by_key.get((offer.store, offer.external_id))
        if identity is None:
            continue
        iv = getattr(identity, attr, None)
        if iv is None and deal_field == "screen_inch":
            iv = getattr(identity, "screen_inch", None)
        if iv is None:
            continue
        prov = (identity.source_identifiers or {}).get("field_provenance") or {}
        src = prov.get(attr) or prov.get(deal_field)
        if src:
            return str(src)
        # Structured cache without explicit provenance.
        sources = str((identity.source_identifiers or {}))
        if "regard.api" in sources or identity.store == "regard":
            return "specs cache"
        if "andpro.card" in sources or identity.store == "andpro":
            return "specs cache"
        return "specs cache"
    # Title fallback across offer names.
    blob = " ".join(
        x
        for o in offers
        for x in (getattr(o, "name", None), getattr(o, "url", None))
        if x
    )
    inferred = extract_specs_from_name(blob)
    title_key = "screen_inch" if deal_field == "screen_inch" else deal_field
    if inferred.get(title_key) is not None:
        return "product title"
    return "unknown"


def diagnose(
    *,
    db_path: Path,
    specs_path: Path,
    details: bool = False,
) -> dict[str, Any]:
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    fresh = get_fresh_store_slugs(get_latest_store_runs_readonly(db_path))
    matches = match_products(products, identifiers).matches
    cache = load_specs_cache(specs_path)
    specs_by_key = _specs_map(cache)
    pids = collect_match_product_ids(matches)
    hist_rows = get_price_history_for_products_readonly(db_path, pids) if pids else []
    historical_mins = historical_mins_from_rows(hist_rows)
    ranked = rank_clusters(
        matches,
        specs_by_key=specs_by_key,
        fresh_stores=fresh,
        historical_mins=historical_mins,
    )

    rows: list[dict[str, Any]] = []
    coverage = {f: 0 for f in FIELDS if f != "history"}
    coverage["history"] = 0

    match_by_name = {m.name: m for m in matches}
    for deal in ranked:
        match = match_by_name.get(deal.cluster_name)
        offers = list(getattr(match, "offers", []) or []) if match else []
        avail = [
            o
            for o in offers
            if o.available
            and o.price is not None
            and config.is_price_in_tracking_scope(o.price)
            and o.store in fresh
        ]
        best = sorted(avail, key=lambda o: (int(o.price), o.store))[0] if avail else None
        values = {
            "gpu": deal.gpu,
            "cpu": deal.cpu,
            "ram_gb": deal.ram_gb,
            "ssd_gb": deal.ssd_gb,
            "screen_inch": deal.screen_inch,
            "screen_resolution": deal.screen_resolution,
            "history": deal.historical_min,
        }
        sources = {
            field: _field_source(field, offers, specs_by_key, values[field])
            for field in ("gpu", "cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")
        }
        sources["history"] = "price_history" if deal.historical_min is not None else "unknown"
        for field in FIELDS:
            if values[field] is not None and values[field] != "":
                coverage[field] += 1
        rows.append(
            {
                "cluster_name": deal.cluster_name,
                "sku": (best.sku if best else None) or (deal.offer or {}).get("sku"),
                "matched_stores": sorted({o.store for o in avail}),
                "best_store": deal.store,
                "best_price": deal.price,
                "score": deal.score,
                "confidence": deal.confidence,
                "values": values,
                "sources": sources,
                "missing": [f for f in FIELDS if values[f] is None or values[f] == ""],
            }
        )

    n = len(rows) or 1
    result = {
        "eligible_models": len(rows),
        "coverage_counts": coverage,
        "coverage_pct": {k: round(100.0 * v / n, 1) for k, v in coverage.items()},
        "rows": rows,
        "db_products": len(products),
        "cache_keys": len(cache),
    }

    _print("=== SPEC COVERAGE DIAGNOSTIC (read-only) ===")
    _print(f"db: {db_path}")
    _print(f"specs: {specs_path}")
    _print(
        f"eligible fresh <= {config.MAX_TRACKED_PRICE_RUB}: "
        f"{result['eligible_models']}"
    )
    _print(f"db products: {result['db_products']} | specs cache keys: {result['cache_keys']}")
    _print("")
    _print("Field coverage:")
    for field in FIELDS:
        _print(
            f"  {field:18} {coverage[field]}/{result['eligible_models']} "
            f"({result['coverage_pct'][field]}%)"
        )
    _print("")
    for i, row in enumerate(rows, start=1):
        _print(
            f"{i:2}. {row['best_price']} {row['best_store']:8} "
            f"conf={row['confidence']:3} score={row['score']:5.1f} | "
            f"{row['cluster_name'][:70]}"
        )
        _print(f"    stores={','.join(row['matched_stores'])} sku={row['sku']}")
        vals = row["values"]
        srcs = row["sources"]
        _print(
            "    "
            f"GPU={vals['gpu']} [{srcs['gpu']}] | "
            f"CPU={vals['cpu']} [{srcs['cpu']}] | "
            f"RAM={vals['ram_gb']} [{srcs['ram_gb']}]"
        )
        _print(
            "    "
            f"SSD={vals['ssd_gb']} [{srcs['ssd_gb']}] | "
            f"screen={vals['screen_inch']} [{srcs['screen_inch']}] | "
            f"res={vals['screen_resolution']} [{srcs['screen_resolution']}] | "
            f"hist={vals['history']} [{srcs['history']}]"
        )
        if row["missing"]:
            _print(f"    missing: {', '.join(row['missing'])}")
        if details:
            for offer in offers:
                key = (offer.store, offer.external_id)
                identity = specs_by_key.get(key)
                _print(
                    f"      offer {offer.store}:{offer.external_id} "
                    f"price={offer.price} cache={'yes' if identity else 'no'} "
                    f"name={(offer.name or '')[:90]}"
                )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--specs",
        type=Path,
        default=Path("data") / "product_specs.json",
    )
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args(argv)
    diagnose(db_path=args.db, specs_path=args.specs, details=args.details)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
