from __future__ import annotations

from typing import Sequence

from models import Product
from parsers.citilink import fetch_target_laptops
from stores.base import StoreAdapter
from stores.common import MONITOR_REGION_MOSCOW


class CitilinkStore(StoreAdapter):
    """
    Citilink Moscow — experimental browser collector.

    Plain Chromium only (no stealth). Uses search + JSON-LD Offer.price.
    HTTP catalog alone returns 429 challenge pages.
    """

    slug = "citilink"
    display_name = "Citilink"
    enabled = True
    region = MONITOR_REGION_MOSCOW
    collection_mode = "browser"
    reliability = "experimental"
    price_semantics = "public"
    min_expected_products = 3

    def collect(self) -> Sequence[Product]:
        return list(fetch_target_laptops())
