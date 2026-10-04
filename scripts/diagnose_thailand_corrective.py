from __future__ import annotations

"""Safe live diagnostic for Thailand corrective pass (no Telegram, no RU pipeline)."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from thailand.eligibility import effective_thb_cap, max_tracked_price_rub
from thailand.formatting import format_thailand_alternatives_message
from thailand.fx import fetch_cbr_thb_rate, fx_usable_for_verdict, thb_to_rub
from thailand.lazada import LAZADA_SEARCH_PLAN
from thailand.scanner import run_thailand_scan
from thailand.seller_trust import is_banana_it_seller, store_display_label


def _print_table(rows: list[dict], *, title: str) -> None:
    print(f"\n=== {title} ===")
    print(
        f"{'rank':>4}  {'RUB':>9}  {'THB':>8}  {'score':>6}  {'conf':>5}  "
        f"{'source':<22}  {'seller':<22}  GPU  model"
    )
    for i, row in enumerate(rows, 1):
        print(
            f"{i:4d}  {row.get('price_rub') or 0:9d}  {row.get('price_thb') or 0:8d}  "
            f"{(row.get('international_score') or 0):6.1f}  "
            f"{(row.get('international_confidence') or 0):5.0f}  "
            f"{str(row.get('store_label') or row.get('store') or '')[:22]:<22}  "
            f"{str(row.get('seller_name') or '-')[:22]:<22}  "
            f"{row.get('gpu')}  {(row.get('name') or '')[:48]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    if args.no_browser:
        config.THAILAND_BANANA_BROWSER_ENABLED = False
        config.THAILAND_LAZADA_BROWSER_ENABLED = False

    print("=== Thailand corrective-pass diagnostic ===")
    print(f"MAX_TRACKED_PRICE_RUB={max_tracked_price_rub()}")
    print(f"LAZADA_SEARCH_PLAN queries={len(LAZADA_SEARCH_PLAN)}")
    for q, hint in LAZADA_SEARCH_PLAN:
        print(f"  - {q!r} hint={hint!r}")

    fx = fetch_cbr_thb_rate()
    print(f"\nfx_usable={fx_usable_for_verdict(fx)}")
    if fx:
        print(f"source={fx.source} rub_per_thb={fx.rub_per_thb}")
        print(f"published_date={fx.published_date} stale={fx.stale}")
        print(f"effective_thb_cap={effective_thb_cap(fx)}")

    t0 = time.perf_counter()
    # Single collect via scan (no double Lazada browser pass).
    scan = run_thailand_scan(
        trigger_type="diagnostic_corrective",
        russian_deals=None,
        trigger_signals=None,
        write_snapshot=False,
    )
    elapsed = time.perf_counter() - t0

    per_store: dict[str, dict] = {}
    for row in scan.get("stores") or []:
        per_store[str(row.get("store"))] = {
            "ok": row.get("ok"),
            "mode": row.get("collection_mode"),
            "error": row.get("error"),
            "duration_seconds": row.get("duration_seconds"),
            "verified": row.get("count"),
        }
        print(
            f"\n[{row.get('store')}] ok={row.get('ok')} mode={row.get('collection_mode')} "
            f"error={row.get('error')} dur={row.get('duration_seconds')} "
            f"verified={row.get('count')}"
        )

    banana_it_hits: list[dict] = []
    for o in scan.get("all_verified_offers") or scan.get("offers") or []:
        store = o.get("store")
        rub = o.get("price_thb")
        rub = thb_to_rub(rub, fx) if rub is not None else None
        label = store_display_label(o)
        if store in {"jib", "advice", "banana", "lazada"}:
            print(
                f"  - {label} | {(o.get('name') or '')[:64]} | {o.get('gpu')} | "
                f"{o.get('price_thb')}฿ ≈{rub}₽ | {o.get('availability_status')} | "
                f"seller={o.get('seller_name')} tier={o.get('seller_trust_tier')} "
                f"brand={o.get('retailer_brand')} | {o.get('url')}"
            )
        if store == "lazada" and (
            o.get("retailer_brand") == "BaNANA" or is_banana_it_seller(o.get("seller_name"))
        ):
            banana_it_hits.append(
                {
                    "model": o.get("name"),
                    "seller": o.get("seller_name"),
                    "gpu": o.get("gpu"),
                    "cpu": o.get("cpu"),
                    "ram_gb": o.get("ram_gb"),
                    "ssd_gb": o.get("ssd_gb"),
                    "price_thb": o.get("price_thb"),
                    "price_rub": rub,
                    "availability": o.get("availability_status"),
                    "price_verified_for_variant": o.get("price_verified_for_variant"),
                    "seller_trust_tier": o.get("seller_trust_tier"),
                    "channel": o.get("channel"),
                    "retailer_brand": o.get("retailer_brand"),
                    "url": o.get("url"),
                }
            )

    print("\n=== BaNANA IT via Lazada ===")
    if not banana_it_hits:
        print("NOT FOUND (no fake results).")
    else:
        for hit in banana_it_hits:
            print(json.dumps(hit, ensure_ascii=False, indent=2))

    cheapest = scan.get("cheapest_eligible_thailand") or scan.get("thailand_top") or []
    value = scan.get("value_ranked_eligible_thailand") or []
    top = cheapest

    _print_table(top[:5], title="CHEAPEST TOP5 (RUB ASC)")
    _print_table(value[:5], title="VALUE RANKED TOP5 (diagnostic)")

    preview = format_thailand_alternatives_message(
        top[:5],
        price_cap=scan.get("price_cap"),
        unverified_count=int(scan.get("unverified_count") or 0),
    )
    print("\n=== TELEGRAM PREVIEW ===")
    print(preview or "(empty)")

    print("\n=== COUNTS ===")
    print(f"direct_banana_offers={scan.get('direct_banana_offers')}")
    print(f"lazada_banana_offers={scan.get('lazada_banana_offers')}")
    print(f"lazada_jib_offers={scan.get('lazada_jib_offers')}")
    print(f"official_brand_store_offers={scan.get('official_brand_store_offers')}")
    print(f"duplicates_removed_from_top={scan.get('duplicates_removed_from_top')}")
    print(f"over_cap_offers_count={scan.get('over_cap_offers_count')}")
    print(f"price_cap={json.dumps(scan.get('price_cap'), ensure_ascii=False)}")
    print(f"stores={json.dumps(scan.get('stores'), ensure_ascii=False)}")
    print(f"runtimes={json.dumps(scan.get('runtimes'), ensure_ascii=False)}")
    print(f"per_store={json.dumps(per_store, ensure_ascii=False)}")
    print(f"overall_wall_seconds={elapsed:.1f}")

    # Strict ascending check
    rubs = [r.get("price_rub") for r in top if r.get("price_rub") is not None]
    print(f"strict_rub_asc={rubs == sorted(rubs)} rubs={rubs}")

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in scan.items() if not str(k).startswith("_")}
        payload["per_store_probe"] = per_store
        payload["banana_it_hits"] = banana_it_hits
        payload["telegram_preview"] = preview
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
