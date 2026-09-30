from __future__ import annotations

from typing import Sequence

from models import Product
from parsers.kns import fetch_target_laptops
from stores.base import StoreAdapter
from stores.common import MONITOR_REGION_MOSCOW


class KnsStore(StoreAdapter):
    slug = "kns"
    display_name = "KNS"
    enabled = True
    region = MONITOR_REGION_MOSCOW
    collection_mode = "http"
    reliability = "stable"
    price_semantics = "public"
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        return list(fetch_target_laptops())
