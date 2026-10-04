from __future__ import annotations

"""Read-only diagnostic for Telegram model price history (no alerts / no writes)."""

import argparse
from pathlib import Path

from model_price_history import (
    assert_history_readonly,
    build_history_picker_items,
    build_model_history_report,
    format_delta_line,
    store_label,
)
from storage import DEFAULT_DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Path to laptop_monitor.db",
    )
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    before = assert_history_readonly(args.db)
    items = build_history_picker_items(args.db, limit=max(1, int(args.limit)))
    print(f"db={args.db}")
    print(f"picker_items={len(items)}")
    for item in items:
        report = build_model_history_report(args.db, item.product_id)
        print("---")
        print(f"product_id={item.product_id}")
        print(f"name={report.name}")
        print(f"sku={report.display_sku}")
        print(f"stale={report.stale_current}")
        print(
            "offers="
            + ", ".join(
                f"{store_label(s.store)}:{s.current_price}"
                f"{'' if s.available else '(na)'}"
                for s in report.stores
            )
        )
        print(
            f"best_now={report.best_now} ({report.best_now_store})"
            if report.best_now_store
            else f"best_now={report.best_now}"
        )
        print(
            f"all_time_min={report.all_time_min} ({report.all_time_min_store}) "
            f"at={report.all_time_min_at}"
        )
        print(
            f"7d={report.best_7d_ago} delta={format_delta_line(report.best_now, report.best_7d_ago)}"
        )
        print(
            f"30d={report.best_30d_ago} delta={format_delta_line(report.best_now, report.best_30d_ago)}"
        )
    after = assert_history_readonly(args.db)
    print("---")
    print(f"readonly_counts_before={before} after={after} unchanged={before == after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
