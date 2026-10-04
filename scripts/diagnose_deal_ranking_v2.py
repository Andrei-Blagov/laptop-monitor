from __future__ import annotations

"""Read-only OLD vs NEW TOP diagnostic for deal ranking v2."""

import argparse
import sys
import time
from pathlib import Path

import config
from comparison import match_products
from deal_ranking import (
    collect_match_product_ids,
    format_top_deals_message,
    historical_mins_from_rows,
    rank_clusters,
    score_offer,
)
from identity_sync import identity_from_cache, load_specs_cache
from storage import (
    DEFAULT_DB_PATH,
    count_alert_events,
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
    open_db_readonly,
)
from store_freshness import get_fresh_store_slugs
from stores.common import extract_specs_from_name


def _print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(enc, errors="replace"))


def _legacy_score(
    *,
    price: int | None,
    gpu: str | None,
    ram_gb: int | None,
    ssd_gb: int | None,
    screen_inch: float | None,
    saving_vs_next: int | None,
) -> float:
    """Pre-v2 additive score for comparison only (not used in production)."""
    score = 0.0
    if gpu:
        g = gpu.upper()
        if "5080" in g and "TI" not in g:
            score += 25.0
        elif "5070" in g and "TI" in g:
            score += 18.0
    if ram_gb is not None:
        if ram_gb >= 32:
            score += 12.0
        elif ram_gb >= 16:
            score += 4.0
    if ssd_gb is not None and ssd_gb >= 1000:
        score += 8.0
    if screen_inch is not None:
        if screen_inch >= 17:
            score += 10.0
        elif screen_inch >= 16:
            score += 4.0
    if price is not None and gpu:
        g = gpu.upper()
        target = None
        if "5080" in g and "TI" not in g:
            target = 300_000
        elif "5070" in g and "TI" in g:
            target = 230_000
        if target is not None:
            delta = target - price
            score += max(-20.0, min(40.0, delta / 5000.0 * 10.0))
        if price <= 200_000:
            score += 15.0
        elif price <= 250_000:
            score += 6.0
    if saving_vs_next and saving_vs_next >= 10_000:
        score += min(25.0, saving_vs_next / 1000.0)
    return round(score, 2)


