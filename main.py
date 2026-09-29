from __future__ import annotations

import logging
from dataclasses import asdict
import json
from pathlib import Path

from parsers.andpro import fetch_target_laptops as fetch_andpro
from parsers.regard import fetch_target_laptops as fetch_regard
from storage import DEFAULT_DB_PATH, save_products


logger = logging.getLogger(__name__)


def _safe_fetch(store_name: str, fetcher):
    try:
        products = fetcher()
        print(f"{store_name}:")
        print(f"  найдено: {len(products)}")
        return products
    except Exception:
        logger.exception("Сбой магазина %s", store_name)
        print(f"{store_name}:")
        print("  ошибка сбора (магазин пропущен)")
        return []


def main() -> None:
    regard_products = _safe_fetch("Regard", fetch_regard)
    andpro_products = _safe_fetch("ANDPRO", fetch_andpro)

    products = regard_products + andpro_products
    if not products:
        print("Товары не получены ни из одного магазина")
        return

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/last_run.json").write_text(
        json.dumps(
            {
                "regard_count": len(regard_products),
                "andpro_count": len(andpro_products),
                "count": len(products),
                "products": [asdict(p) for p in products],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    stats = save_products(products, DEFAULT_DB_PATH)

    print(f"Всего получено: {stats.found}")
    print(f"Новых товаров: {stats.inserted}")
    print(f"Обновлено: {stats.updated}")
    print(f"Изменений цены/наличия: {stats.price_changes}")
    print(f"Всего товаров в БД: {stats.total_products}")


if __name__ == "__main__":
    main()
