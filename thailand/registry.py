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
    from thailand import advice, banana, jib

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
    ]
