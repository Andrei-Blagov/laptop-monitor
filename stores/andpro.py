from __future__ import annotations

from typing import Sequence

from models import Product
from parsers.andpro import fetch_target_laptops
from stores.base import StoreAdapter


class AndproStore(StoreAdapter):
    slug = "andpro"
    display_name = "ANDPRO"
    enabled = True
    min_expected_products = 5

    def collect(self) -> Sequence[Product]:
        return list(fetch_target_laptops())
