from __future__ import annotations

from typing import Sequence

from models import Product
from stores.base import StoreAdapter
from stores.common import MONITOR_REGION_MOSCOW


class XcomStore(StoreAdapter):
    """
    XCOM-SHOP — disabled.

    Discovery: DDoS-Guard / captcha interstitial on catalog and search
    (HTTP and plain Chromium). No stable public product/price HTML without bypass.
    """

    slug = "xcom"
    display_name = "XCOM"
    enabled = False
    region = MONITOR_REGION_MOSCOW
    collection_mode = "http"
    reliability = "experimental"
    price_semantics = "public"
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        raise RuntimeError(
            "XCOM adapter disabled: DDoS-Guard/captcha on catalog (Moscow site)."
        )
