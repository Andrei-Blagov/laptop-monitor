#!/usr/bin/env python3
"""Run one Thailand adapter directly, including stores disabled in the automatic scan."""

from __future__ import annotations

import argparse
import json

from thailand.registry import get_thailand_adapters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect one Thailand store, bypassing the circuit breaker")
    parser.add_argument("--store", required=True, help="jib, advice, speedcom, invadeit, itcity, banana, lazada")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
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
