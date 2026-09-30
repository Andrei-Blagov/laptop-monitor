from __future__ import annotations

"""Multi-store collection via StoreAdapter registry."""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from models import Product
from storage import DEFAULT_DB_PATH, SaveStats, save_products
from stores.base import StoreAdapter, StoreCollectOutcome
from stores.registry import enabled_adapters

logger = logging.getLogger(__name__)

# Back-compat alias used by older call sites / tests.
StoreFetcher = Callable[[], Sequence[Product]]


@dataclass
class StoreCollectResult:
    """Совместимый wrapper над StoreCollectOutcome."""

    store: str
    ok: bool
    products: list[Product] = field(default_factory=list)
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.products)

    @classmethod
    def from_outcome(cls, outcome: StoreCollectOutcome) -> StoreCollectResult:
        return cls(
            store=outcome.store,
            ok=outcome.ok,
            products=list(outcome.products),
            error=outcome.error,
        )


@dataclass
class CollectionResult:
    stores: dict[str, StoreCollectResult] = field(default_factory=dict)
    save_stats: SaveStats | None = None

    # Back-compat for Regard/ANDPRO-era pipeline tests.
    @property
    def regard(self) -> StoreCollectResult:
        return self.stores.get(
            "regard",
            StoreCollectResult(store="regard", ok=False, error="not collected"),
        )

    @property
    def andpro(self) -> StoreCollectResult:
        return self.stores.get(
            "andpro",
            StoreCollectResult(store="andpro", ok=False, error="not collected"),
        )

    @property
    def successful_stores(self) -> list[str]:
        return [s for s, r in self.stores.items() if r.ok]

    @property
    def failed_stores(self) -> list[str]:
        return [s for s, r in self.stores.items() if not r.ok]

    @property
    def both_ok(self) -> bool:
        """Все участвующие stores успешны (для enabled set)."""
        return bool(self.stores) and all(r.ok for r in self.stores.values())

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.stores.values())

    @property
    def none_ok(self) -> bool:
        return not self.any_ok

    @property
    def products(self) -> list[Product]:
        out: list[Product] = []
        for result in self.stores.values():
            if result.ok:
                out.extend(result.products)
        return out


def collect_store(
    store_name: str,
    fetcher: StoreFetcher,
) -> StoreCollectResult:
    """Legacy helper: collect one store via callable fetcher."""
    try:
        products = list(fetcher())
        return StoreCollectResult(store=store_name, ok=True, products=products)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Сбой магазина %s", store_name)
        return StoreCollectResult(
            store=store_name,
            ok=False,
            products=[],
            error=f"{exc.__class__.__name__}: {exc}",
        )


def collect_products(
    *,
    fetch_regard: StoreFetcher | None = None,
    fetch_andpro: StoreFetcher | None = None,
    adapters: Sequence[StoreAdapter] | None = None,
    fetchers: dict[str, StoreFetcher] | None = None,
) -> CollectionResult:
    """
    Собирает enabled stores независимо.

    Back-compat: fetch_regard / fetch_andpro override adapters for those slugs.
    """
    result = CollectionResult()
    override: dict[str, StoreFetcher] = dict(fetchers or {})
    if fetch_regard is not None:
        override["regard"] = fetch_regard
    if fetch_andpro is not None:
        override["andpro"] = fetch_andpro

    if adapters is None and (fetch_regard is not None or fetch_andpro is not None):
        # Test / legacy path: only run stores that have fetchers provided,
        # plus defaults if one side missing.
        from parsers.andpro import fetch_target_laptops as fetch_andpro_default
        from parsers.regard import fetch_target_laptops as fetch_regard_default

        pairs = [
            ("regard", override.get("regard", fetch_regard_default)),
            ("andpro", override.get("andpro", fetch_andpro_default)),
        ]
        # If only one override intended for failure tests that still pass both...
        # Always collect both for back-compat with existing pipeline tests.
        for slug, fetcher in pairs:
            result.stores[slug] = collect_store(slug, fetcher)
        return result

    for adapter in adapters if adapters is not None else enabled_adapters():
        if adapter.slug in override:
            result.stores[adapter.slug] = collect_store(
                adapter.slug, override[adapter.slug]
            )
        else:
            outcome = adapter.collect_safe()
            result.stores[adapter.slug] = StoreCollectResult.from_outcome(outcome)
    return result


def write_collection_diagnostic(
    collection: CollectionResult,
    last_run_path: Path | str,
) -> None:
    path = Path(last_run_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stores_payload = {
        slug: {
            "ok": r.ok,
            "count": r.count if r.ok else None,
            "error": r.error,
        }
        for slug, r in collection.stores.items()
    }
    path.write_text(
        json.dumps(
            {
                "stores": stores_payload,
                "regard_ok": collection.regard.ok,
                "andpro_ok": collection.andpro.ok,
                "regard_count": (
                    collection.regard.count if collection.regard.ok else None
                ),
                "andpro_count": (
                    collection.andpro.count if collection.andpro.ok else None
                ),
                "count": len(collection.products),
                "incomplete": not collection.both_ok,
                "products": [asdict(p) for p in collection.products],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def persist_collection(
    collection: CollectionResult,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    last_run_path: Path | str | None = None,
    only_successful: bool = True,
) -> SaveStats:
    """
    Сохраняет продукты успешных магазинов.

    only_successful=True (default): failed stores не пишутся → snapshot stale.
    """
    products = collection.products if only_successful else []
    if not only_successful:
        for r in collection.stores.values():
            products.extend(r.products)
    if last_run_path is not None:
        write_collection_diagnostic(collection, last_run_path)
    stats = save_products(products, db_path)
    collection.save_stats = stats
    return stats
