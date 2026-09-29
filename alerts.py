from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import config
from comparison import match_products
from identity_sync import identity_from_cache, load_specs_cache
from product_identity import ProductIdentity
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers,
    get_all_products,
    get_historical_min_before_current,
    get_previous_price,
    init_db,
    open_db,
    save_alert_event,
)
from target_gpu import get_target_gpu


EVENT_PRICE_DROP = "PRICE_DROP"
EVENT_HISTORICAL_LOW = "NEW_HISTORICAL_LOW"
EVENT_TARGET_PRICE = "TARGET_PRICE"
EVENT_CROSS_STORE = "CROSS_STORE_SAVING"


@dataclass
class MonitorResult:
    created: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_new(self) -> int:
        return len(self.created)


@dataclass
class CurrentDeals:
    target_price: list[dict] = field(default_factory=list)
    cross_store: list[dict] = field(default_factory=list)
    cheapest_by_gpu: dict[str, dict | None] = field(default_factory=dict)


def price_drop_percent(old_price: int | None, new_price: int | None) -> float | None:
    if old_price is None or new_price is None:
        return None
    if old_price <= 0:
        return None
    if new_price >= old_price:
        return None
    return (old_price - new_price) / old_price * 100.0


def is_significant_historical_low(
    previous_min: int,
    new_price: int,
    *,
    min_percent: float | None = None,
    min_absolute: int | None = None,
) -> bool:
    """True если new_price ниже previous_min на >= percent ИЛИ >= absolute руб."""
    if new_price >= previous_min or previous_min <= 0:
        return False
    drop = previous_min - new_price
    pct = drop / previous_min * 100.0
    min_percent = (
        float(config.HISTORICAL_LOW_MIN_PERCENT)
        if min_percent is None
        else float(min_percent)
    )
    min_absolute = (
        int(config.HISTORICAL_LOW_MIN_ABSOLUTE)
        if min_absolute is None
        else int(min_absolute)
    )
    return pct >= min_percent or drop >= min_absolute


def _specs_index(
    specs_path: Path | str | None = None,
) -> dict[tuple[str, str], ProductIdentity]:
    cache = load_specs_cache(specs_path) if specs_path else load_specs_cache()
    result: dict[tuple[str, str], ProductIdentity] = {}
    for key, raw in cache.items():
        if not isinstance(raw, dict):
            continue
        store = str(raw.get("store") or "")
        external_id = str(raw.get("external_id") or "")
        if not store or not external_id:
            if isinstance(key, str) and ":" in key:
                store, external_id = key.split(":", 1)
            else:
                continue
        identity = identity_from_cache(cache, store, external_id)
        if identity is not None:
            result[(store, external_id)] = identity
    return result


def _product_gpu(
    product: Mapping[str, Any],
    specs: Mapping[tuple[str, str], ProductIdentity],
) -> str | None:
    key = (str(product["store"]), str(product["external_id"]))
    identity = specs.get(key)
    if identity is not None:
        label = get_target_gpu(identity)
        if label:
            return label
    return get_target_gpu(product)


def price_drop_applies(
    conn,
    product: Mapping[str, Any],
) -> bool:
    """True если текущее изменение цены квалифицируется как PRICE_DROP."""
    new_price = product.get("price")
    old_price = get_previous_price(conn, int(product["id"]))
    if new_price is None or old_price is None:
        return False
    percent = price_drop_percent(int(old_price), int(new_price))
    return percent is not None and percent >= float(config.PRICE_DROP_PERCENT)


def evaluate_price_drop(
    conn,
    product: Mapping[str, Any],
) -> dict | None:
    product_id = int(product["id"])
    new_price = product.get("price")
    old_price = get_previous_price(conn, product_id)
    if new_price is None or old_price is None:
        return None
    new_price_i = int(new_price)
    old_price_i = int(old_price)
    percent = price_drop_percent(old_price_i, new_price_i)
    if percent is None or percent < float(config.PRICE_DROP_PERCENT):
        return None

    hist_min = get_historical_min_before_current(conn, product_id)
    is_historical_low = (
        hist_min is not None and new_price_i < int(hist_min)
    )

    dedupe_key = (
        f"{EVENT_PRICE_DROP}|{product_id}|{old_price_i}|{new_price_i}"
    )
    metadata: dict[str, Any] = {
        "store": product["store"],
        "sku": product.get("sku"),
        "name": product.get("name"),
        "url": product.get("url"),
        "drop_percent": round(percent, 2),
        "drop_amount": old_price_i - new_price_i,
        "threshold_percent": float(config.PRICE_DROP_PERCENT),
        "is_historical_low": is_historical_low,
    }
    if is_historical_low:
        metadata["previous_historical_low"] = int(hist_min)
        metadata["previous_historical_min"] = int(hist_min)

    return save_alert_event(
        conn,
        event_type=EVENT_PRICE_DROP,
        product_id=product_id,
        dedupe_key=dedupe_key,
        old_price=old_price_i,
        new_price=new_price_i,
        metadata=metadata,
    )


