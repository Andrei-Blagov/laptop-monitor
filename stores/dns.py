from __future__ import annotations

from typing import Sequence

from models import Product
from stores.base import StoreAdapter


class DnsStore(StoreAdapter):
    """
    DNS Shop — disabled.

    Live probe (2026-09): catalog/search return HTTP 401 challenge HTML;
    api.dns-shop.ru returns 403. Reliable collection without anti-bot bypass
    is not available for VPS polling.
    """

    slug = "dns"
    display_name = "DNS"
    enabled = False
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        raise RuntimeError(
            "DNS adapter disabled: store returns 401 challenge / API 403 "
            "(anti-bot). Enable only after a documented stable public API "
            "or partner feed is available."
        )
