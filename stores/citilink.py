from __future__ import annotations

from typing import Sequence

from models import Product
from stores.base import StoreAdapter


class CitilinkStore(StoreAdapter):
    """
    Citilink — disabled.

    Live probe (2026-09): catalog/search return HTTP 429 with JS challenge
    page («Подождите...»). No stable public JSON without browser challenge.
    """

    slug = "citilink"
    display_name = "Citilink"
    enabled = False
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        raise RuntimeError(
            "Citilink adapter disabled: HTTP 429 JS challenge on catalog. "
            "Enable only after a documented stable public API is available."
        )
