from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Iterable, Sequence

from models import Product

DEFAULT_DB_PATH = Path("data") / "laptop_monitor.db"


@dataclass
class SaveStats:
    found: int = 0
    inserted: int = 0
    updated: int = 0
    price_changes: int = 0
    total_products: int = 0


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_db(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Открывает БД и гарантированно закрывает соединение (важно для Windows)."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


STRONG_IDENTIFIER_TYPES = (
    "sku",
    "manufacturer_part_number",
    "alternative_part_number",
)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            external_id TEXT NOT NULL,
            sku TEXT,
            name TEXT NOT NULL,
            url TEXT,
            price INTEGER,
            available INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL,
            UNIQUE(store, external_id)
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            price INTEGER,
            available INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS product_identifiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            identifier_type TEXT NOT NULL,
            value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            source TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id),
            UNIQUE(product_id, identifier_type, normalized_value)
        );

        CREATE INDEX IF NOT EXISTS idx_product_identifiers_normalized
            ON product_identifiers(normalized_value);

        CREATE INDEX IF NOT EXISTS idx_product_identifiers_type_normalized
            ON product_identifiers(identifier_type, normalized_value);

        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            product_id INTEGER,
            created_at TEXT NOT NULL,
            old_price INTEGER,
            new_price INTEGER,
            metadata_json TEXT,
            dedupe_key TEXT NOT NULL UNIQUE,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );

        CREATE INDEX IF NOT EXISTS idx_alert_events_type
            ON alert_events(event_type);

        CREATE INDEX IF NOT EXISTS idx_alert_events_product
            ON alert_events(product_id);

        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_event_id INTEGER,
            channel TEXT NOT NULL,
            destination TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_attempt_at TEXT,
            sent_at TEXT,
            last_error TEXT,
            provider_message_id TEXT,
            FOREIGN KEY(alert_event_id) REFERENCES alert_events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
            ON notification_deliveries(status);

        CREATE INDEX IF NOT EXISTS idx_notification_deliveries_event
            ON notification_deliveries(alert_event_id);

        CREATE TABLE IF NOT EXISTS notification_delivery_events (
            delivery_id INTEGER NOT NULL,
            alert_event_id INTEGER NOT NULL,
            PRIMARY KEY (delivery_id, alert_event_id),
            FOREIGN KEY(delivery_id) REFERENCES notification_deliveries(id),
            FOREIGN KEY(alert_event_id) REFERENCES alert_events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_notification_delivery_events_alert
            ON notification_delivery_events(alert_event_id);
        """
    )
    _migrate_notification_schema(conn)
    conn.commit()


def _table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return str(row["sql"]) if row and row["sql"] else None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(r["name"])
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _migrate_notification_schema(conn: sqlite3.Connection) -> None:
    """Безопасные миграции notification_deliveries + junction."""
    # Table may not exist yet on brand-new DB before executescript — but we call after.
    cols = _column_names(conn, "notification_deliveries")
    if not cols:
        return

    if "provider_message_id" not in cols:
        conn.execute(
            "ALTER TABLE notification_deliveries "
            "ADD COLUMN provider_message_id TEXT"
        )

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notification_delivery_events (
            delivery_id INTEGER NOT NULL,
            alert_event_id INTEGER NOT NULL,
            PRIMARY KEY (delivery_id, alert_event_id),
            FOREIGN KEY(delivery_id) REFERENCES notification_deliveries(id),
            FOREIGN KEY(alert_event_id) REFERENCES alert_events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_notification_delivery_events_alert
            ON notification_delivery_events(alert_event_id);
        """
    )

    # Backfill junction from legacy alert_event_id.
    conn.execute(
        """
        INSERT OR IGNORE INTO notification_delivery_events (
            delivery_id, alert_event_id
        )
        SELECT id, alert_event_id
        FROM notification_deliveries
        WHERE alert_event_id IS NOT NULL
        """
    )

    # Drop UNIQUE(alert_event_id, channel, destination) if present — rebuild table.
    table_sql = _table_sql(conn, "notification_deliveries") or ""
    normalized = " ".join(table_sql.split()).replace(" ", "").upper()
    if "UNIQUE(ALERT_EVENT_ID,CHANNEL,DESTINATION)" in normalized:
        cols = _column_names(conn, "notification_deliveries")
        if "provider_message_id" not in cols:
            conn.execute(
                "ALTER TABLE notification_deliveries "
                "ADD COLUMN provider_message_id TEXT"
            )
        # PRAGMA foreign_keys may only change outside a transaction.
        conn.commit()
        prev_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("DROP TABLE IF EXISTS notification_delivery_events")
            conn.execute("DROP TABLE IF EXISTS notification_deliveries_v2")
            conn.execute(
                """
                CREATE TABLE notification_deliveries_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_event_id INTEGER,
                    channel TEXT NOT NULL,
                    destination TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    sent_at TEXT,
                    last_error TEXT,
                    provider_message_id TEXT,
                    FOREIGN KEY(alert_event_id) REFERENCES alert_events(id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO notification_deliveries_v2 (
                    id, alert_event_id, channel, destination, status, attempts,
                    created_at, last_attempt_at, sent_at, last_error,
                    provider_message_id
                )
                SELECT
                    id, alert_event_id, channel, destination, status, attempts,
                    created_at, last_attempt_at, sent_at, last_error,
                    provider_message_id
                FROM notification_deliveries
                """
            )
            conn.execute("DROP TABLE notification_deliveries")
            conn.execute(
                "ALTER TABLE notification_deliveries_v2 "
                "RENAME TO notification_deliveries"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status
                    ON notification_deliveries(status)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_deliveries_event
                    ON notification_deliveries(alert_event_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE notification_delivery_events (
                    delivery_id INTEGER NOT NULL,
                    alert_event_id INTEGER NOT NULL,
                    PRIMARY KEY (delivery_id, alert_event_id),
                    FOREIGN KEY(delivery_id) REFERENCES notification_deliveries(id),
                    FOREIGN KEY(alert_event_id) REFERENCES alert_events(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_delivery_events_alert
                    ON notification_delivery_events(alert_event_id)
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO notification_delivery_events (
                    delivery_id, alert_event_id
                )
                SELECT id, alert_event_id
                FROM notification_deliveries
                WHERE alert_event_id IS NOT NULL
                """
            )
            conn.commit()
        finally:
            conn.execute(f"PRAGMA foreign_keys={'ON' if prev_fk else 'OFF'}")
    else:
        # Cleanup leftover from interrupted migration attempts.
        conn.execute("DROP TABLE IF EXISTS notification_deliveries_v2")


def count_products(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM products").fetchone()
    return int(row["cnt"])


def count_price_history(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM price_history").fetchone()
    return int(row["cnt"])


def count_product_identifiers(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM product_identifiers").fetchone()
    return int(row["cnt"])


def upsert_product_identifier(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    identifier_type: str,
    value: str,
    normalized_value: str,
    source: str | None,
    seen_at: str | None = None,
) -> str:
    """
    Сохраняет strong identifier без дублей.

    Returns:
        "inserted" | "updated" | "unchanged"
    """
    if identifier_type not in STRONG_IDENTIFIER_TYPES:
        raise ValueError(f"Unsupported identifier_type: {identifier_type}")
    if not value or not normalized_value:
        raise ValueError("value and normalized_value are required")

    now = seen_at or _iso(datetime.now(timezone.utc))
    existing = conn.execute(
        """
        SELECT id, value, source, last_seen_at
        FROM product_identifiers
        WHERE product_id = ?
          AND identifier_type = ?
          AND normalized_value = ?
        """,
        (product_id, identifier_type, normalized_value),
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO product_identifiers (
                product_id,
                identifier_type,
                value,
                normalized_value,
                source,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                identifier_type,
                value,
                normalized_value,
                source,
                now,
                now,
            ),
        )
        return "inserted"

    same_payload = existing["value"] == value and (
        source is None or existing["source"] == source
    )
    conn.execute(
        """
        UPDATE product_identifiers
        SET
            value = ?,
            source = COALESCE(?, source),
            last_seen_at = ?
        WHERE id = ?
        """,
        (value, source, now, existing["id"]),
    )
    return "unchanged" if same_payload else "updated"


def get_product_identifiers(
    conn: sqlite3.Connection,
    product_id: int | None = None,
) -> list[dict]:
    if product_id is None:
        rows = conn.execute(
            """
            SELECT
                pi.id,
                pi.product_id,
                pi.identifier_type,
                pi.value,
                pi.normalized_value,
                pi.source,
                pi.first_seen_at,
                pi.last_seen_at,
                p.store,
                p.external_id,
                p.sku AS product_sku
            FROM product_identifiers AS pi
            JOIN products AS p ON p.id = pi.product_id
            ORDER BY pi.product_id, pi.identifier_type, pi.normalized_value
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                pi.id,
                pi.product_id,
                pi.identifier_type,
                pi.value,
                pi.normalized_value,
                pi.source,
                pi.first_seen_at,
                pi.last_seen_at,
                p.store,
                p.external_id,
                p.sku AS product_sku
            FROM product_identifiers AS pi
            JOIN products AS p ON p.id = pi.product_id
            WHERE pi.product_id = ?
            ORDER BY pi.identifier_type, pi.normalized_value
            """,
            (product_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_all_identifiers(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    with open_db(db_path) as conn:
        init_db(conn)
        return get_product_identifiers(conn)


def get_product(
    conn: sqlite3.Connection,
    store: str,
    external_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM products
        WHERE store = ? AND external_id = ?
        """,
        (store, external_id),
    ).fetchone()


def get_price_history(
    store: str,
    external_id: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    with open_db(db_path) as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT
                ph.id,
                ph.product_id,
                ph.price,
                ph.available,
                ph.checked_at
            FROM price_history AS ph
            JOIN products AS p ON p.id = ph.product_id
            WHERE p.store = ? AND p.external_id = ?
            ORDER BY ph.checked_at ASC, ph.id ASC
            """,
            (store, external_id),
        ).fetchall()
        return [dict(row) for row in rows]


def _last_history_state(
    conn: sqlite3.Connection,
    product_id: int,
) -> tuple[int | None, int] | None:
    row = conn.execute(
        """
        SELECT price, available
        FROM price_history
        WHERE product_id = ?
        ORDER BY checked_at DESC, id DESC
        LIMIT 1
        """,
        (product_id,),
    ).fetchone()
    if row is None:
        return None
    return row["price"], int(row["available"])


def _insert_history(
    conn: sqlite3.Connection,
    product_id: int,
    price: int | None,
    available: bool,
    checked_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO price_history (product_id, price, available, checked_at)
        VALUES (?, ?, ?, ?)
        """,
        (product_id, price, int(available), checked_at),
    )


def upsert_product(conn: sqlite3.Connection, product: Product) -> tuple[str, bool]:
    """
    Сохраняет товар и при необходимости пишет price_history.

    Returns:
        (action, history_changed)
        action: "inserted" | "updated"
        history_changed: True только если у уже существующего товара
            изменились price/available и добавлена запись в history
    """
    checked_at = _iso(product.checked_at)
    available_int = int(bool(product.available))
    existing = get_product(conn, product.store, product.external_id)

    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO products (
                store,
                external_id,
                sku,
                name,
                url,
                price,
                available,
                first_seen_at,
                last_seen_at,
                last_checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product.store,
                product.external_id,
                product.sku,
                product.name,
                product.url,
                product.price,
                available_int,
                checked_at,
                checked_at,
                checked_at,
            ),
        )
        product_id = int(cur.lastrowid)
        _insert_history(
            conn,
            product_id,
            product.price,
            product.available,
            checked_at,
        )
        return "inserted", False

    product_id = int(existing["id"])
    conn.execute(
        """
        UPDATE products
        SET
            sku = ?,
            name = ?,
            url = ?,
            price = ?,
            available = ?,
            last_seen_at = ?,
            last_checked_at = ?
        WHERE id = ?
        """,
        (
            product.sku,
            product.name,
            product.url,
            product.price,
            available_int,
            checked_at,
            checked_at,
            product_id,
        ),
    )

    last_state = _last_history_state(conn, product_id)
    history_changed = False
    if last_state is None:
        _insert_history(
            conn,
            product_id,
            product.price,
            product.available,
            checked_at,
        )
        history_changed = True
    else:
        last_price, last_available = last_state
        if last_price != product.price or last_available != available_int:
            _insert_history(
                conn,
                product_id,
                product.price,
                product.available,
                checked_at,
            )
            history_changed = True

    return "updated", history_changed


def get_all_products(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Возвращает текущие товары из products как список dict."""
    with open_db(db_path) as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT
                id,
                store,
                external_id,
                sku,
                name,
                url,
                price,
                available,
                first_seen_at,
                last_seen_at,
                last_checked_at
            FROM products
            ORDER BY store, id
            """
        ).fetchall()
        return [
            {
                **dict(row),
                "available": bool(row["available"]),
            }
            for row in rows
        ]


def save_products(
    products: Iterable[Product],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> SaveStats:
    products_list = list(products)
    stats = SaveStats(found=len(products_list))

    with open_db(db_path) as conn:
        init_db(conn)
        for product in products_list:
            action, history_changed = upsert_product(conn, product)
            if action == "inserted":
                stats.inserted += 1
            else:
                stats.updated += 1
            if history_changed:
                stats.price_changes += 1
        stats.total_products = count_products(conn)

    return stats


def get_price_history_for_product(
    conn: sqlite3.Connection,
    product_id: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, product_id, price, available, checked_at
        FROM price_history
        WHERE product_id = ?
        ORDER BY checked_at ASC, id ASC
        """,
        (product_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_previous_price(
    conn: sqlite3.Connection,
    product_id: int,
) -> int | None:
    """Цена предыдущего наблюдения (вторая с конца). None если истории < 2."""
    rows = conn.execute(
        """
        SELECT price
        FROM price_history
        WHERE product_id = ?
        ORDER BY checked_at DESC, id DESC
        LIMIT 2
        """,
        (product_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    prev = rows[1]["price"]
    return int(prev) if prev is not None else None


def get_historical_min_before_current(
    conn: sqlite3.Connection,
    product_id: int,
) -> int | None:
    """
    Минимум цен до текущего (последнего) наблюдения.
    None если нет предыдущих ценовых точек.
    """
    rows = conn.execute(
        """
        SELECT price
        FROM price_history
        WHERE product_id = ?
        ORDER BY checked_at DESC, id DESC
        """,
        (product_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    previous_prices = [
        int(r["price"]) for r in rows[1:] if r["price"] is not None
    ]
    if not previous_prices:
        return None
    return min(previous_prices)


def alert_exists(conn: sqlite3.Connection, dedupe_key: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM alert_events WHERE dedupe_key = ? LIMIT 1
        """,
        (dedupe_key,),
    ).fetchone()
    return row is not None


def save_alert_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    product_id: int | None,
    dedupe_key: str,
    old_price: int | None = None,
    new_price: int | None = None,
    metadata: dict | None = None,
    created_at: str | None = None,
) -> dict | None:
    """
    Сохраняет alert event. При дубликате dedupe_key возвращает None.
    """
    now = created_at or _iso(datetime.now(timezone.utc))
    if alert_exists(conn, dedupe_key):
        return None
    try:
        cur = conn.execute(
            """
            INSERT INTO alert_events (
                event_type,
                product_id,
                created_at,
                old_price,
                new_price,
                metadata_json,
                dedupe_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                product_id,
                now,
                old_price,
                new_price,
                json.dumps(metadata or {}, ensure_ascii=False),
                dedupe_key,
            ),
        )
    except sqlite3.IntegrityError:
        return None
    return {
        "id": int(cur.lastrowid),
        "event_type": event_type,
        "product_id": product_id,
        "created_at": now,
        "old_price": old_price,
        "new_price": new_price,
        "metadata": metadata or {},
        "dedupe_key": dedupe_key,
    }


def get_alert_events(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    sql = """
        SELECT
            id,
            event_type,
            product_id,
            created_at,
            old_price,
            new_price,
            metadata_json,
            dedupe_key
        FROM alert_events
    """
    params: list = []
    if event_type:
        sql += " WHERE event_type = ?"
        params.append(event_type)
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        meta = {}
        raw = row["metadata_json"]
        if raw:
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                meta = {"raw": raw}
        result.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "product_id": row["product_id"],
                "created_at": row["created_at"],
                "old_price": row["old_price"],
                "new_price": row["new_price"],
                "metadata": meta,
                "dedupe_key": row["dedupe_key"],
            }
        )
    return result


def count_alert_events(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM alert_events").fetchone()
    return int(row["cnt"])


def count_unsent_alert_events(
    conn: sqlite3.Connection,
    *,
    channel: str,
    destination: str,
    max_attempts: int,
) -> int:
    """Число alert_events, которые deliver/--dry-run ещё будут рассматривать."""
    return len(
        get_undelivered_alert_events(
            conn,
            channel=channel,
            destination=destination,
            max_attempts=max_attempts,
        )
    )


def list_unsent_alert_event_ids(
    conn: sqlite3.Connection,
    *,
    channel: str,
    destination: str,
) -> list[int]:
    """ID alert_events без sent/skipped delivery по каналу."""
    events = get_undelivered_alert_events(
        conn,
        channel=channel,
        destination=destination,
        max_attempts=10**9,
    )
    return [int(e["id"]) for e in events]


def delete_unsent_alert_events(
    conn: sqlite3.Connection,
    *,
    channel: str,
    destination: str,
) -> dict[str, int]:
    """
    Удаляет alert_events без успешной/пропущенной delivery по каналу.

    Не трогает events со status=sent или status=skipped.
    Сначала удаляет связанные delivery (pending/failed), затем сами events.
    """
    destination = destination or ""
    rows = conn.execute(
        """
        SELECT
            ae.id AS event_id,
            nd.id AS delivery_id,
            nd.status AS delivery_status
        FROM alert_events AS ae
        LEFT JOIN notification_delivery_events AS nde
          ON nde.alert_event_id = ae.id
        LEFT JOIN notification_deliveries AS nd
          ON nd.id = nde.delivery_id
         AND nd.channel = ?
         AND COALESCE(nd.destination, '') = ?
        """,
        (channel, destination),
    ).fetchall()

    to_delete_events: set[int] = set()
    to_delete_deliveries: set[int] = set()
    protected = 0

    by_event: dict[int, list] = {}
    for row in rows:
        by_event.setdefault(int(row["event_id"]), []).append(row)

    for event_id, entries in by_event.items():
        statuses = {
            str(e["delivery_status"])
            for e in entries
            if e["delivery_status"] is not None
        }
        if DELIVERY_STATUS_SENT in statuses or DELIVERY_STATUS_SKIPPED in statuses:
            protected += 1
            continue
        to_delete_events.add(event_id)
        for e in entries:
            if e["delivery_id"] is not None:
                to_delete_deliveries.add(int(e["delivery_id"]))

    for delivery_id in sorted(to_delete_deliveries):
        conn.execute(
            "DELETE FROM notification_delivery_events WHERE delivery_id = ?",
            (delivery_id,),
        )
        conn.execute(
            "DELETE FROM notification_deliveries WHERE id = ?",
            (delivery_id,),
        )
    for event_id in sorted(to_delete_events):
        conn.execute(
            "DELETE FROM notification_delivery_events WHERE alert_event_id = ?",
            (event_id,),
        )
        conn.execute("DELETE FROM alert_events WHERE id = ?", (event_id,))

    return {
        "deleted_events": len(to_delete_events),
        "deleted_deliveries": len(to_delete_deliveries),
        "protected_events": protected,
    }


DELIVERY_STATUS_PENDING = "pending"
DELIVERY_STATUS_SENT = "sent"
DELIVERY_STATUS_FAILED = "failed"
DELIVERY_STATUS_SKIPPED = "skipped"


def _row_to_delivery(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "alert_event_id": row["alert_event_id"],
        "channel": row["channel"],
        "destination": row["destination"],
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "created_at": row["created_at"],
        "last_attempt_at": row["last_attempt_at"],
        "sent_at": row["sent_at"],
        "last_error": row["last_error"],
        "provider_message_id": (
            row["provider_message_id"] if "provider_message_id" in keys else None
        ),
    }


def get_delivery(
    conn: sqlite3.Connection,
    *,
    alert_event_id: int,
    channel: str,
    destination: str,
) -> dict | None:
    destination = destination or ""
    row = conn.execute(
        """
        SELECT nd.*
        FROM notification_deliveries AS nd
        JOIN notification_delivery_events AS nde
          ON nde.delivery_id = nd.id
        WHERE nde.alert_event_id = ?
          AND nd.channel = ?
          AND COALESCE(nd.destination, '') = ?
        ORDER BY nd.id DESC
        LIMIT 1
        """,
        (alert_event_id, channel, destination),
    ).fetchone()
    if row is not None:
        return _row_to_delivery(row)
    # Legacy fallback.
    row = conn.execute(
        """
        SELECT *
        FROM notification_deliveries
        WHERE alert_event_id = ?
          AND channel = ?
          AND COALESCE(destination, '') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (alert_event_id, channel, destination),
    ).fetchone()
    return _row_to_delivery(row) if row else None


def link_delivery_events(
    conn: sqlite3.Connection,
    delivery_id: int,
    alert_event_ids: Sequence[int],
) -> None:
    for event_id in alert_event_ids:
        conn.execute(
            """
            INSERT OR IGNORE INTO notification_delivery_events (
                delivery_id, alert_event_id
            )
            VALUES (?, ?)
            """,
            (delivery_id, int(event_id)),
        )


def create_delivery_for_events(
    conn: sqlite3.Connection,
    *,
    alert_event_ids: Sequence[int],
    channel: str,
    destination: str,
    status: str = DELIVERY_STATUS_PENDING,
) -> dict:
    """Создаёт delivery и связывает его со всеми alert_event_ids."""
    if not alert_event_ids:
        raise ValueError("alert_event_ids required")
    destination = destination or ""
    primary = int(sorted(int(x) for x in alert_event_ids)[0])
    now = _iso(datetime.now(timezone.utc))
    cur = conn.execute(
        """
        INSERT INTO notification_deliveries (
            alert_event_id,
            channel,
            destination,
            status,
            attempts,
            created_at,
            last_attempt_at,
            sent_at,
            last_error,
            provider_message_id
        )
        VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL)
        """,
        (primary, channel, destination, status, now),
    )
    delivery_id = int(cur.lastrowid)
    link_delivery_events(conn, delivery_id, alert_event_ids)
    row = conn.execute(
        "SELECT * FROM notification_deliveries WHERE id = ?",
        (delivery_id,),
    ).fetchone()
    return _row_to_delivery(row)


def ensure_delivery(
    conn: sqlite3.Connection,
    *,
    alert_event_id: int,
    channel: str,
    destination: str,
    status: str = DELIVERY_STATUS_PENDING,
) -> dict:
    """Создаёт delivery record если его ещё нет; иначе возвращает существующий."""
    destination = destination or ""
    existing = get_delivery(
        conn,
        alert_event_id=alert_event_id,
        channel=channel,
        destination=destination,
    )
    if existing is not None:
        return existing
    return create_delivery_for_events(
        conn,
        alert_event_ids=[alert_event_id],
        channel=channel,
        destination=destination,
        status=status,
    )


def find_retryable_delivery_for_events(
    conn: sqlite3.Connection,
    *,
    alert_event_ids: Sequence[int],
    channel: str,
    destination: str,
    max_attempts: int,
) -> dict | None:
    """Ищет pending/failed delivery, связанный с любым из events."""
    destination = destination or ""
    ids = [int(x) for x in alert_event_ids]
    if not ids:
        return None
    placeholders = ",".join("?" for _ in ids)
    row = conn.execute(
        f"""
        SELECT nd.*
        FROM notification_deliveries AS nd
        JOIN notification_delivery_events AS nde
          ON nde.delivery_id = nd.id
        WHERE nde.alert_event_id IN ({placeholders})
          AND nd.channel = ?
          AND COALESCE(nd.destination, '') = ?
          AND nd.status IN ('pending', 'failed')
          AND nd.attempts < ?
        ORDER BY nd.id DESC
        LIMIT 1
        """,
        (*ids, channel, destination, max_attempts),
    ).fetchone()
    return _row_to_delivery(row) if row else None


def get_delivery_event_ids(
    conn: sqlite3.Connection,
    delivery_id: int,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT alert_event_id
        FROM notification_delivery_events
        WHERE delivery_id = ?
        ORDER BY alert_event_id
        """,
        (delivery_id,),
    ).fetchall()
    return [int(r["alert_event_id"]) for r in rows]


def update_delivery(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    status: str | None = None,
    attempts: int | None = None,
    last_attempt_at: str | None = None,
    sent_at: str | None = None,
    last_error: str | None | object = ...,
    provider_message_id: str | int | None | object = ...,
) -> dict:
    fields: list[str] = []
    params: list = []
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if attempts is not None:
        fields.append("attempts = ?")
        params.append(attempts)
    if last_attempt_at is not None:
        fields.append("last_attempt_at = ?")
        params.append(last_attempt_at)
    if sent_at is not None:
        fields.append("sent_at = ?")
        params.append(sent_at)
    if last_error is not ...:
        fields.append("last_error = ?")
        params.append(last_error)
    if provider_message_id is not ...:
        fields.append("provider_message_id = ?")
        params.append(
            str(provider_message_id) if provider_message_id is not None else None
        )
    if not fields:
        row = conn.execute(
            "SELECT * FROM notification_deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()
        return _row_to_delivery(row)

    params.append(delivery_id)
    conn.execute(
        f"UPDATE notification_deliveries SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    row = conn.execute(
        "SELECT * FROM notification_deliveries WHERE id = ?",
        (delivery_id,),
    ).fetchone()
    return _row_to_delivery(row)


def get_undelivered_alert_events(
    conn: sqlite3.Connection,
    *,
    channel: str,
    destination: str,
    max_attempts: int,
) -> list[dict]:
    """
    Alert events без sent/skipped delivery по каналу,
    и без exhausted pending/failed (>= max_attempts).
    """
    destination = destination or ""
    rows = conn.execute(
        """
        SELECT
            ae.id,
            ae.event_type,
            ae.product_id,
            ae.created_at,
            ae.old_price,
            ae.new_price,
            ae.metadata_json,
            ae.dedupe_key
        FROM alert_events AS ae
        WHERE ae.id NOT IN (
            SELECT nde.alert_event_id
            FROM notification_delivery_events AS nde
            JOIN notification_deliveries AS nd ON nd.id = nde.delivery_id
            WHERE nd.channel = ?
              AND COALESCE(nd.destination, '') = ?
              AND nd.status IN ('sent', 'skipped')
        )
        AND ae.id NOT IN (
            SELECT nde.alert_event_id
            FROM notification_delivery_events AS nde
            JOIN notification_deliveries AS nd ON nd.id = nde.delivery_id
            WHERE nd.channel = ?
              AND COALESCE(nd.destination, '') = ?
              AND nd.status IN ('pending', 'failed')
              AND nd.attempts >= ?
        )
        -- legacy fallback without junction rows
        AND ae.id NOT IN (
            SELECT nd.alert_event_id
            FROM notification_deliveries AS nd
            WHERE nd.alert_event_id IS NOT NULL
              AND nd.channel = ?
              AND COALESCE(nd.destination, '') = ?
              AND nd.status IN ('sent', 'skipped')
              AND NOT EXISTS (
                  SELECT 1 FROM notification_delivery_events nde
                  WHERE nde.delivery_id = nd.id
              )
        )
        ORDER BY ae.id ASC
        """,
        (
            channel,
            destination,
            channel,
            destination,
            max_attempts,
            channel,
            destination,
        ),
    ).fetchall()

    result = []
    for row in rows:
        meta = {}
        raw = row["metadata_json"]
        if raw:
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError:
                meta = {"raw": raw}
        result.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "product_id": row["product_id"],
                "created_at": row["created_at"],
                "old_price": row["old_price"],
                "new_price": row["new_price"],
                "metadata": meta,
                "dedupe_key": row["dedupe_key"],
            }
        )
    return result


def get_delivery_stats(
    conn: sqlite3.Connection,
    *,
    channel: str | None = None,
) -> dict[str, int]:
    sql = """
        SELECT status, COUNT(*) AS cnt
        FROM notification_deliveries
    """
    params: list = []
    if channel:
        sql += " WHERE channel = ?"
        params.append(channel)
    sql += " GROUP BY status"
    rows = conn.execute(sql, params).fetchall()
    stats = {
        DELIVERY_STATUS_PENDING: 0,
        DELIVERY_STATUS_SENT: 0,
        DELIVERY_STATUS_FAILED: 0,
        DELIVERY_STATUS_SKIPPED: 0,
    }
    for row in rows:
        stats[str(row["status"])] = int(row["cnt"])
    return stats


def count_notification_deliveries(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM notification_deliveries"
    ).fetchone()
    return int(row["cnt"])


def baseline_existing_alert_events(
    conn: sqlite3.Connection,
    *,
    channel: str,
    destination: str,
) -> int:
    """
    Помечает все существующие alert_events как skipped для канала.
    Не перезаписывает уже sent/failed/pending записи.
    Returns: количество новых skipped записей.
    """
    destination = destination or ""
    events = get_alert_events(conn)
    created = 0
    for event in events:
        existing = get_delivery(
            conn,
            alert_event_id=int(event["id"]),
            channel=channel,
            destination=destination,
        )
        if existing is not None:
            continue
        create_delivery_for_events(
            conn,
            alert_event_ids=[int(event["id"])],
            channel=channel,
            destination=destination,
            status=DELIVERY_STATUS_SKIPPED,
        )
        # annotate last_error for audit
        delivery = get_delivery(
            conn,
            alert_event_id=int(event["id"]),
            channel=channel,
            destination=destination,
        )
        if delivery:
            update_delivery(
                conn,
                int(delivery["id"]),
                last_error="baseline-existing",
            )
        created += 1
    return created
