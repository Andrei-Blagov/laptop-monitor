#!/usr/bin/env python3
"""Run one Thailand adapter directly, including stores disabled in the automatic scan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from thailand.registry import get_thailand_adapters


def _ru_top(index: int, *, db: str | None, live: bool, timeout: float) -> int:
    from russian_deals import load_russian_ranked_deals
    from storage import DEFAULT_DB_PATH
    from thailand.target_model import build_target_model, search_collected_offers

    deals = load_russian_ranked_deals(Path(db) if db else DEFAULT_DB_PATH, limit=10)
    if index < 1 or index > len(deals):
        print(json.dumps({"ok": False, "error": "index_out_of_range", "count": len(deals)}))
        return 2
    deal = deals[index - 1]
    target = build_target_model(deal)
    print(json.dumps({"target_model": target, "picker_index": index}, ensure_ascii=False, default=str))
    if not live:
        return 0
    from thailand import invadeit, itcity, jib

    offers = []
    report = {}
    for name, fn in (("jib", jib.collect), ("invadeit", invadeit.collect), ("itcity", itcity.collect)):
        kwargs = {"timeout": timeout}
        if name == "itcity":
            kwargs["target_codes"] = list(target.get("model_codes") or [])[:1]
        result = fn(**kwargs)
        report[name] = {
            "ok": result.ok,
            "error_code": result.error_code,
            "requests": result.request_count,
            "verified": result.count,
        }
        offers.extend(result.offers)
        offers.extend(result.unverified_candidates or [])
    report["speedcom"] = {"ok": True, "error_code": "RATE_LIMITED", "requests": 0, "note": "skipped"}
    report["advice"] = {"ok": True, "error_code": "NO_RESULTS", "requests": 0, "note": "no exact endpoint"}
    found = search_collected_offers(target, offers)
    print(json.dumps({"stores": report, "target_search": found}, ensure_ascii=False, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thailand store or Russian-TOP model diagnostic")
    parser.add_argument("--store", default=None, help="jib, advice, speedcom, invadeit, itcity, banana, lazada")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--ru-top", type=int, default=None, help="1-based row of the current Russian TOP")
    parser.add_argument("--db", default=None)
    parser.add_argument("--live", action="store_true", help="One network search. Skips SpeedCom.")
    args = parser.parse_args(argv)
    if args.ru_top is not None:
        return _ru_top(args.ru_top, db=args.db, live=args.live, timeout=args.timeout)
    if not args.store:
        parser.error("--store or --ru-top is required")
    slug = args.store.strip().lower()
    adapter = next((a for a in get_thailand_adapters() if a.slug == slug), None)
    if adapter is None:
        print(json.dumps({"ok": False, "error": "unknown_store", "store": slug}))
        return 2
    result = adapter.collect(timeout=args.timeout)
    print(
        json.dumps(
            {
                "store": result.store,
                "ok": result.ok,
                "error_code": result.error_code,
                "technical_detail": result.technical_detail,
                "collection_mode": result.collection_mode,
                "discovered": result.discovered_count,
                "verified": result.verified_count,
                "offers": result.count,
                "duration_seconds": result.duration_seconds,
                "policy_enabled": adapter.enabled,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
