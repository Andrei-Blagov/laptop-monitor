from __future__ import annotations

"""Read-only diagnostic: render price-history PNGs for a few models (no alerts)."""

import argparse
import re
import sys
from pathlib import Path


def _print(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(enc, errors="replace"))

from model_price_history import assert_history_readonly, build_history_picker_items
from price_history_chart import PNG_SIGNATURE, build_price_history_chart
from storage import (
    DEFAULT_DB_PATH,
    count_alert_events,
    open_db_readonly,
)


def _slug(name: str) -> str:
    # ASCII-ish filenames for portable diagnostic artifacts.
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return (text or "model")[:80]


def _alert_count(db: Path) -> int:
    try:
        conn = open_db_readonly(db)
    except FileNotFoundError:
        return 0
    try:
        return count_alert_events(conn)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data") / "_chart_diag",
        help="Directory for diagnostic PNGs (not for commit)",
    )
    parser.add_argument(
        "--names",
        nargs="*",
        default=["Maibenben X16F", "ASUS G614PR", "ASUS G615LR"],
        help="Substring match against model names",
    )
    parser.add_argument(
        "--product-id",
        type=int,
        action="append",
        default=[],
        help="Explicit product_id (repeatable); bypasses picker filter",
    )
    parser.add_argument("--period", choices=("30", "90", "all"), default="all")
    args = parser.parse_args()

    before = assert_history_readonly(args.db)
    alerts_before = _alert_count(args.db)
    items = build_history_picker_items(args.db, limit=40)
    selected = []
    for pid in args.product_id:
        selected.append(type("Item", (), {"product_id": int(pid), "name": f"id={pid}"})())
    for needle in args.names:
        hit = next(
            (i for i in items if needle.lower() in i.name.lower()),
            None,
        )
        if hit is None:
            # Fallback: scan all products for substring (diagnostic only).
            from storage import get_all_products_readonly

            prods = get_all_products_readonly(args.db)
            prod = next(
                (p for p in prods if needle.lower() in str(p.get("name") or "").lower()),
                None,
            )
            if prod is None:
                _print(f"WARN: not found: {needle}")
                continue
            selected.append(
                type(
                    "Item",
                    (),
                    {
                        "product_id": int(prod["id"]),
                        "name": str(prod.get("name") or needle),
                    },
                )()
            )
            _print(f"note: used product scan for {needle!r} -> id={prod['id']}")
            continue
        selected.append(hit)

    if not selected:
        # Fallback: top picker items
        selected = items[:3]
        _print(f"fallback to top {len(selected)} picker items")

    args.out.mkdir(parents=True, exist_ok=True)
    _print(f"db={args.db}")
    _print(f"out={args.out.resolve()}")
    _print(f"period={args.period}")

    for item in selected:
        result = build_price_history_chart(args.db, item.product_id, args.period)
        _print("---")
        _print(f"product_id={item.product_id} name={item.name}")
        if not result.ok or not result.png:
            _print(f"FAIL: {result.error}")
            continue
        path = args.out / f"{_slug(item.name)}_{args.period}.png"
        path.write_bytes(result.png)
        _print(f"ok bytes={len(result.png)} png={path.name}")
        _print(f"signature_ok={result.png.startswith(PNG_SIGNATURE)}")
        _print("caption:")
        _print(result.caption)

    after = assert_history_readonly(args.db)
    alerts_after = _alert_count(args.db)
    _print("---")
    _print(
        f"readonly products/history before={before} after={after} "
        f"unchanged={before == after}"
    )
    _print(
        f"alert_events before={alerts_before} after={alerts_after} "
        f"unchanged={alerts_before == alerts_after}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
