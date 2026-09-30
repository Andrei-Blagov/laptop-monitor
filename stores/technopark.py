from __future__ import annotations

from typing import Sequence

from models import Product
from stores.base import StoreAdapter
from stores.common import MONITOR_REGION_MOSCOW


class TechnoparkStore(StoreAdapter):
    """
    Technopark — disabled.

    Discovery: HTTP 401 and Playwright Chromium HTTP 403 on catalog/search.
    robots.txt reachable; product pages not available without anti-bot bypass.
    """

    slug = "technopark"
    display_name = "Technopark"
    enabled = False
    region = MONITOR_REGION_MOSCOW
    collection_mode = "browser"
    reliability = "experimental"
    price_semantics = "public"
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        raise RuntimeError(
            "Technopark adapter disabled: HTTP 401 / browser 403 on catalog."
        )
