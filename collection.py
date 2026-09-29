from __future__ import annotations

"""Сбор офферов Regard / ANDPRO без ложных unavailable при сбое магазина."""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from models import Product
from parsers.andpro import fetch_target_laptops as fetch_andpro_default
from parsers.regard import fetch_target_laptops as fetch_regard_default
from storage import DEFAULT_DB_PATH, SaveStats, save_products

logger = logging.getLogger(__name__)

StoreFetcher = Callable[[], Sequence[Product]]


@dataclass
class StoreCollectResult:
    store: str
    ok: bool
    products: list[Product] = field(default_factory=list)
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.products)


@dataclass
class CollectionResult:
    regard: StoreCollectResult
    andpro: StoreCollectResult
    save_stats: SaveStats | None = None

    @property
    def both_ok(self) -> bool:
        return self.regard.ok and self.andpro.ok

    @property
    def any_ok(self) -> bool:
        return self.regard.ok or self.andpro.ok

    @property
    def none_ok(self) -> bool:
        return not self.any_ok

    @property
    def products(self) -> list[Product]:
        out: list[Product] = []
        if self.regard.ok:
            out.extend(self.regard.products)
        if self.andpro.ok:
            out.extend(self.andpro.products)
        return out


def collect_store(
    store_name: str,
    fetcher: StoreFetcher,
) -> StoreCollectResult:
    """
    Собирает один магазин.

    При исключении возвращает ok=False и пустой список products —
    caller НЕ должен трактовать это как «0 товаров в наличии».
    """
    try:
        products = list(fetcher())
        return StoreCollectResult(store=store_name, ok=True, products=products)
    except Exception as exc:
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
) -> CollectionResult:
    """Собирает Regard и ANDPRO независимо."""
    regard = collect_store("regard", fetch_regard or fetch_regard_default)
    andpro = collect_store("andpro", fetch_andpro or fetch_andpro_default)
    return CollectionResult(regard=regard, andpro=andpro)


def write_collection_diagnostic(
    collection: CollectionResult,
    last_run_path: Path | str,
) -> None:
    """Пишет diagnostic JSON без изменения production DB."""
    path = Path(last_run_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    products = collection.products
    path.write_text(
        json.dumps(
            {
                "regard_ok": collection.regard.ok,
                "andpro_ok": collection.andpro.ok,
                "regard_count": (
                    collection.regard.count if collection.regard.ok else None
                ),
                "andpro_count": (
                    collection.andpro.count if collection.andpro.ok else None
                ),
                "count": len(products),
                "incomplete": not collection.both_ok,
                "products": [asdict(p) for p in products],
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
) -> SaveStats:
    """
    Сохраняет продукты успешно собранных магазинов в production DB.

    Для неполного collection вызывающий код (pipeline) не должен
    вызывать эту функцию — иначе можно потерять PRICE_DROP.
    """
    products = collection.products
    if last_run_path is not None:
        write_collection_diagnostic(collection, last_run_path)
    stats = save_products(products, db_path)
    collection.save_stats = stats
    return stats
