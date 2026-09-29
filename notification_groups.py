from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
)
from comparison import normalize_sku
from product_identity import normalize_identifier


@dataclass
class NotificationGroup:
    """Один Telegram message, покрывающий 1+ alert_events."""

    group_key: str
    kind: str  # price_update | historical_low | cross_store | target_price | single
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def event_ids(self) -> list[int]:
        return [int(e["id"]) for e in self.events]


def _meta(event: Mapping[str, Any]) -> dict[str, Any]:
    meta = event.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def match_group_key_from_metadata(meta: Mapping[str, Any]) -> str | None:
    for field_name in ("matched_identifier", "normalized_sku", "sku"):
        value = meta.get(field_name)
        if value:
            key = normalize_identifier(str(value)) or normalize_sku(str(value))
            if key:
                return key
    return None


def build_product_group_keys(
    matches: Sequence[Any],
) -> dict[int, str]:
    """product_id -> stable matched-model key."""
    mapping: dict[int, str] = {}
    for match in matches:
        key = (
            normalize_identifier(getattr(match, "matched_identifier", None))
            or normalize_sku(getattr(match, "normalized_sku", None))
            or normalize_sku(getattr(match, "display_sku", None))
        )
        if not key:
            continue
        for offer in getattr(match, "offers", []) or []:
            pid = getattr(offer, "product_id", None)
            if pid is not None:
                mapping[int(pid)] = key
    return mapping


def event_group_key(
    event: Mapping[str, Any],
    product_keys: Mapping[int, str],
) -> str:
    """
    Ключ matched model для grouping.

    Приоритет: product_id → comparison match key (единый для пары магазинов),
    затем metadata identifiers, затем fallback.
    """
    product_id = event.get("product_id")
    if product_id is not None and int(product_id) in product_keys:
        return product_keys[int(product_id)]
    meta = _meta(event)
    from_meta = match_group_key_from_metadata(meta)
    if from_meta:
        return from_meta
    if product_id is not None:
        return f"product:{int(product_id)}"
    return f"event:{int(event['id'])}"


def group_alert_events_for_delivery(
    events: Sequence[Mapping[str, Any]],
    *,
    product_keys: Mapping[int, str] | None = None,
) -> list[NotificationGroup]:
    """
    Группирует unsent alert_events в Telegram messages.

    Правила:
    - PRICE_DROP(+CROSS_STORE той же модели) -> одно PRICE UPDATE
    - NEW_HISTORICAL_LOW той же модели, если есть PRICE_DROP для того же
      product_id — не отдельное сообщение
    - CROSS_STORE без PRICE_DROP модели — standalone
    - TARGET_PRICE — standalone (или секция в PRICE UPDATE той же модели)
    """
    product_keys = product_keys or {}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        key = event_group_key(event, product_keys)
        buckets.setdefault(key, []).append(dict(event))

    groups: list[NotificationGroup] = []
    for key, bucket in buckets.items():
        drops = [e for e in bucket if e.get("event_type") == EVENT_PRICE_DROP]
        crosses = [e for e in bucket if e.get("event_type") == EVENT_CROSS_STORE]
        hist = [e for e in bucket if e.get("event_type") == EVENT_HISTORICAL_LOW]
        targets = [e for e in bucket if e.get("event_type") == EVENT_TARGET_PRICE]
        other = [
            e
            for e in bucket
            if e.get("event_type")
            not in {
                EVENT_PRICE_DROP,
                EVENT_CROSS_STORE,
                EVENT_HISTORICAL_LOW,
                EVENT_TARGET_PRICE,
            }
        ]

        drop_product_ids = {
            int(e["product_id"])
            for e in drops
            if e.get("product_id") is not None
        }
        # HIST той же product_id, что и PRICE_DROP: поглощаем в группу
        # (информация уже в is_historical_low у DROP), но связываем delivery.
        hist_absorbed = [
            e
            for e in hist
            if e.get("product_id") is not None
            and int(e["product_id"]) in drop_product_ids
        ]
        hist_standalone = [
            e
            for e in hist
            if e.get("product_id") is None
            or int(e["product_id"]) not in drop_product_ids
        ]

        if drops:
            merged = list(drops)
            # CROSS_STORE той же модели включаем в PRICE UPDATE.
            merged.extend(crosses)
            # TARGET той же модели — явная секция в том же сообщении.
            merged.extend(targets)
            # Поглощённые HIST — association only (не отдельные сообщения).
            merged.extend(hist_absorbed)
            groups.append(
                NotificationGroup(
                    group_key=key,
                    kind="price_update",
                    events=merged,
                )
            )
        else:
            for event in crosses:
                groups.append(
                    NotificationGroup(
                        group_key=key,
                        kind="cross_store",
                        events=[event],
                    )
                )
            for event in targets:
                groups.append(
                    NotificationGroup(
                        group_key=key,
                        kind="target_price",
                        events=[event],
                    )
                )

        for event in hist_standalone:
            groups.append(
                NotificationGroup(
                    group_key=key,
                    kind="historical_low",
                    events=[event],
                )
            )
        for event in other:
            groups.append(
                NotificationGroup(
                    group_key=key,
                    kind="single",
                    events=[event],
                )
            )

    # Stable order by min event id in group.
    groups.sort(key=lambda g: min(g.event_ids) if g.event_ids else 0)
    return groups
