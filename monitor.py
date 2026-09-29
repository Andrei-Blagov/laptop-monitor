from __future__ import annotations

import argparse
import sys

import config
from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
    get_current_deals,
    run_monitor,
)
from storage import DEFAULT_DB_PATH, count_alert_events, init_db, open_db


def _fmt_price(price: int | None) -> str:
    if price is None:
        return "н/д"
    return f"{price:,}".replace(",", " ") + " ₽"


def _store_label(store: str) -> str:
    return store.upper() if store == "andpro" else store.capitalize()


def _print_event(event: dict) -> None:
    meta = event.get("metadata") or {}
    etype = event["event_type"]

    if etype == EVENT_TARGET_PRICE:
        print("[TARGET PRICE]")
        print(meta.get("name") or "?")
        print(meta.get("gpu") or "?")
        print(_store_label(str(meta.get("store") or "?")))
        print(_fmt_price(event.get("new_price")))
        print(f"Порог: {_fmt_price(meta.get('threshold'))}")
        if meta.get("url"):
            print(meta["url"])
        print()
        return

    if etype == EVENT_CROSS_STORE:
        print("[CROSS STORE]")
        print(meta.get("name") or meta.get("sku") or "?")
        print(
            f"{_store_label(str(meta.get('cheapest_store')))}: "
            f"{_fmt_price(meta.get('cheapest_price'))}"
        )
        print(
            f"{_store_label(str(meta.get('other_store')))}: "
            f"{_fmt_price(meta.get('other_price'))}"
        )
        print(f"Экономия: {_fmt_price(meta.get('difference'))}")
        print()
        return

    if etype == EVENT_PRICE_DROP:
        print("[PRICE DROP]")
        print(meta.get("name") or "?")
        print(_store_label(str(meta.get("store") or "?")))
        print(
            f"{_fmt_price(event.get('old_price'))} → "
            f"{_fmt_price(event.get('new_price'))}"
            f" (−{meta.get('drop_percent')}%)"
        )
        if meta.get("url"):
            print(meta["url"])
        print()
        return

    if etype == EVENT_HISTORICAL_LOW:
        print("[NEW HISTORICAL LOW]")
        print(meta.get("name") or "?")
        print(_store_label(str(meta.get("store") or "?")))
        print(_fmt_price(event.get("new_price")))
        print(f"Бывший минимум: {_fmt_price(event.get('old_price'))}")
        if meta.get("url"):
            print(meta["url"])
        print()
        return

    print(f"[{etype}]")
    print(meta)
    print()


def _print_deals() -> None:
    deals = get_current_deals(DEFAULT_DB_PATH)
    print("CURRENT DEALS")
    print()
    print("TARGET PRICE thresholds:")
    for gpu, threshold in config.TARGET_PRICES.items():
        print(f"  {gpu} <= {_fmt_price(threshold)}")
    print()

    print(f"TARGET PRICE deals: {len(deals.target_price)}")
    for deal in deals.target_price:
        print("-" * 40)
        print(deal["name"])
        print(deal["gpu"])
        print(_store_label(deal["store"]))
        print(_fmt_price(deal["price"]))
        print(f"Порог: {_fmt_price(deal['threshold'])}")
        if deal.get("url"):
            print(deal["url"])
    print()

    print(
        f"CROSS STORE deals (>= {_fmt_price(config.CROSS_STORE_DIFFERENCE_RUB)}): "
        f"{len(deals.cross_store)}"
    )
    for deal in deals.cross_store:
        print("-" * 40)
        print(deal["name"])
        print(
            f"{_store_label(deal['cheapest_store'])}: "
            f"{_fmt_price(deal['cheapest_price'])}"
        )
        print(
            f"{_store_label(deal['other_store'])}: "
            f"{_fmt_price(deal['other_price'])}"
        )
        print(f"Экономия: {_fmt_price(deal['difference'])}")
    print()

    print("Cheapest available by GPU:")
    for gpu in config.TARGET_PRICES:
        best = deals.cheapest_by_gpu.get(gpu)
        print("-" * 40)
        print(gpu)
        if best is None:
            print("  нет доступных предложений")
            continue
        print(f"  {_store_label(best['store'])}")
        print(f"  {best['name']}")
        print(f"  {_fmt_price(best['price'])}")
        if best.get("url"):
            print(f"  {best['url']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Price monitor / alert engine")
    parser.add_argument(
        "--deals",
        action="store_true",
        help="Показать текущие deals (не создавать alerts)",
    )
    args = parser.parse_args(argv)

    if args.deals:
        _print_deals()
        return 0

    print("PRICE MONITOR")
    print()
    result = run_monitor(DEFAULT_DB_PATH)
    print(f"{EVENT_PRICE_DROP}: {result.counts.get(EVENT_PRICE_DROP, 0)}")
    print(f"{EVENT_HISTORICAL_LOW}: {result.counts.get(EVENT_HISTORICAL_LOW, 0)}")
    print(f"{EVENT_TARGET_PRICE}: {result.counts.get(EVENT_TARGET_PRICE, 0)}")
    print(f"{EVENT_CROSS_STORE}: {result.counts.get(EVENT_CROSS_STORE, 0)}")
    print()
    print(f"Новых событий: {result.total_new}")
    print()

    for event in result.created:
        _print_event(event)

    with open_db(DEFAULT_DB_PATH) as conn:
        init_db(conn)
        total = count_alert_events(conn)
    print(f"Всего alert_events в DB: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
