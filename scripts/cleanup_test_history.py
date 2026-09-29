"""Одноразовая очистка искусственных тестовых записей price_history.

Не импортируется и не вызывается из main.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("data") / "laptop_monitor.db"
STORE = "regard"
EXTERNAL_ID = "763832"
ARTIFICIAL_PRICE = 223190


def cleanup() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    product = conn.execute(
        """
        SELECT id, price
        FROM products
        WHERE store = ? AND external_id = ?
        """,
        (STORE, EXTERNAL_ID),
    ).fetchone()
    if product is None:
        raise SystemExit(f"Товар {STORE}/{EXTERNAL_ID} не найден")

    product_id = int(product["id"])
    history = conn.execute(
        """
        SELECT id, price, available, checked_at
        FROM price_history
        WHERE product_id = ?
        ORDER BY checked_at ASC, id ASC
        """,
        (product_id,),
    ).fetchall()

    print("История до очистки:")
    for row in history:
        print(dict(row))

    if not history:
        raise SystemExit("У товара нет записей price_history")

    # Удаляем искусственный скачок 223190 и следующую за ним тестовую запись 222190.
    ids_to_delete: list[int] = []
    for index, row in enumerate(history):
        if row["price"] == ARTIFICIAL_PRICE:
            ids_to_delete.append(int(row["id"]))
            if index + 1 < len(history):
                nxt = history[index + 1]
                if nxt["price"] != ARTIFICIAL_PRICE:
                    ids_to_delete.append(int(nxt["id"]))
            break

    if not ids_to_delete:
        print("Искусственные записи не найдены — ничего удалять не нужно")
    else:
        placeholders = ",".join("?" for _ in ids_to_delete)
        conn.execute(
            f"DELETE FROM price_history WHERE id IN ({placeholders})",
            ids_to_delete,
        )
        print(f"Удалены history id: {ids_to_delete}")

    # products.price должен остаться реальным (из первой/последней реальной истории).
    remaining = conn.execute(
        """
        SELECT id, price, checked_at
        FROM price_history
        WHERE product_id = ?
        ORDER BY checked_at ASC, id ASC
        """,
        (product_id,),
    ).fetchall()
    if not remaining:
        raise SystemExit("После очистки не осталось реальной истории")

    real_price = remaining[-1]["price"]
    conn.execute(
        """
        UPDATE products
        SET price = ?
        WHERE id = ?
        """,
        (real_price, product_id),
    )
    conn.commit()

    products_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    history_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    has_artificial = conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE price = ?",
        (ARTIFICIAL_PRICE,),
    ).fetchone()[0]

    print("История после очистки:")
    for row in remaining:
        print(dict(row))
    print(f"products.price={real_price}")
    print(f"products={products_count}")
    print(f"price_history={history_count}")
    print(f"artificial_223190_left={has_artificial}")
    conn.close()


if __name__ == "__main__":
    cleanup()
