#!/usr/bin/env python3
"""
One-off: удалить неотправленные alert_events и пересчитать monitor.

Не трогает:
- alert_events со delivery status=sent
- alert_events со delivery status=skipped (baseline)

Usage:
  python scripts/rebuild_unsent_alerts.py
  python scripts/rebuild_unsent_alerts.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from alerts import run_monitor
from storage import (
    DEFAULT_DB_PATH,
    count_alert_events,
    delete_unsent_alert_events,
    get_delivery_stats,
    init_db,
    list_unsent_alert_event_ids,
    open_db,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Удалить unsent alert_events и пересчитать monitor"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать, сколько events было бы удалено",
    )
    parser.add_argument(
        "--skip-monitor",
        action="store_true",
        help="Только cleanup, без повторного run_monitor",
    )
    args = parser.parse_args(argv)

    with open_db(DEFAULT_DB_PATH) as conn:
        init_db(conn)
        before_events = count_alert_events(conn)
        before_delivery = get_delivery_stats(
            conn, channel=config.CHANNEL_TELEGRAM
        )
        unsent_ids = list_unsent_alert_event_ids(
            conn,
            channel=config.CHANNEL_TELEGRAM,
            destination=config.TELEGRAM_DESTINATION,
        )
        if args.dry_run:
            print("DRY RUN rebuild_unsent_alerts:")
            print(f"  alert_events before: {before_events}")
            print(f"  would delete events: {len(unsent_ids)}")
            print(f"  event ids: {unsent_ids[:20]}{'...' if len(unsent_ids) > 20 else ''}")
            print(f"  protected skipped/sent approx: {before_events - len(unsent_ids)}")
            print(f"  delivery stats now: {before_delivery}")
            return 0

        result = delete_unsent_alert_events(
            conn,
            channel=config.CHANNEL_TELEGRAM,
            destination=config.TELEGRAM_DESTINATION,
        )
        after_delete = count_alert_events(conn)

    print("Cleanup unsent alert_events:")
    print(f"  deleted_events: {result['deleted_events']}")
    print(f"  deleted_deliveries: {result['deleted_deliveries']}")
    print(f"  protected_events: {result['protected_events']}")
    print(f"  alert_events: {before_events} -> {after_delete}")

    if args.skip_monitor:
        return 0

    print()
    print("Re-run monitor...")
    monitor = run_monitor(DEFAULT_DB_PATH)
    print(f"PRICE_DROP: {monitor.counts.get('PRICE_DROP', 0)}")
    print(f"NEW_HISTORICAL_LOW: {monitor.counts.get('NEW_HISTORICAL_LOW', 0)}")
    print(f"TARGET_PRICE: {monitor.counts.get('TARGET_PRICE', 0)}")
    print(f"CROSS_STORE_SAVING: {monitor.counts.get('CROSS_STORE_SAVING', 0)}")
    print(f"Новых событий: {monitor.total_new}")

    with open_db(DEFAULT_DB_PATH) as conn:
        init_db(conn)
        print(f"alert_events total: {count_alert_events(conn)}")
        print(
            "delivery stats:",
            get_delivery_stats(conn, channel=config.CHANNEL_TELEGRAM),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
