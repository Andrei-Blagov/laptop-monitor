from __future__ import annotations

"""Load current fresh Russian ranked deals (read-only). Shared by pipeline + control bot."""

from pathlib import Path
from typing import Any

import config
from comparison import match_products
from deal_ranking import (
    RankedDeal,
    collect_match_product_ids,
    historical_mins_from_rows,
    history_starts_from_rows,
    rank_clusters,
)
from identity_sync import identity_from_cache, load_specs_cache
from store_freshness import get_fresh_store_slugs
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
)


def load_russian_ranked_deals(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    limit: int | None = None,
) -> list[RankedDeal]:
    latest = get_latest_store_runs_readonly(db_path)
    fresh = get_fresh_store_slugs(latest)
    if not fresh:
        return []
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    comparison = match_products(products, identifiers)
    cache = load_specs_cache()
    specs_by_key: dict[tuple[str, str], Any] = {}
    for p in products:
        identity = identity_from_cache(cache, str(p["store"]), str(p["external_id"]))
        if identity is not None:
            specs_by_key[(str(p["store"]), str(p["external_id"]))] = identity
    pids = collect_match_product_ids(comparison.matches)
    histories = get_price_history_for_products_readonly(db_path, pids)
    return rank_clusters(
        comparison.matches,
        specs_by_key=specs_by_key,
        limit=int(limit if limit is not None else config.TOP_DEALS_LIMIT),
        fresh_stores=fresh,
        historical_mins=historical_mins_from_rows(histories),
        history_starts=history_starts_from_rows(histories),
    )