def evaluate_historical_low(
    conn,
    product: Mapping[str, Any],
) -> dict | None:
    product_id = int(product["id"])
    new_price = product.get("price")
    if new_price is None:
        return None
    new_price = int(new_price)
    hist_min = get_historical_min_before_current(conn, product_id)
    if hist_min is None:
        return None
    if not is_significant_historical_low(int(hist_min), new_price):
        return None
    drop = int(hist_min) - new_price
    drop_percent = round(drop / int(hist_min) * 100.0, 2)
    dedupe_key = (
        f"{EVENT_HISTORICAL_LOW}|{product_id}|{hist_min}|{new_price}"
    )
    return save_alert_event(
        conn,
        event_type=EVENT_HISTORICAL_LOW,
        product_id=product_id,
        dedupe_key=dedupe_key,
        old_price=hist_min,
        new_price=new_price,
        metadata={
            "store": product["store"],
            "sku": product.get("sku"),
            "name": product.get("name"),
            "url": product.get("url"),
            "previous_historical_min": hist_min,
            "drop_amount": drop,
            "drop_percent": drop_percent,
        },
    )


def evaluate_target_price(
    conn,
    product: Mapping[str, Any],
    gpu: str | None,
) -> dict | None:
    if not product.get("available"):
        return None
    price = product.get("price")
    if price is None or gpu is None:
        return None
    threshold = config.TARGET_PRICES.get(gpu)
    if threshold is None:
        return None
    price = int(price)
    if price > threshold:
        return None

    product_id = int(product["id"])
    previous = get_previous_price(conn, product_id)
    # Staying below threshold: previous also <= threshold → no new event.
    if previous is not None and previous <= threshold:
        return None

    # Crossing (previous > threshold) or first observation (previous is None).
    prev_token = "none" if previous is None else str(int(previous))
    dedupe_key = (
        f"{EVENT_TARGET_PRICE}|{product_id}|{threshold}|from:{prev_token}|to:{price}"
    )
    return save_alert_event(
        conn,
        event_type=EVENT_TARGET_PRICE,
        product_id=product_id,
        dedupe_key=dedupe_key,
        old_price=previous,
        new_price=price,
        metadata={
            "store": product["store"],
            "sku": product.get("sku"),
            "name": product.get("name"),
            "url": product.get("url"),
            "gpu": gpu,
            "threshold": threshold,
            "available": True,
        },
    )


def evaluate_cross_store(
    conn,
    matches: Sequence[Any],
) -> list[dict]:
    created: list[dict] = []
    threshold = int(config.CROSS_STORE_DIFFERENCE_RUB)

    for match in matches:
        available = [
            o
            for o in match.offers
            if o.available and o.price is not None
        ]
        if len(available) < 2:
            continue
        ordered = sorted(available, key=lambda o: (o.price or 0, o.store))
        cheapest = ordered[0]
        other = ordered[1]
        difference = int(other.price) - int(cheapest.price)
        if difference < threshold:
            continue

        identity = (
            match.matched_identifier
            or match.normalized_sku
            or match.display_sku
        )
        price_parts = sorted(
            f"{o.store}:{int(o.price)}" for o in ordered
        )
        dedupe_key = (
            f"{EVENT_CROSS_STORE}|{identity}|{'|'.join(price_parts)}"
        )

        # Prefer product_id of cheapest offer when available.
        product_id = cheapest.product_id
        event = save_alert_event(
            conn,
            event_type=EVENT_CROSS_STORE,
            product_id=product_id,
            dedupe_key=dedupe_key,
            old_price=int(other.price),
            new_price=int(cheapest.price),
            metadata={
                "name": match.name,
                "sku": match.display_sku,
                "normalized_sku": match.normalized_sku,
                "match_method": match.match_method,
                "matched_identifier": match.matched_identifier,
                "cheapest_store": cheapest.store,
                "cheapest_price": int(cheapest.price),
                "cheapest_url": cheapest.url,
                "cheapest_sku": cheapest.sku,
                "other_store": other.store,
                "other_price": int(other.price),
                "other_url": other.url,
                "other_sku": other.sku,
                "difference": difference,
                "threshold": threshold,
                "offers": [
                    {
                        "store": o.store,
                        "sku": o.sku,
                        "price": o.price,
                        "available": o.available,
                        "url": o.url,
                        "external_id": o.external_id,
                    }
                    for o in match.offers
                ],
            },
        )
        if event is not None:
            created.append(event)
    return created


