from __future__ import annotations

import logging
from pathlib import Path

from collection import collect_products, persist_collection
from storage import DEFAULT_DB_PATH


logger = logging.getLogger(__name__)


def main() -> None:
    collection = collect_products()
    if collection.regard.ok:
        print("Regard:")
        print(f"  найдено: {collection.regard.count}")
    else:
        print("Regard:")
        print("  ошибка сбора (магазин пропущен)")
    if collection.andpro.ok:
        print("ANDPRO:")
        print(f"  найдено: {collection.andpro.count}")
    else:
        print("ANDPRO:")
        print("  ошибка сбора (магазин пропущен)")

    if collection.none_ok:
        print("Товары не получены ни из одного магазина")
        return

    Path("data").mkdir(parents=True, exist_ok=True)
    stats = persist_collection(
        collection,
        DEFAULT_DB_PATH,
        last_run_path=Path("data") / "last_run.json",
    )

    print(f"Всего получено: {stats.found}")
    print(f"Новых товаров: {stats.inserted}")
    print(f"Обновлено: {stats.updated}")
    print(f"Изменений цены/наличия: {stats.price_changes}")
    print(f"Всего товаров в БД: {stats.total_products}")


if __name__ == "__main__":
    main()
