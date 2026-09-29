from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from models import Product
from storage import (
    count_price_history,
    count_products,
    get_price_history,
    get_product,
    init_db,
    open_db,
    save_products,
)


def _product(
    *,
    external_id: str = "1001",
    price: int | None = 100_000,
    available: bool = True,
    name: str = "Test Laptop",
) -> Product:
    return Product(
        store="test_store",
        external_id=external_id,
        url=f"https://example.test/product/{external_id}",
        name=name,
        sku="SKU-1001",
        price=price,
        available=available,
        checked_at=datetime.now(timezone.utc),
    )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmpdir.name) / "test_laptop_monitor.db"
        # Защита: тесты никогда не должны трогать production DB.
        self.assertNotEqual(
            self.db_path.resolve(),
            (Path("data") / "laptop_monitor.db").resolve(),
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_new_product_inserts_product_and_history(self) -> None:
        stats = save_products([_product()], self.db_path)
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.updated, 0)
        self.assertEqual(stats.price_changes, 0)

        with open_db(self.db_path) as conn:
            self.assertEqual(count_products(conn), 1)
            self.assertEqual(count_price_history(conn), 1)

    def test_unchanged_resave_does_not_duplicate(self) -> None:
        save_products([_product(price=100_000)], self.db_path)
        stats = save_products([_product(price=100_000)], self.db_path)

        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.price_changes, 0)

        with open_db(self.db_path) as conn:
            self.assertEqual(count_products(conn), 1)
            self.assertEqual(count_price_history(conn), 1)

    def test_price_change_updates_product_and_history(self) -> None:
        save_products([_product(price=100_000)], self.db_path)
        stats = save_products([_product(price=110_000)], self.db_path)

        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.price_changes, 1)

        with open_db(self.db_path) as conn:
            row = get_product(conn, "test_store", "1001")
            assert row is not None
            self.assertEqual(row["price"], 110_000)
            self.assertEqual(count_products(conn), 1)
            self.assertEqual(count_price_history(conn), 2)

    def test_available_change_updates_product_and_history(self) -> None:
        save_products([_product(available=True)], self.db_path)
        stats = save_products([_product(available=False)], self.db_path)

        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.price_changes, 1)

        with open_db(self.db_path) as conn:
            row = get_product(conn, "test_store", "1001")
            assert row is not None
            self.assertEqual(row["available"], 0)
            self.assertEqual(count_price_history(conn), 2)

    def test_price_reverts_adds_another_history_row(self) -> None:
        save_products([_product(price=100_000)], self.db_path)
        save_products([_product(price=110_000)], self.db_path)
        stats = save_products([_product(price=100_000)], self.db_path)

        self.assertEqual(stats.price_changes, 1)

        history = get_price_history("test_store", "1001", self.db_path)
        self.assertEqual(len(history), 3)
        self.assertEqual([h["price"] for h in history], [100_000, 110_000, 100_000])

    def test_uses_temporary_db_not_production(self) -> None:
        production = Path("data") / "laptop_monitor.db"
        production_mtime_before = (
            production.stat().st_mtime if production.exists() else None
        )
        production_size_before = (
            production.stat().st_size if production.exists() else None
        )

        save_products([_product()], self.db_path)

        if production.exists() and production_mtime_before is not None:
            after = production.stat()
            self.assertEqual(after.st_mtime, production_mtime_before)
            self.assertEqual(after.st_size, production_size_before)

        with open_db(self.db_path) as conn:
            init_db(conn)
            self.assertEqual(count_products(conn), 1)

        self.assertTrue(
            self.db_path.is_relative_to(Path(self._tmpdir.name))
            or str(self.db_path).startswith(tempfile.gettempdir())
        )

if __name__ == "__main__":
    unittest.main()