def run_monitor(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    specs_path: Path | str | None = None,
) -> MonitorResult:
    products = get_all_products(db_path)
    identifiers = get_all_identifiers(db_path)
    specs = _specs_index(specs_path)
    comparison = match_products(
        products,
        identifiers,
        specs_by_key=specs,
    )

    created: list[dict] = []
    with open_db(db_path) as conn:
        init_db(conn)
        for product in products:
            gpu = _product_gpu(product, specs)
            # PRICE_DROP имеет приоритет над NEW_HISTORICAL_LOW для одного изменения.
            # Даже если PRICE_DROP уже есть (dedupe → None), hist не создаём.
            drop_event = evaluate_price_drop(conn, product)
            if drop_event is not None:
                created.append(drop_event)
            elif not price_drop_applies(conn, product):
                hist_event = evaluate_historical_low(conn, product)
                if hist_event is not None:
                    created.append(hist_event)

            target_event = evaluate_target_price(conn, product, gpu)
            if target_event is not None:
                created.append(target_event)

        created.extend(evaluate_cross_store(conn, comparison.matches))

    counts = {
        EVENT_PRICE_DROP: 0,
        EVENT_HISTORICAL_LOW: 0,
        EVENT_TARGET_PRICE: 0,
        EVENT_CROSS_STORE: 0,
    }
    for event in created:
        counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1

    return MonitorResult(created=created, counts=counts)


def get_current_deals(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    specs_path: Path | str | None = None,
) -> CurrentDeals:
    """Текущее состояние рынка — не зависит от уже отправленных alerts."""
    products = get_all_products(db_path)
    identifiers = get_all_identifiers(db_path)
    specs = _specs_index(specs_path)
    comparison = match_products(
        products,
        identifiers,
        specs_by_key=specs,
    )

    target_deals: list[dict] = []
    cheapest_by_gpu: dict[str, dict | None] = {
        label: None for label in config.TARGET_PRICES
    }

    for product in products:
        if not product.get("available"):
            continue
        price = product.get("price")
        if price is None:
            continue
        price = int(price)
        gpu = _product_gpu(product, specs)
        if gpu is None:
            continue

        deal = {
            "store": product["store"],
            "external_id": product["external_id"],
            "sku": product.get("sku"),
            "name": product.get("name"),
            "price": price,
            "url": product.get("url"),
            "gpu": gpu,
            "threshold": config.TARGET_PRICES.get(gpu),
        }

        current_best = cheapest_by_gpu.get(gpu)
        if current_best is None or price < int(current_best["price"]):
            cheapest_by_gpu[gpu] = deal

        threshold = config.TARGET_PRICES.get(gpu)
        if threshold is not None and price <= threshold:
            target_deals.append(deal)

    target_deals.sort(key=lambda d: (d["gpu"], d["price"], d["store"]))

    cross_deals: list[dict] = []
    threshold_diff = int(config.CROSS_STORE_DIFFERENCE_RUB)
    for match in comparison.matches:
        available = [
            o for o in match.offers if o.available and o.price is not None
        ]
        if len(available) < 2:
            continue
        ordered = sorted(available, key=lambda o: (o.price or 0, o.store))
        cheapest = ordered[0]
        other = ordered[1]
        difference = int(other.price) - int(cheapest.price)
        if difference < threshold_diff:
            continue
        cross_deals.append(
            {
                "name": match.name,
                "sku": match.display_sku,
                "match_method": match.match_method,
                "matched_identifier": match.matched_identifier,
                "cheapest_store": cheapest.store,
                "cheapest_price": int(cheapest.price),
                "cheapest_url": cheapest.url,
                "other_store": other.store,
                "other_price": int(other.price),
                "other_url": other.url,
                "difference": difference,
            }
        )
    cross_deals.sort(key=lambda d: -d["difference"])

    return CurrentDeals(
        target_price=target_deals,
        cross_store=cross_deals,
        cheapest_by_gpu=cheapest_by_gpu,
    )
