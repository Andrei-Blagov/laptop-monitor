from __future__ import annotations

from stores.andpro import AndproStore
from stores.base import StoreAdapter
from stores.citilink import CitilinkStore
from stores.dns import DnsStore
from stores.regard import RegardStore

_REGISTERED: list[StoreAdapter] = [
    RegardStore(),
    AndproStore(),
    DnsStore(),
    CitilinkStore(),
]


def all_adapters() -> list[StoreAdapter]:
    return list(_REGISTERED)


def enabled_adapters() -> list[StoreAdapter]:
    return [a for a in _REGISTERED if a.enabled]


def get_adapter(slug: str) -> StoreAdapter | None:
    for adapter in _REGISTERED:
        if adapter.slug == slug:
            return adapter
    return None


def enabled_slugs() -> list[str]:
    return [a.slug for a in enabled_adapters()]
