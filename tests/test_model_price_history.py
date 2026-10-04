from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from comparison import Offer, ProductMatch, match_products
from control_bot import BTN_HISTORY, process_update
from model_price_history import (
    CALLBACK_DATA_LIMIT,
    CALLBACK_LIST,
    CALLBACK_MENU,
    TELEGRAM_MESSAGE_SOFT_LIMIT,
    assert_history_readonly,
    build_history_picker_items,
    build_model_history_report,
    find_cluster_by_product_id,
    format_delta_line,
    format_model_history_message,
    make_model_callback,
    parse_model_callback,
    price_at_or_before,
    short_model_button_text,
)
from models import Product
from storage import (
    count_price_history,
    count_products,
    create_pipeline_run,
    get_product,
    init_db,
    insert_store_run,
    open_db,
    save_products,
)


def _now() -> datetime:
    return datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)


def _product(
    store: str,
    external_id: str,
    *,
    price: int,
    available: bool = True,
    sku: str = "MODEL-A",
    name: str = "Gigabyte A16 Pro",
    checked_at: datetime | None = None,
) -> Product:
    return Product(
        store=store,
        external_id=external_id,
        url=f"https://example.test/{store}/{external_id}",
        name=name,
        sku=sku,
        price=price,
        available=available,
        checked_at=checked_at or _now(),
    )


def _seed_fresh_ok(conn, stores: list[str], when: datetime) -> None:
    rid = create_pipeline_run(
        conn,
        started_at=when.isoformat(),
        status="success",
        app_version="0.2.0",
        instance_id="test",
    )
    for store in stores:
        insert_store_run(
            conn,
            pipeline_run_id=rid,
            store=store,
            attempted_at=when.isoformat(),
            finished_at=when.isoformat(),
            status="ok",
            products_count=1,
        )


