from __future__ import annotations

"""SAFE live diagnostic for Thailand stores + FX + RU comparison (no production alerts)."""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deal_ranking import canonical_gpu
from russian_deals import load_russian_ranked_deals
from storage import DEFAULT_DB_PATH
from thailand.advice import collect as collect_advice
from thailand.banana import collect as collect_banana
from thailand.fx import fetch_cbr_thb_rate, thb_to_rub
from thailand.jib import collect as collect_jib
from thailand.matching import match_russian_to_thai
from thailand.scanner import build_thai_top, run_thailand_scan


def _row(o) -> dict:
    return {
        "store": o.store,
        "external_id": o.external_id,
        "model": o.name[:80],
        "sku_mpn": o.manufacturer_part_number or o.sku,
        "gpu": o.gpu,
        "cpu": o.cpu,
        "ram": o.ram_gb,
        "ssd": o.ssd_gb,
        "screen": o.screen_size_inch,
        "resolution": o.screen_resolution,
        "price_thb": o.price_thb,
        "availability": o.availability_status,
        "warranty": o.warranty,
        "url": o.url,
    }


def main() -> int:
    print("=== Thailand live diagnostic (non-production) ===")
    t0 = time.perf_counter()

    fx_t0 = time.perf_counter()
    fx = fetch_cbr_thb_rate()
    fx_dt = time.perf_counter() - fx_t0
    if fx:
        print(
            f"FX CBR nominal={fx.nominal} rate={fx.official_rate} "
            f"rub_per_thb={fx.rub_per_thb:.6f} date={fx.published_date} "
            f"runtime={fx_dt:.2f}s"
        )
    else:
        print(f"FX FAILED runtime={fx_dt:.2f}s")

    stores = []
    for name, fn in (("jib", collect_jib), ("advice", collect_advice), ("banana", collect_banana)):
        st = time.perf_counter()
        res = fn()
        dt = time.perf_counter() - st
        stores.append((name, res, dt))
        print(
            f"STORE {name}: ok={res.ok} count={res.count} "
            f"error={res.error} runtime={dt:.2f}s"
        )

    all_offers = []
    for name, res, _dt in stores:
        all_offers.extend(res.offers)

    print("\n=== Offers table ===")
    print(
        "store | external_id | model | SKU/MPN | GPU | CPU | RAM | SSD | screen | "
        "resolution | price THB | availability | warranty | URL"
    )
    for o in all_offers:
        r = _row(o)
        print(
            f"{r['store']} | {r['external_id']} | {r['model']} | {r['sku_mpn']} | "
            f"{r['gpu']} | {r['cpu']} | {r['ram']} | {r['ssd']} | {r['screen']} | "
            f"{r['resolution']} | {r['price_thb']} | {r['availability']} | "
            f"{r['warranty']} | {r['url']}"
        )

    counts = {"5070ti": {}, "5080": {}}
    for name, res, _ in stores:
        counts["5070ti"][name] = sum(
            1 for o in res.offers if canonical_gpu(o.gpu) == "RTX 5070 Ti"
        )
        counts["5080"][name] = sum(
            1 for o in res.offers if canonical_gpu(o.gpu) == "RTX 5080"
        )
    print("\nCounts 5070 Ti by store:", counts["5070ti"])
    print("Counts 5080 by store:", counts["5080"])

    if fx and all_offers:
        sample = all_offers[0]
        print(
            f"\nSample conversion: {sample.price_thb} THB ≈ "
            f"{thb_to_rub(sample.price_thb, fx)} RUB"
        )

    # Russian TOP from local/copy DB if present
    db = DEFAULT_DB_PATH
    print(f"\n=== Russian TOP from {db} ===")
    try:
        deals = load_russian_ranked_deals(db, limit=5)
        for i, d in enumerate(deals, 1):
            print(
                f"{i}. {d.cluster_name} | {d.gpu} | {d.price} RUB | "
                f"{d.store} | score={d.score} conf={d.confidence}"
            )
    except Exception as exc:
        deals = []
        print(f"Russian TOP unavailable: {type(exc).__name__}: {exc}")

    match_t0 = time.perf_counter()
    top = build_thai_top(all_offers, fx=fx, limit=5)
    print("\n=== Thailand TOP 5 (intl score) ===")
    for i, row in enumerate(top, 1):
        print(
            f"{i}. {row['name'][:60]} | {row['gpu']} | {row['price_thb']} THB | "
            f"≈{row['price_rub']} RUB | {row['store']} | "
            f"{row['availability_status']} | score={row['international_score']}"
        )

    if deals:
        price_map = {
            f"{o.store}:{o.external_id}": thb_to_rub(o.price_thb, fx) for o in all_offers
        }
        matches = match_russian_to_thai(deals[0], all_offers, thai_price_rub=price_map)
        print("\n=== Match vs Russian #1 ===")
        for m in matches[:5]:
            print(
                f"{m.level}: {m.thai_offer.name[:50]} | {m.thai_offer.store} | "
                f"{m.thai_price_thb} THB ≈ {m.thai_price_rub} RUB | {m.reason}"
            )

    best_5070 = next(
        (r for r in top if canonical_gpu(r.get("gpu")) == "RTX 5070 Ti"), None
    )
    best_5080 = next(
        (r for r in build_thai_top(all_offers, fx=fx, limit=50) if canonical_gpu(r.get("gpu")) == "RTX 5080"),
        None,
    )
    print("\nBest Thai 5070 Ti:", json.dumps(best_5070, ensure_ascii=False)[:500] if best_5070 else None)
    print("Best Thai 5080:", json.dumps(best_5080, ensure_ascii=False)[:500] if best_5080 else None)

    # Full orchestrated scan to snapshot (local data/)
    scan = run_thailand_scan(
        trigger_type="diagnostic",
        russian_deals=deals,
        write_snapshot=True,
        snapshot_dir=ROOT / "data" / "thailand_scans",
    )
    print(
        f"\nOrchestrated scan status={scan.get('status')} "
        f"snapshot={scan.get('snapshot_path')} "
        f"overall={scan.get('runtimes', {}).get('overall_seconds')}s"
    )
    print(f"TOTAL diagnostic runtime={time.perf_counter() - t0:.2f}s")

    ok_stores = sum(1 for _n, r, _d in stores if r.ok)
    gate = ok_stores >= 1 and (
        sum(counts["5070ti"].values()) + sum(counts["5080"].values()) > 0
    )
    print(f"\nFEASIBILITY GATE: {'PASS' if gate else 'FAIL'} (ok_stores={ok_stores})")
    return 0 if gate else 2


if __name__ == "__main__":
    raise SystemExit(main())