def _specs_for(best, match, cache):
    gpu = ram = ssd = screen = cpu = resolution = None
    if cache:
        ident = identity_from_cache(cache, best.store, best.external_id)
        if ident is not None:
            gpu = ident.gpu
            ram = ident.ram_gb
            ssd = ident.ssd_gb
            screen = ident.screen_size_inch
            cpu = ident.cpu
            resolution = ident.screen_resolution
    inferred = extract_specs_from_name(
        " ".join(x for x in (match.name, best.name, best.url or "") if x)
    )
    return {
        "gpu": gpu or inferred.get("gpu"),
        "ram_gb": ram if ram is not None else inferred.get("ram_gb"),
        "ssd_gb": ssd if ssd is not None else inferred.get("ssd_gb"),
        "screen_inch": screen if screen is not None else inferred.get("screen_inch"),
        "cpu": cpu or inferred.get("cpu"),
        "screen_resolution": resolution,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    products = get_all_products_readonly(args.db)
    identifiers = get_all_identifiers_readonly(args.db)
    matches = match_products(products, identifiers).matches
    fresh = get_fresh_store_slugs(get_latest_store_runs_readonly(args.db))
    try:
        cache = load_specs_cache()
    except Exception:
        cache = {}

    specs_by_key = {}
    for p in products:
        identity = identity_from_cache(cache, str(p["store"]), str(p["external_id"]))
        if identity is not None:
            specs_by_key[(str(p["store"]), str(p["external_id"]))] = identity

    pids = collect_match_product_ids(matches)
    t0 = time.perf_counter()
    hist_mins = historical_mins_from_rows(
        get_price_history_for_products_readonly(args.db, pids)
    )
    t_hist = time.perf_counter() - t0

    t1 = time.perf_counter()
    new_deals = rank_clusters(
        matches,
        specs_by_key=specs_by_key,
        fresh_stores=fresh,
        historical_mins=hist_mins,
        limit=None,
    )
    t_new = time.perf_counter() - t1

    # Build legacy ranking on same eligible clusters.
    legacy_rows = []
    t2 = time.perf_counter()
    for match in matches:
        available = [
            o
            for o in match.offers
            if o.available
            and o.price is not None
            and config.is_price_in_tracking_scope(o.price)
            and (not fresh or o.store in fresh)
        ]
        if not available:
            continue
        ordered = sorted(available, key=lambda o: (int(o.price), o.store))
        best = ordered[0]
        nxt = ordered[1] if len(ordered) > 1 else None
        saving = int(nxt.price) - int(best.price) if nxt else None
        specs = _specs_for(best, match, cache)
        old_score = _legacy_score(
            price=int(best.price),
            gpu=specs["gpu"],
            ram_gb=specs["ram_gb"],
            ssd_gb=specs["ssd_gb"],
            screen_inch=specs["screen_inch"],
            saving_vs_next=saving,
        )
        legacy_rows.append(
            {
                "name": match.name,
                "score": old_score,
                "price": int(best.price),
                "store": best.store,
                "gpu": specs["gpu"],
                "cpu": specs["cpu"],
                "ram_gb": specs["ram_gb"],
                "ssd_gb": specs["ssd_gb"],
                "screen_inch": specs["screen_inch"],
            }
        )
    legacy_rows.sort(key=lambda r: (-r["score"], r["price"], r["name"]))
    t_old = time.perf_counter() - t2

    _print(f"db={args.db}")
    _print(f"fresh={sorted(fresh)}")
    _print(f"eligible_clusters={len(new_deals)}")
    _print(f"runtime_hist_context_s={t_hist:.4f}")
    _print(f"runtime_legacy_rank_s={t_old:.4f}")
    _print(f"runtime_new_rank_s={t_new:.4f}")
    _print("")
    _print("=== OLD TOP ===")
    for i, row in enumerate(legacy_rows[: args.limit], start=1):
        _print(
            f"{i}. {row['name'][:70]} | {row['gpu']} | CPU={row['cpu']} | "
            f"{row['ram_gb']}GB/{row['ssd_gb']}GB/{row['screen_inch']}\" | "
            f"{row['price']} {row['store']} | old={row['score']}"
        )
    _print("")
    _print("=== NEW TOP ===")
    for i, deal in enumerate(new_deals[: args.limit], start=1):
        delta = None
        if deal.historical_min and deal.price:
            delta = deal.price - deal.historical_min
        _print(
            f"{i}. {deal.cluster_name[:70]} | {deal.gpu} | CPU={deal.cpu} | "
            f"{deal.ram_gb}GB/{deal.ssd_gb}GB/{deal.screen_inch}\" | "
            f"{deal.price} {deal.store} | new={deal.score} conf={deal.confidence}% "
            f"hist_min={deal.historical_min} delta={delta}"
        )
        _print(f"   breakdown={deal.score_breakdown}")
        _print(f"   reasons={deal.reasons}")

    old_names = [r["name"] for r in legacy_rows[: args.limit]]
    new_names = [d.cluster_name for d in new_deals[: args.limit]]
    _print("")
    _print("=== ORDER DELTA ===")
    for name in new_names:
        old_pos = old_names.index(name) + 1 if name in old_names else None
        new_pos = new_names.index(name) + 1
        _print(f"NEW#{new_pos} <- OLD#{old_pos}: {name[:80]}")

    _print("")
    _print("=== SAMPLE TELEGRAM ===")
    _print(format_top_deals_message(new_deals, limit=min(3, args.limit)))

    try:
        conn = open_db_readonly(args.db)
        alerts = count_alert_events(conn)
        conn.close()
    except Exception:
        alerts = None
    _print(f"alert_events={alerts} (read-only check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
