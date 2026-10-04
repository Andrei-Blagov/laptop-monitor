from __future__ import annotations

"""Thailand store registry (separate from Russian stores.registry)."""

from dataclasses import dataclass
from typing import Callable

from thailand.models import StoreScanResult


@dataclass(frozen=True)
class ThailandStoreAdapter:
    slug: str
    display_name: str
    enabled: bool
    collect: Callable[..., StoreScanResult]


def get_thailand_adapters() -> list[ThailandStoreAdapter]:
    # Local imports avoid circular deps at package import time.
    import config
    from thailand import advice, banana, jib, lazada

    return [
        ThailandStoreAdapter(
            slug="jib",
            display_name="JIB",
            enabled=True,
            collect=jib.collect,
        ),
        ThailandStoreAdapter(
            slug="advice",
            display_name="Advice",
            enabled=True,
            collect=advice.collect,
        ),
        ThailandStoreAdapter(
            slug="banana",
            display_name="BaNANA",
            enabled=True,
            collect=banana.collect,
        ),
        ThailandStoreAdapter(
            slug="lazada",
            display_name="Lazada",
            enabled=bool(config.THAILAND_LAZADA_ENABLED),
            collect=lazada.collect,
        ),
    ]
