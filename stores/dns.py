from __future__ import annotations

from typing import Sequence

from models import Product
from stores.base import StoreAdapter
from stores.common import MONITOR_REGION_MOSCOW


class DnsStore(StoreAdapter):
    """
    DNS Shop — disabled.

    Deep discovery (HTTP + plain Playwright Chromium, 2026-09):
    - www.dns-shop.ru catalog/search → HTTP 401 / browser title HTTP 403
    - api.dns-shop.ru → HTTP 403
    - robots.txt reachable, but product HTML/API not available without anti-bot bypass

    Enable only with a documented stable public/partner feed for Moscow.
    """

    slug = "dns"
    display_name = "DNS"
    enabled = False
    region = MONITOR_REGION_MOSCOW
    collection_mode = "browser"
    reliability = "experimental"
    price_semantics = "public"
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        raise RuntimeError(
            "DNS adapter disabled: HTTP 401 / browser 403 / API 403 "
            "(anti-bot). No stable public Moscow catalog without bypass."
        )
