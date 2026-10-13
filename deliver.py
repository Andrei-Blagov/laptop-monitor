from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config
from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
    _specs_index,
)
from laptop_eligibility import product_is_eligible
from comparison import Offer, ProductMatch, match_products, comparable_offer_groups
from notification_groups import (
    NotificationGroup,
    build_product_group_keys,
    group_alert_events_for_delivery,
)
from notifications import format_notification_group, format_test_message
from storage import (
    DEFAULT_DB_PATH,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENT,
    DELIVERY_STATUS_SKIPPED,
    baseline_existing_alert_events,
    count_alert_events,
    count_unsent_alert_events,
    create_delivery_for_events,
    find_retryable_delivery_for_events,
    get_all_identifiers,
    get_all_products,
    get_delivery_stats,
    get_delivery_event_ids,
    get_undelivered_alert_events,
    init_db,
    link_delivery_events,
    open_db,
    update_delivery,
)
from telegram_sender import FakeTelegramSender, MessageSender, TelegramSender


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_destination(chat_id: str | None = None) -> str:
    return config.TELEGRAM_DESTINATION


def _build_groups(
    events: list[dict[str, Any]],
    db_path: Path | str,
) -> list[NotificationGroup]:
    products = get_all_products(db_path)
    identifiers = get_all_identifiers(db_path)
    comparison = match_products(products, identifiers)
    product_keys = build_product_group_keys(comparison.matches)
    return group_alert_events_for_delivery(events, product_keys=product_keys)


def _send_with_retries(
    transport: MessageSender,
    text: str,
    *,
    attempts_so_far: int,
    max_attempts: int,
    sleep: Callable[[float], None],
    before_attempt: Callable[[], bool] | None = None,
) -> tuple[Any, int]:
    """
    Отправляет сообщение, обрабатывая 429 через retry_after.

    Каждая попытка (включая 429) увеличивает attempts.
    Returns: (SendResult, final_attempts)
    """
    attempts = attempts_so_far
    last_result = None
    while attempts < max_attempts:
        if before_attempt is not None and not before_attempt():
            return None, attempts
        attempts += 1
        result = transport.send_message(text)
        last_result = result
        if result.ok:
            return result, attempts
        if (
            result.status_code == 429
            and result.retry_after is not None
            and attempts < max_attempts
        ):
            sleep(float(result.retry_after))
            continue
        # Non-429 failure or no retry_after: stop; caller marks failed.
        return result, attempts
    assert last_result is not None
    return last_result, attempts


