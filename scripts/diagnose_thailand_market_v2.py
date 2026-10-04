from __future__ import annotations

"""Safe live diagnostic for Thailand Market Quality v2 (no Telegram, no RU pipeline)."""

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
from thailand.fx import fetch_cbr_thb_rate, fx_usable_for_verdict, thb_to_rub
from thailand.registry import get_thailand_adapters
from thailand.scanner import run_thailand_scan
from thailand.verification import is_verified_for_ranking


def _brief_offer(o) -> dict:
    d = o.to_dict() if hasattr(o, "to_dict") else dict(o)
    keys = [
        "store",
        "name",
        "sku",
        "manufacturer_part_number",
        "gpu",
        "cpu",
        "ram_gb",
        "ssd_gb",
        "screen_size_inch",
        "price_thb",
        "availability_status",
        "verification_status",
        "url",
        "marketplace",
        "seller_name",
        "seller_trust_tier",
        "official_store",
        "mall",
        "price_verified_for_variant",
        "in_tracking_scope",
        "excluded_reason",
        "variant_id",
    ]
    return {k: d.get(k) for k in keys}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    if args.no_browser:
        config.THAILAND_BANANA_BROWSER_ENABLED = False
        config.THAILAND_LAZADA_BROWSER_ENABLED = False

    print("=== Thailand Market Quality v2 diagnostic ===")
    print(f"MAX_TRACKED_PRICE_RUB={max_tracked_price_rub()}")
    fx = fetch_cbr_thb_rate()
    print(f"fx_usable={fx_usable_for_verdict(fx)}")
    if fx:
        print(f"rub_per_thb={fx.rub_per_thb}")
        print(f"effective_thb_cap={effective_thb_cap(fx)}")

    t0 = time.perf_counter()
    # Per-store probe first
    for adapter in get_thailand_adapters():
        if not adapter.enabled:
            continue
        st = time.perf_counter()
        try:
            res = adapter.collect(timeout=config.THAILAND_PER_STORE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{adapter.slug}] ERROR {type(exc).__name__}")
            continue
        verified = [o for o in res.offers if is_verified_for_ranking(o)]
        print(
            f"\n[{adapter.slug}] ok={res.ok} mode={res.collection_mode} "
            f"error={res.error} dur={res.duration_seconds or (time.perf_counter()-st):.1f}s "
            f"verified={len(verified)} unverified={len(res.unverified_candidates)}"
        )
        for o in verified[:8]:
            rub = thb_to_rub(o.price_thb, fx)
            print(
                f"  - {o.name[:70]} | {o.gpu} | {o.price_thb}฿ ≈{rub}₽ | "
                f"{o.availability_status} | seller={o.seller_name} tier={o.seller_trust_tier} | {o.url}"
            )

    scan = run_thailand_scan(
        trigger_type="diagnostic_market_v2",
        russian_deals=None,
        trigger_signals=None,
        write_snapshot=False,
    )
    elapsed = time.perf_counter() - t0
    print("\n=== SCAN SUMMARY ===")
    print(f"status={scan.get('status')} overall_s={elapsed:.1f}")
    print("price_cap=", json.dumps(scan.get("price_cap"), ensure_ascii=False))
    print("stores=", json.dumps(scan.get("stores"), ensure_ascii=False))
    print("\nValue TOP:")
    for i, row in enumerate(scan.get("thailand_top") or [], 1):
        print(
            f"  {i}. {row.get('store_label') or row.get('store')} | "
            f"{row.get('gpu')} | {row.get('price_thb')}฿ ≈{row.get('price_rub')}₽ | "
            f"score={row.get('international_score')} | {(row.get('name') or '')[:60]}"
        )
    print("\nCheapest eligible:")
    for i, row in enumerate(scan.get("cheapest_eligible_thailand") or [], 1):
        print(
            f"  {i}. {row.get('store_label') or row.get('store')} | "
            f"{row.get('price_thb')}฿ ≈{row.get('price_rub')}₽ | "
            f"{(row.get('name') or '')[:60]}"
        )
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in scan.items() if not str(k).startswith("_")}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
