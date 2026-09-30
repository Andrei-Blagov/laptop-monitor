from __future__ import annotations

from typing import Sequence

from models import Product
from parsers.regard import fetch_target_laptops
from stores.base import StoreAdapter


class RegardStore(StoreAdapter):
    slug = "regard"
    display_name = "Regard"
    enabled = True
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        return list(fetch_target_laptops())
