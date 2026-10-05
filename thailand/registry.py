from __future__ import annotations

"""Thailand store registry (separate from Russian stores.registry)."""

from dataclasses import dataclass
from typing import Callable

import config
from thailand.models import StoreScanResult


@dataclass(frozen=True)
class ThailandStoreAdapter:
    slug: str
    display_name: str
    enabled: bool
    collect: Callable[..., StoreScanResult]


def get_thailand_adapters() -> list[ThailandStoreAdapter]:
    # Local imports avoid circular deps at package import time.
    from thailand import advice, banana, invadeit, itcity, jib, lazada, speedcom

    return [
        ThailandStoreAdapter(
            slug="jib",
            display_name="JIB",
            enabled=bool(config.THAILAND_STORE_JIB_ENABLED),
            collect=jib.collect,
        ),
        ThailandStoreAdapter(
            slug="advice",
            display_name="Advice",
            enabled=bool(config.THAILAND_STORE_ADVICE_ENABLED),
            collect=advice.collect,
        ),
        ThailandStoreAdapter(
            slug="speedcom",
            display_name="SpeedCom",
            enabled=bool(config.THAILAND_STORE_SPEEDCOM_ENABLED),
            collect=speedcom.collect,
        ),
        ThailandStoreAdapter(
            slug="invadeit",
            display_name="InvadeIT",
            enabled=bool(config.THAILAND_STORE_INVADEIT_ENABLED),
            collect=invadeit.collect,
        ),
        ThailandStoreAdapter(
            slug="itcity",
            display_name="IT City",
            enabled=bool(config.THAILAND_STORE_ITCITY_ENABLED),
            collect=itcity.collect,
        ),
        ThailandStoreAdapter(
            slug="banana",
            display_name="BaNANA",
            enabled=bool(config.THAILAND_STORE_BANANA_ENABLED),
            collect=banana.collect,
        ),
        ThailandStoreAdapter(
            slug="lazada",
            display_name="Lazada",
            enabled=bool(config.THAILAND_STORE_LAZADA_ENABLED),
            collect=lazada.collect,
        ),
    ]