def _insert_history(
    conn,
    product_id: int,
    price: int,
    checked_at: datetime,
    *,
    available: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO price_history (product_id, price, available, checked_at)
        VALUES (?, ?, ?, ?)
        """,
        (product_id, price, int(available), checked_at.isoformat()),
    )


class ModelPriceHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmpdir.name) / "hist.db"
        self.assertNotEqual(
            self.db.resolve(),
            (Path("data") / "laptop_monitor.db").resolve(),
        )
        with open_db(self.db) as conn:
            init_db(conn)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _seed_cluster(self) -> dict[str, int]:
        """KNS/Regard/ANDPRO/Citilink same SKU; mixed history incl. >300k."""
        now = _now()
        save_products(
            [
                _product("kns", "k1", price=220_485, checked_at=now),
                _product("regard", "r1", price=226_840, checked_at=now),
                _product("andpro", "a1", price=236_000, checked_at=now),
                _product(
                    "citilink",
                    "c1",
                    price=229_000,
                    available=False,
                    checked_at=now,
                ),
            ],
            self.db,
        )
        with open_db(self.db) as conn:
            ids = {}
            for store, eid in (
                ("kns", "k1"),
                ("regard", "r1"),
                ("andpro", "a1"),
                ("citilink", "c1"),
            ):
                row = get_product(conn, store, eid)
                assert row is not None
                ids[store] = int(row["id"])
            # Wipe auto history from save_products, insert controlled timeline.
            conn.execute("DELETE FROM price_history")
            # KNS: was 350k historically, then 206391, then 229000, now 220485
            _insert_history(
                conn, ids["kns"], 350_000, now - timedelta(days=60)
            )
            _insert_history(
                conn, ids["kns"], 206_391, now - timedelta(days=40)
            )
            _insert_history(
                conn, ids["kns"], 229_000, now - timedelta(days=10)
            )
            _insert_history(conn, ids["kns"], 220_485, now - timedelta(hours=1))
            # Regard: flat
            _insert_history(
                conn, ids["regard"], 226_840, now - timedelta(days=20)
            )
            # ANDPRO: older higher
            _insert_history(
                conn, ids["andpro"], 250_000, now - timedelta(days=25)
            )
            _insert_history(
                conn, ids["andpro"], 236_000, now - timedelta(days=2)
            )
            # Citilink unavailable now
            _insert_history(
                conn, ids["citilink"], 240_000, now - timedelta(days=15)
            )
            _insert_history(
                conn, ids["citilink"], 229_000, now - timedelta(days=1)
            )
            _seed_fresh_ok(
                conn, ["kns", "regard", "andpro", "citilink"], now
            )
            conn.commit()
        return ids

    def test_1_picker_only_le_300k(self) -> None:
        now = _now()
        save_products(
            [
                _product("kns", "cheap", price=220_000, sku="CHEAP"),
                _product("regard", "cheap-r", price=225_000, sku="CHEAP"),
                _product(
                    "kns", "dear", price=350_000, sku="DEAR", name="Overpriced X"
                ),
                _product(
                    "regard",
                    "dear-r",
                    price=360_000,
                    sku="DEAR",
                    name="Overpriced X",
                ),
            ],
            self.db,
        )
        with open_db(self.db) as conn:
            _seed_fresh_ok(conn, ["kns", "regard"], now)
            conn.commit()
        items = build_history_picker_items(self.db, limit=10)
        prices = [i.price for i in items]
        self.assertTrue(prices)
        self.assertTrue(all(p <= 300_000 for p in prices))
        self.assertTrue(all("Overpriced" not in i.name for i in items))

    def test_2_callback_data_le_64_bytes(self) -> None:
        data = make_model_callback(12_345_678)
        self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_DATA_LIMIT)
        self.assertEqual(parse_model_callback(data), 12_345_678)

    def test_3_cluster_resolved_by_product_id(self) -> None:
        ids = self._seed_cluster()
        products = __import__("storage", fromlist=["get_all_products_readonly"]).get_all_products_readonly(self.db)
        matches = match_products(products, []).matches
        # SKU matching without identifiers still groups same SKU
        cluster = find_cluster_by_product_id(matches, ids["regard"])
        self.assertIsNotNone(cluster)
        assert cluster is not None
        pids = {o.product_id for o in cluster.offers}
        self.assertIn(ids["kns"], pids)
        self.assertIn(ids["regard"], pids)

    def test_4_current_best_across_stores(self) -> None:
        ids = self._seed_cluster()
        report = build_model_history_report(self.db, ids["regard"], now=_now())
        self.assertEqual(report.best_now, 220_485)
        self.assertEqual(report.best_now_store, "kns")

    def test_5_historical_minimum_across_stores(self) -> None:
        ids = self._seed_cluster()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        self.assertEqual(report.all_time_min, 206_391)
        self.assertEqual(report.all_time_min_store, "kns")

    def test_6_per_store_minimum(self) -> None:
        ids = self._seed_cluster()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        by_store = {s.store: s for s in report.stores}
        self.assertEqual(by_store["kns"].historical_min, 206_391)
        self.assertEqual(by_store["kns"].current_price, 220_485)
        self.assertEqual(by_store["regard"].historical_min, 226_840)
        self.assertEqual(by_store["regard"].current_price, 226_840)

    def test_7_7d_cutoff_latest_le_cutoff(self) -> None:
        hist = [
            {"price": 100, "checked_at": (_now() - timedelta(days=20)).isoformat()},
            {"price": 90, "checked_at": (_now() - timedelta(days=8)).isoformat()},
            {"price": 80, "checked_at": (_now() - timedelta(days=1)).isoformat()},
        ]
        cutoff = _now() - timedelta(days=7)
        self.assertEqual(price_at_or_before(hist, cutoff), 90)

    def test_8_30d_cutoff_latest_le_cutoff(self) -> None:
        hist = [
            {"price": 100, "checked_at": (_now() - timedelta(days=40)).isoformat()},
            {"price": 95, "checked_at": (_now() - timedelta(days=31)).isoformat()},
            {"price": 88, "checked_at": (_now() - timedelta(days=10)).isoformat()},
        ]
        cutoff = _now() - timedelta(days=30)
        self.assertEqual(price_at_or_before(hist, cutoff), 95)

    def test_9_no_data_at_cutoff_insufficient(self) -> None:
        ids = self._seed_cluster()
        # Brand-new product history only today → 30d insufficient
        with open_db(self.db) as conn:
            conn.execute("DELETE FROM price_history WHERE product_id=?", (ids["regard"],))
            _insert_history(conn, ids["regard"], 226_840, _now())
            conn.commit()
        # Use a singleton? Still cluster. Force report on cluster with only recent hist for all?
        # Clear all history older than 5 days for whole cluster.
        with open_db(self.db) as conn:
            conn.execute(
                "DELETE FROM price_history WHERE checked_at < ?",
                ((_now() - timedelta(days=5)).isoformat(),),
            )
            conn.commit()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        self.assertIsNone(report.best_30d_ago)
        self.assertEqual(
            format_delta_line(report.best_now, report.best_30d_ago),
            "недостаточно данных",
        )

    def test_10_historical_over_300k_preserved(self) -> None:
        ids = self._seed_cluster()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        # All-time min is 206391, but 350k must still be part of history calc path
        # (min is not 350k — presence verified via step at old cutoff)
        cutoff = _now() - timedelta(days=50)
        from storage import get_price_history_for_products_readonly

        hist = get_price_history_for_products_readonly(self.db, [ids["kns"]])[ids["kns"]]
        self.assertEqual(price_at_or_before(hist, cutoff), 350_000)
        self.assertLess(report.all_time_min or 0, 300_000)

    def test_11_unavailable_not_best(self) -> None:
        ids = self._seed_cluster()
        # Make KNS unavailable and cheaper than Regard — best should skip it.
        save_products(
            [
                _product("kns", "k1", price=100_000, available=False),
                _product("regard", "r1", price=226_840, available=True),
                _product("andpro", "a1", price=236_000, available=True),
                _product("citilink", "c1", price=229_000, available=False),
            ],
            self.db,
        )
        with open_db(self.db) as conn:
            _seed_fresh_ok(conn, ["kns", "regard", "andpro", "citilink"], _now())
            conn.commit()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        self.assertEqual(report.best_now, 226_840)
        self.assertEqual(report.best_now_store, "regard")
        cit = next(s for s in report.stores if s.store == "citilink")
        self.assertFalse(cit.available)

    def test_12_stale_store_excluded_from_current_best(self) -> None:
        ids = self._seed_cluster()
        with open_db(self.db) as conn:
            # Only regard fresh; kns last run failed → stale
            insert_store_run(
                conn,
                pipeline_run_id=None,
                store="kns",
                attempted_at=_now().isoformat(),
                finished_at=_now().isoformat(),
                status="failed",
                products_count=0,
                error_message="boom",
            )
            conn.commit()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        self.assertNotEqual(report.best_now_store, "kns")
        self.assertEqual(report.best_now, 226_840)

    def test_13_history_readonly_counts_unchanged(self) -> None:
        ids = self._seed_cluster()
        before = assert_history_readonly(self.db)
        build_history_picker_items(self.db)
        build_model_history_report(self.db, ids["kns"], now=_now())
        format_model_history_message(
            build_model_history_report(self.db, ids["kns"], now=_now())
        )
        after = assert_history_readonly(self.db)
        self.assertEqual(before, after)
        with open_db(self.db) as conn:
            self.assertEqual(count_products(conn), before[0])
            self.assertEqual(count_price_history(conn), before[1])

    def test_14_unknown_product_graceful(self) -> None:
        report = build_model_history_report(self.db, 999999, now=_now())
        self.assertFalse(report.found)
        msg = format_model_history_message(report)
        self.assertIn("не найдена", msg.lower())

    def test_15_message_below_size_limit(self) -> None:
        ids = self._seed_cluster()
        msg = format_model_history_message(
            build_model_history_report(self.db, ids["kns"], now=_now())
        )
        self.assertLessEqual(len(msg), TELEGRAM_MESSAGE_SOFT_LIMIT)
        self.assertIn("ИСТОРИЯ ЦЕНЫ", msg)
        self.assertIn("Изменение:", msg)

    def test_button_text_and_menu_callback(self) -> None:
        text = short_model_button_text("Gigabyte A16 Pro Super Long Name", 220_485)
        self.assertIn("220 485", text)
        self.assertEqual(BTN_HISTORY, "ctrl:hist")
        self.assertEqual(CALLBACK_LIST, "hist:list")
        self.assertEqual(CALLBACK_MENU, "hist:menu")

    def test_7d_delta_from_cluster(self) -> None:
        ids = self._seed_cluster()
        report = build_model_history_report(self.db, ids["kns"], now=_now())
        # At 7d ago: KNS latest <=cutoff is 229000 (10d ago), Regard 226840, etc.
        # best_7d = min(229000, 226840, 250000?, 240000) = 226840 from regard
        # ANDPRO at 25d is 250000, at 2d is 236000 — at 7d cutoff 250000
        self.assertEqual(report.best_7d_ago, 226_840)
        self.assertIsNotNone(report.best_30d_ago)
        line = format_delta_line(report.best_now, report.best_7d_ago)
        self.assertTrue(line.startswith("−") or line.startswith("+") or line.startswith("0"))

    def test_control_bot_history_callback_no_pipeline(self) -> None:
        ids = self._seed_cluster()
        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": True},
        )
        with patch("control_bot.config.get_telegram_admin_chat_ids", return_value={"1"}):
            with patch("control_bot.run_pipeline") as run_pipe:
                with patch("control_bot.DEFAULT_DB_PATH", self.db):
                    process_update(
                        client,
                        "TOKEN",
                        {
                            "callback_query": {
                                "id": "cb1",
                                "data": BTN_HISTORY,
                                "message": {
                                    "message_id": 10,
                                    "chat": {"id": 1},
                                },
                            }
                        },
                    )
                    process_update(
                        client,
                        "TOKEN",
                        {
                            "callback_query": {
                                "id": "cb2",
                                "data": make_model_callback(ids["kns"]),
                                "message": {
                                    "message_id": 10,
                                    "chat": {"id": 1},
                                },
                            }
                        },
                    )
                run_pipe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