def _event_rejection_reason(
    event: dict, products: dict[int, dict], specs: dict, identifiers: list[dict],
) -> str | None:
    """Check current evidence, not last_checked_at equality: enrichment may be price-neutral."""
    if event.get("event_type") not in {
        EVENT_CROSS_STORE, EVENT_HISTORICAL_LOW, EVENT_PRICE_DROP, EVENT_TARGET_PRICE,
    }:
        return None
    now = datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(str(event.get("created_at")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() > config.ALERT_EVENT_MAX_AGE_HOURS * 3600:
            return "expired_event"
    except (TypeError, ValueError):
        return "invalid_event_time"
    product = products.get(event.get("product_id"))
    if product is None:
        return "missing_product"
    if product.get("price") != event.get("new_price"):
        return "stale_price"

    def current_reason(row: dict) -> str | None:
        if not row.get("available"):
            return "unavailable_offer"
        try:
            checked = datetime.fromisoformat(str(row.get("last_checked_at")).replace("Z", "+00:00"))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            if (now - checked).total_seconds() > config.STORE_FRESHNESS_MAX_MINUTES * 60:
                return "stale_snapshot"
        except (TypeError, ValueError):
            return "invalid_snapshot_time"
        identity = specs.get((str(row["store"]), str(row["external_id"])))
        if not product_is_eligible(row, identity):
            return "eligibility"
        return None

    reason = current_reason(product)
    if reason:
        return reason
    if event["event_type"] != EVENT_CROSS_STORE:
        return None

    meta = event.get("metadata") or {}
    snapshots = meta.get("offers") or []
    pair = []
    for side in ("cheapest", "other"):
        store, price = meta.get(f"{side}_store"), meta.get(f"{side}_price")
        external_id = meta.get(f"{side}_external_id")
        url = meta.get(f"{side}_url")
        # Old events have offers[] or URL but no side-specific external_id.
        if external_id is None:
            saved = [o for o in snapshots if o.get("store") == store and o.get("url") == url]
            if len(saved) == 1:
                external_id = saved[0].get("external_id")
        candidates = [row for row in products.values() if row["store"] == store and (
            str(row["external_id"]) == str(external_id) if external_id is not None
            else bool(url) and row.get("url") == url
        )]
        if len(candidates) != 1:
            return "missing_or_ambiguous_compared_offer"
        row = candidates[0]
        if row.get("price") != price or (url and row.get("url") != url):
            return "stale_compared_offer"
        reason = current_reason(row)
        if reason:
            return reason
        pair.append(row)
    if pair[0]["id"] != product["id"] or pair[0]["store"] == pair[1]["store"]:
        return "invalid_comparison"
    if meta.get("cheapest_price") != event.get("new_price") or (
        event.get("old_price") is not None and meta.get("other_price") != event["old_price"]
    ):
        return "invalid_comparison_prices"
    difference = pair[1]["price"] - pair[0]["price"]
    if difference < config.CROSS_STORE_DIFFERENCE_RUB or difference != meta.get("difference"):
        return "invalid_comparison_saving"
    # All offers displayed in this event must still carry the saved price.
    checked_offers = {row["id"]: row for row in pair}
    for snapshot in snapshots:
        rows = [row for row in products.values() if row["store"] == snapshot.get("store")
                and str(row["external_id"]) == str(snapshot.get("external_id"))]
        if len(rows) != 1 or rows[0].get("price") != snapshot.get("price"):
            return "stale_compared_offer"
        reason = current_reason(rows[0])
        if reason:
            return reason
        checked_offers[rows[0]["id"]] = rows[0]
    comparison = match_products(list(checked_offers.values()), identifiers, specs_by_key=specs)
    # Verify the current identifier link as well as all six configuration fields.
    offers = [Offer(store=r["store"], external_id=r["external_id"], name=r["name"],
                    sku=r.get("sku"), price=r["price"], available=r["available"],
                    url=r.get("url"), product_id=r["id"]) for r in checked_offers.values()]
    match = ProductMatch("", "", "", offers=offers)
    if not any(len(group) == len(offers) for group in comparable_offer_groups(match, specs)):
        return "eligibility"
    # Configuration equality alone does not invent a new product-identity link.
    if not any({o.product_id for o in m.offers} >= set(checked_offers) for m in comparison.matches):
        return "comparison_identity_changed"
    return None


def deliver_alert_events(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    sender: MessageSender | None = None,
    destination: str | None = None,
    dry_run: bool = False,
    max_attempts: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    specs_path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Доставляет undelivered alert_events в Telegram с grouping.

    dry_run: только печатает тексты, DB не меняет.
    """
    max_attempts = (
        int(config.MAX_DELIVERY_ATTEMPTS)
        if max_attempts is None
        else int(max_attempts)
    )
    dest = destination if destination is not None else _resolve_destination()
    owns_sender = False

    if dry_run:
        transport: MessageSender = FakeTelegramSender()
    elif sender is not None:
        transport = sender
    else:
        token, chat_id = config.get_telegram_credentials()
        transport = TelegramSender(token, chat_id)
        owns_sender = True

    stats = {
        "unsent_alert_events": 0,
        "grouped_messages": 0,
        "sent": 0,
        "failed": 0,
        "skipped_limit": 0,
        "dry_run": 0,
        "excluded_eligibility": 0,
        "excluded_stale": 0,
    }
    messages: list[dict[str, Any]] = []

    try:
        with open_db(db_path) as conn:
            init_db(conn)
            events = get_undelivered_alert_events(
                conn,
                channel=config.CHANNEL_TELEGRAM,
                destination=dest,
                max_attempts=max_attempts,
            )
            stats["unsent_alert_events"] = len(events)
            # A missing spec may be enriched within a bounded wait. Stale events
            # become terminal skipped deliveries, never deleted or retried forever.
            excluded_ids: set[int] = set()

            def revalidate(candidates: list[dict]) -> list[dict]:
                products = {int(row["id"]): dict(row) for row in conn.execute("SELECT * FROM products")}
                specs = _specs_index(specs_path)
                identifiers = get_all_identifiers(db_path)
                eligible = []
                for event in candidates:
                    reason = _event_rejection_reason(event, products, specs, identifiers)
                    if reason is None:
                        eligible.append(event)
                        continue
                    event_id = int(event["id"])
                    if event_id in excluded_ids:
                        continue
                    excluded_ids.add(event_id)
                    if reason == "eligibility":
                        stats["excluded_eligibility"] += 1
                        continue
                    stats["excluded_stale"] += 1
                    if not dry_run:
                        skipped = create_delivery_for_events(
                            conn, alert_event_ids=[event_id], channel=config.CHANNEL_TELEGRAM,
                            destination=dest, status=DELIVERY_STATUS_SKIPPED,
                        )
                        update_delivery(conn, int(skipped["id"]), last_error=reason)
                if not dry_run:
                    conn.commit()
                return eligible

            events = revalidate(events)
            groups = _build_groups(events, db_path)
            stats["grouped_messages"] = len(groups)

            for group in groups:
                text = format_notification_group(group)
                messages.append(
                    {
                        "alert_event_ids": group.event_ids,
                        "group_key": group.group_key,
                        "kind": group.kind,
                        "event_types": [e.get("event_type") for e in group.events],
                        "text": text,
                    }
                )

                if dry_run:
                    stats["dry_run"] += 1
                    continue

                delivery = find_retryable_delivery_for_events(
                    conn,
                    alert_event_ids=group.event_ids,
                    channel=config.CHANNEL_TELEGRAM,
                    destination=dest,
                    max_attempts=max_attempts,
                )
                previous_attempts = int(delivery["attempts"]) if delivery else 0
                if delivery is not None and set(get_delivery_event_ids(conn, int(delivery["id"]))) != set(group.event_ids):
                    delivery = None
                if delivery is None:
                    delivery = create_delivery_for_events(
                        conn,
                        alert_event_ids=group.event_ids,
                        channel=config.CHANNEL_TELEGRAM,
                        destination=dest,
                        status=DELIVERY_STATUS_PENDING,
                    )
                else:
                    # Ensure all group events are linked (retry of partial group).
                    link_delivery_events(
                        conn, int(delivery["id"]), group.event_ids
                    )

                if delivery["status"] in {
                    DELIVERY_STATUS_SENT,
                    DELIVERY_STATUS_SKIPPED,
                }:
                    continue
                if int(delivery["attempts"]) >= max_attempts:
                    stats["skipped_limit"] += 1
                    continue

                if previous_attempts and int(delivery["attempts"]) < previous_attempts:
                    delivery = update_delivery(conn, int(delivery["id"]), attempts=previous_attempts)
                conn.commit()
                result, attempts = _send_with_retries(
                    transport,
                    text,
                    attempts_so_far=int(delivery["attempts"]),
                    max_attempts=max_attempts,
                    sleep=sleep,
                    before_attempt=lambda: len(revalidate(group.events)) == len(group.events),
                )
                if result is None:
                    # Surviving events will be regrouped on the next invocation.
                    update_delivery(conn, int(delivery["id"]), attempts=attempts)
                    continue
                now = _iso_now()

                if result.ok:
                    update_delivery(
                        conn,
                        int(delivery["id"]),
                        status=DELIVERY_STATUS_SENT,
                        attempts=attempts,
                        last_attempt_at=now,
                        sent_at=now,
                        last_error=None,
                        provider_message_id=result.message_id,
                    )
                    stats["sent"] += 1
                else:
                    update_delivery(
                        conn,
                        int(delivery["id"]),
                        status=DELIVERY_STATUS_FAILED,
                        attempts=attempts,
                        last_attempt_at=now,
                        last_error=(result.error or "unknown error")[:1000],
                    )
                    stats["failed"] += 1
                conn.commit()
    finally:
        if owns_sender and isinstance(transport, TelegramSender):
            transport.close()

    return {"stats": stats, "messages": messages, "destination": dest}


def run_baseline(db_path: Path | str = DEFAULT_DB_PATH) -> int:
    dest = _resolve_destination()
    with open_db(db_path) as conn:
        init_db(conn)
        return baseline_existing_alert_events(
            conn,
            channel=config.CHANNEL_TELEGRAM,
            destination=dest,
        )


def run_status(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, int]:
    with open_db(db_path) as conn:
        init_db(conn)
        delivery = get_delivery_stats(conn, channel=config.CHANNEL_TELEGRAM)
        unsent = count_unsent_alert_events(
            conn,
            channel=config.CHANNEL_TELEGRAM,
            destination=config.TELEGRAM_DESTINATION,
            max_attempts=int(config.MAX_DELIVERY_ATTEMPTS),
        )
        return {
            **delivery,
            "unsent_alert_events": unsent,
            "alert_events_total": count_alert_events(conn),
        }


def run_test_message(sender: MessageSender | None = None) -> None:
    if sender is None:
        token, chat_id = config.get_telegram_credentials()
        with TelegramSender(token, chat_id) as client:
            result = client.send_message(format_test_message())
    else:
        result = sender.send_message(format_test_message())
    if not result.ok:
        raise RuntimeError(result.error or "Telegram test send failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Доставка alert_events в Telegram"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать сообщения без отправки и без изменения delivery",
    )
    parser.add_argument(
        "--baseline-existing",
        action="store_true",
        help="Пометить существующие alert_events как skipped (без отправки)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Показать статистику delivery",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Отправить одно тестовое сообщение (без alert_event)",
    )
    args = parser.parse_args(argv)

    if args.status:
        stats = run_status(DEFAULT_DB_PATH)
        print("Telegram delivery:")
        print(f"  unsent alert events: {stats.get('unsent_alert_events', 0)}")
        print(f"  pending deliveries: {stats.get(DELIVERY_STATUS_PENDING, 0)}")
        print(f"  sent deliveries: {stats.get(DELIVERY_STATUS_SENT, 0)}")
        print(f"  failed deliveries: {stats.get(DELIVERY_STATUS_FAILED, 0)}")
        print(f"  skipped deliveries: {stats.get(DELIVERY_STATUS_SKIPPED, 0)}")
        print(f"  alert_events total: {stats.get('alert_events_total', 0)}")
        return 0

    if args.baseline_existing:
        created = run_baseline(DEFAULT_DB_PATH)
        print(f"Baseline: помечено skipped = {created}")
        stats = run_status(DEFAULT_DB_PATH)
        print("Telegram delivery:")
        print(f"  unsent alert events: {stats.get('unsent_alert_events', 0)}")
        print(f"  pending deliveries: {stats.get(DELIVERY_STATUS_PENDING, 0)}")
        print(f"  sent deliveries: {stats.get(DELIVERY_STATUS_SENT, 0)}")
        print(f"  failed deliveries: {stats.get(DELIVERY_STATUS_FAILED, 0)}")
        print(f"  skipped deliveries: {stats.get(DELIVERY_STATUS_SKIPPED, 0)}")
        print(f"  alert_events total: {stats.get('alert_events_total', 0)}")
        return 0

    if args.test:
        try:
            run_test_message()
        except ValueError as exc:
            print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(f"Ошибка отправки: {exc}", file=sys.stderr)
            return 1
        print("Тестовое сообщение отправлено.")
        return 0

    if args.dry_run:
        result = deliver_alert_events(DEFAULT_DB_PATH, dry_run=True)
        print(f"unsent alert events: {result['stats']['unsent_alert_events']}")
        print(
            "Telegram messages after grouping: "
            f"{result['stats']['grouped_messages']}"
        )
        print()
        for item in result["messages"]:
            print("=" * 40)
            print(
                f"group={item['group_key']} kind={item['kind']} "
                f"events={item['alert_event_ids']} types={item['event_types']}"
            )
            print(item["text"])
            print()
        return 0

    try:
        result = deliver_alert_events(DEFAULT_DB_PATH, dry_run=False)
    except ValueError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    stats = result["stats"]
    print("Telegram delivery run:")
    print(f"  unsent alert events: {stats['unsent_alert_events']}")
    print(f"  grouped messages: {stats['grouped_messages']}")
    print(f"  sent: {stats['sent']}")
    print(f"  failed: {stats['failed']}")
    print(f"  skipped_limit: {stats['skipped_limit']}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
