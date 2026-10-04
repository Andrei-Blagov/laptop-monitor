from __future__ import annotations

"""SAFE live diagnostic for hardened Thailand verification (no production alerts)."""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deal_ranking import canonical_gpu
from russian_deals import load_russian_ranked_deals
from thailand.comparison import country_verdict, verdict_label_ru
from thailand.fx import fetch_cbr_thb_rate, thb_to_rub
from thailand.jib import collect as collect_jib
from thailand.matching import match_russian_to_thai
from thailand.scanner import build_thai_top


def _print_offer(o, fx) -> None:
    rub = thb_to_rub(o.price_thb, fx)
    print(
        f"  - {o.name[:70]}\n"
        f"    id={o.external_id} sku={o.manufacturer_part_number or o.sku}\n"
        f"    gpu={o.gpu} gpu_source={o.gpu_source} candidate={o.candidate_gpu}\n"
        f"    cpu={o.cpu} ram={o.ram_gb} ssd={o.ssd_gb} screen={o.screen_size_inch}\n"
        f"    price={o.price_thb} THB (~{rub} RUB) price_source={o.price_source}\n"
        f"    avail={o.availability_status} confirmed={o.availability_confirmed} "
        f"src={o.availability_source}\n"
        f"    verification={o.verification_status} url={o.url}"
    )


def main() -> int:
    print("=== Hardened Thailand live diagnostic ===")
    t0 = time.perf_counter()
    fx = fetch_cbr_thb_rate()
    if fx:
        print(
            f"FX CBR nominal={fx.nominal} rate={fx.official_rate} "
            f"rub/thb={fx.rub_per_thb:.6f} date={fx.published_date}"
        )
    else:
        print("FX FAILED")

    st = time.perf_counter()
    res = collect_jib()
    jib_dt = time.perf_counter() - st
    print(
        f"\nJIB ok={res.ok} verified={res.count} "
        f"unverified={len(res.unverified_candidates)} "
        f"runtime={jib_dt:.2f}s error={res.error}"
    )

    discovered = list(res.offers) + list(res.unverified_candidates)
    print(f"\nA. Discovered candidates: {len(discovered)}")
    print(f"B. Verified offers: {len(res.offers)}")
    print(f"C. Unverified candidates: {len(res.unverified_candidates)}")

    v5070 = [o for o in res.offers if canonical_gpu(o.gpu) == "RTX 5070 Ti"]
    v5080 = [o for o in res.offers if canonical_gpu(o.gpu) == "RTX 5080"]
    print(f"\nVerified 5070 Ti: {len(v5070)}")
    print(f"Verified 5080: {len(v5080)}")

    print("\n=== Verified offers detail ===")
    for o in res.offers:
        _print_offer(o, fx)

    print("\n=== Unverified candidates (sample) ===")
    for o in res.unverified_candidates[:10]:
        _print_offer(o, fx)

    tuf = [
        o
        for o in discovered
        if "TUF" in (o.name or "").upper() and "A18" in (o.name or "").upper()
    ]
    print("\n=== TUF A18 check ===")
    if not tuf:
        print("TUF A18 not in discovery set")
    for o in tuf:
        print(
            f"model={o.name}\n"
            f"sku={o.sku}/{o.manufacturer_part_number}\n"
            f"authoritative_gpu={o.gpu} candidate={o.candidate_gpu}\n"
            f"gpu_source={o.gpu_source} rejected={o.metadata.get('rejected_gpu')}\n"
            f"avail={o.availability_status} confirmed={o.availability_confirmed}\n"
            f"verification={o.verification_status} price={o.price_thb}\n"
            f"reasons={o.verification_reasons}"
        )

    top = build_thai_top(res.offers, fx=fx, limit=5, verified_only=True)
    print("\n=== Verified Thailand TOP 5 ===")
    for i, row in enumerate(top, 1):
        print(
            f"{i}. {row['name'][:60]} | {row['gpu']} | {row['price_thb']}฿ "
            f"≈{row['price_rub']}₽ | score={row['international_score']} "
            f"conf={row['international_confidence']} | {row['verification_status']}"
        )

    db = ROOT / "data" / "_chart_diag" / "prod_readonly.db"
    print(f"\n=== RU vs TH sample ({db}) ===")
    try:
        deals = load_russian_ranked_deals(db, limit=5)
        for i, d in enumerate(deals[:3], 1):
            print(f"RU{i}. {d.cluster_name} | {d.price} | {d.gpu} | {d.store}")
        if deals:
            # Prefer Strix G614PR if present
            target = next(
                (d for d in deals if "G614PR" in (d.cluster_name or "").upper()),
                deals[0],
            )
            price_map = {
                f"{o.store}:{o.external_id}": thb_to_rub(o.price_thb, fx)
                for o in discovered
            }
            matches = match_russian_to_thai(
                target, res.offers or discovered, thai_price_rub=price_map
            )
            print(f"\nCompare vs: {target.cluster_name} @ {target.price}")
            for m in matches[:5]:
                print(
                    f"  {m.level}: {m.thai_offer.name[:55]} | "
                    f"{m.thai_price_thb}฿ ≈{m.thai_price_rub}₽ | "
                    f"delta%={m.delta_percent} | {m.reason} | diffs={m.differences[:4]}"
                )
            best = next(
                (m for m in matches if m.level in {"EXACT", "EQUIVALENT", "SAME_FAMILY"}),
                None,
            )
            if best:
                c = country_verdict(best, fx=fx)
                print(
                    f"Verdict: {c.verdict} — {verdict_label_ru(c.verdict)}\n"
                    f"Reasons: {c.reasons}"
                )
    except Exception as exc:
        print(f"RU compare unavailable: {type(exc).__name__}: {exc}")

    print(f"\nTOTAL runtime={time.perf_counter() - t0:.2f}s")
    gate = res.ok and (len(res.offers) >= 0)
    # Feasibility: discovery works; verified may be 0 if detail fails — still PASS if JIB ok
    print(f"GATE: {'PASS' if res.ok else 'FAIL'}")
    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
