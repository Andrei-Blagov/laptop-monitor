from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Sequence

from models import Product
from stores.common import MONITOR_REGION_MOSCOW

CollectionMode = Literal["http", "browser"]
Reliability = Literal["stable", "experimental"]
PriceSemantics = Literal["public"]


@dataclass
class StoreSanityResult:
    ok: bool
    reason: str | None = None


@dataclass
class StoreCollectOutcome:
    store: str
    ok: bool
    products: list[Product] = field(default_factory=list)
    error: str | None = None
    sanity: StoreSanityResult | None = None

    @property
    def count(self) -> int:
        return len(self.products)


class StoreAdapter(ABC):
    """Единый интерфейс магазина для collection / registry."""

    slug: str
    display_name: str
    enabled: bool = True
    region: str = MONITOR_REGION_MOSCOW
    collection_mode: CollectionMode = "http"
    reliability: Reliability = "stable"
    price_semantics: PriceSemantics = "public"
    # Sanity: минимум ожидаемых товаров (защита от сломанного HTML/API).
    min_expected_products: int = 1

    @abstractmethod
    def collect(self) -> Sequence[Product]:
        """Сырой сбор. Исключения → FAILED для этого store."""

    def validate_products(self, products: Sequence[Product]) -> StoreSanityResult:
        items = list(products)
        if len(items) < int(self.min_expected_products):
            return StoreSanityResult(
                ok=False,
                reason=(
                    f"sanity: got {len(items)} products, "
                    f"expected >= {self.min_expected_products}"
                ),
            )
        for p in items:
            if not p.store or not p.external_id or not p.url:
                return StoreSanityResult(
                    ok=False, reason="sanity: missing store/external_id/url"
                )
            if p.price is None or p.price < 10_000 or p.price > 5_000_000:
                return StoreSanityResult(
                    ok=False, reason=f"sanity: unreasonable price {p.price}"
                )
        return StoreSanityResult(ok=True)

    def collect_safe(self) -> StoreCollectOutcome:
        try:
            products = list(self.collect())
        except Exception as exc:  # noqa: BLE001 — store isolation
            return StoreCollectOutcome(
                store=self.slug,
                ok=False,
                products=[],
                error=f"{exc.__class__.__name__}: {exc}",
            )
        sanity = self.validate_products(products)
        if not sanity.ok:
            return StoreCollectOutcome(
                store=self.slug,
                ok=False,
                products=[],
                error=sanity.reason,
                sanity=sanity,
            )
        normalized: list[Product] = []
        for p in products:
            if p.store != self.slug:
                p = Product(
                    store=self.slug,
                    external_id=p.external_id,
                    url=p.url,
                    name=p.name,
                    sku=p.sku,
                    price=p.price,
                    available=p.available,
                    checked_at=p.checked_at,
                    metadata=dict(p.metadata or {}),
                )
            normalized.append(p)
        return StoreCollectOutcome(
            store=self.slug,
            ok=True,
            products=normalized,
            sanity=sanity,
        )

    def meta_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "region": self.region,
            "collection_mode": self.collection_mode,
            "reliability": self.reliability,
            "price_semantics": self.price_semantics,
        }
