from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from comparison import Offer, ProductMatch
from control_bot import send_photo
from model_price_history import CALLBACK_DATA_LIMIT, assert_history_readonly
from models import Product
from price_history_chart import (
    PNG_SIGNATURE,
    build_price_history_chart,
    build_step_segments,
    earliest_valid_timestamp,
    history_events,
    make_chart_callback,
    open_figure_count,
    parse_chart_callback,
    period_start,
    render_price_history_png,
    StoreSeries,
)
from storage import (
    count_alert_events,
    count_price_history,
    count_products,
    create_pipeline_run,
    get_product,
    init_db,
    insert_store_run,
    open_db,
    save_products,
)
from telegram_safe import redact_secrets, safe_exc_message, telegram_http_error_summary


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
    price: int | None,
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


def _counts(db: Path) -> tuple[int, int, int, int]:
    with open_db(db) as conn:
        pipelines = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        return (
            count_products(conn),
            count_price_history(conn),
            count_alert_events(conn),
            int(pipelines),
        )


class PriceHistoryChartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmpdir.name) / "chart.db"
        with open_db(self.db) as conn:
            init_db(conn)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _seed_cluster(self) -> dict[str, int]:
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
            ids: dict[str, int] = {}
            for store, eid in (
                ("kns", "k1"),
                ("regard", "r1"),
                ("andpro", "a1"),
                ("citilink", "c1"),
            ):
                row = get_product(conn, store, eid)
                assert row is not None
                ids[store] = int(row["id"])
            conn.execute("DELETE FROM price_history")
            _insert_history(conn, ids["kns"], 350_000, now - timedelta(days=60))
            _insert_history(conn, ids["kns"], 330_000, now - timedelta(days=45))
            _insert_history(conn, ids["kns"], 206_391, now - timedelta(days=40))
            _insert_history(conn, ids["kns"], 229_000, now - timedelta(days=10))
            _insert_history(conn, ids["kns"], 220_485, now - timedelta(hours=1))
            _insert_history(conn, ids["regard"], 226_840, now - timedelta(days=20))
            _insert_history(conn, ids["andpro"], 250_000, now - timedelta(days=25))
            _insert_history(conn, ids["andpro"], 236_000, now - timedelta(days=2))
            _insert_history(conn, ids["citilink"], 240_000, now - timedelta(days=15))
            _insert_history(
                conn,
                ids["citilink"],
                229_000,
                now - timedelta(days=1),
                available=False,
            )
            _seed_fresh_ok(conn, ["kns", "regard", "andpro", "citilink"], now)
            conn.commit()
        return ids

    # --- 1–4 callbacks ---
    def test_01_callback_30_le_64(self) -> None:
        data = make_chart_callback("30", 12_345_678)
        self.assertEqual(data, "hist:chart:30:12345678")
        self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_DATA_LIMIT)

    def test_02_callback_90_le_64(self) -> None:
        data = make_chart_callback("90", 12_345_678)
        self.assertEqual(data, "hist:chart:90:12345678")
        self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_DATA_LIMIT)

    def test_03_callback_all_le_64(self) -> None:
        data = make_chart_callback("all", 12_345_678)
        self.assertEqual(data, "hist:chart:all:12345678")
        self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_DATA_LIMIT)

    def test_04_parse_chart_callback(self) -> None:
        self.assertEqual(parse_chart_callback("hist:chart:30:42"), ("30", 42))
        self.assertEqual(parse_chart_callback("hist:chart:90:7"), ("90", 7))
        self.assertEqual(parse_chart_callback("hist:chart:all:9"), ("all", 9))
        self.assertIsNone(parse_chart_callback("hist:chart:bogus:1"))
        self.assertIsNone(parse_chart_callback("hist:model:1"))

    # --- 5–7 periods ---
    def test_05_30d_cutoff(self) -> None:
        start = period_start("30", now=_now(), earliest=None)
        self.assertEqual(start, _now() - timedelta(days=30))

    def test_06_90d_cutoff(self) -> None:
        start = period_start("90", now=_now(), earliest=None)
        self.assertEqual(start, _now() - timedelta(days=90))

    def test_07_all_starts_at_earliest(self) -> None:
        earliest = _now() - timedelta(days=60)
        start = period_start("all", now=_now(), earliest=earliest)
        self.assertEqual(start, earliest)
        ids = self._seed_cluster()
        from storage import get_price_history_for_products_readonly

        hist = get_price_history_for_products_readonly(self.db, list(ids.values()))
        self.assertEqual(earliest_valid_timestamp(hist), _now() - timedelta(days=60))

    # --- 8–13 step / availability / current ---
    def test_08_seed_before_period_used(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        events = history_events(
            [
                {
                    "price": 350_000,
                    "available": 1,
                    "checked_at": (now - timedelta(days=60)).isoformat(),
                },
                {
                    "price": 220_000,
                    "available": 1,
                    "checked_at": (now - timedelta(days=10)).isoformat(),
                },
            ]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=220_000,
            current_available=True,
        )
        self.assertTrue(segs)
        self.assertEqual(segs[0][0], (start, 350_000.0))

    def test_09_step_function_semantics(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        t1 = now - timedelta(days=20)
        t2 = now - timedelta(days=5)
        events = history_events(
            [
                {"price": 100, "available": 1, "checked_at": t1.isoformat()},
                {"price": 90, "available": 1, "checked_at": t2.isoformat()},
            ]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=90,
            current_available=True,
        )
        self.assertEqual(len(segs), 1)
        xs = [p[0] for p in segs[0]]
        ys = [p[1] for p in segs[0]]
        self.assertEqual(ys[0], 100.0)
        self.assertIn(t2, xs)
        self.assertEqual(ys[xs.index(t2)], 90.0)
        self.assertEqual(xs[-1], now)
        self.assertEqual(ys[-1], 90.0)

    def test_10_unavailable_creates_gap(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        t_ok = now - timedelta(days=20)
        t_na = now - timedelta(days=10)
        events = history_events(
            [
                {"price": 100, "available": 1, "checked_at": t_ok.isoformat()},
                {"price": 100, "available": 0, "checked_at": t_na.isoformat()},
            ]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=100,
            current_available=False,
        )
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][-1][0], t_na)
        self.assertNotEqual(segs[0][-1][0], now)

    def test_11_later_available_resumes(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        t1 = now - timedelta(days=25)
        t_na = now - timedelta(days=15)
        t2 = now - timedelta(days=5)
        events = history_events(
            [
                {"price": 100, "available": 1, "checked_at": t1.isoformat()},
                {"price": 100, "available": 0, "checked_at": t_na.isoformat()},
                {"price": 95, "available": 1, "checked_at": t2.isoformat()},
            ]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=95,
            current_available=True,
        )
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0][-1][0], t_na)
        self.assertEqual(segs[1][0], (t2, 95.0))
        self.assertEqual(segs[1][-1], (now, 95.0))

    def test_12_current_price_appended(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        t1 = now - timedelta(days=3)
        events = history_events(
            [{"price": 200, "available": 1, "checked_at": t1.isoformat()}]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=180,
            current_available=True,
        )
        self.assertEqual(segs[0][-1], (now, 180.0))

    def test_13_current_unavailable_not_extended(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        t1 = now - timedelta(days=3)
        events = history_events(
            [{"price": 200, "available": 1, "checked_at": t1.isoformat()}]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=200,
            current_available=False,
        )
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][-1][0], t1)
        self.assertNotIn(now, [p[0] for p in segs[0]])

    # --- 14–16 prices / multi-store ---
    def test_14_historical_over_300k_retained(self) -> None:
        ids = self._seed_cluster()
        result = build_price_history_chart(self.db, ids["kns"], "all", now=_now())
        self.assertTrue(result.ok)
        assert result.png is not None
        # Series must include 350k in constructed segments path
        from price_history_chart import (
            _load_match_and_histories,
            build_store_series_for_cluster,
            period_start as _ps,
            earliest_valid_timestamp as _earliest,
        )

        match, histories, _ = _load_match_and_histories(self.db, ids["kns"])
        assert match is not None
        start = _ps("all", now=_now(), earliest=_earliest(histories))
        series = build_store_series_for_cluster(
            match, histories, start=start, now=_now()
        )
        kns = next(s for s in series if s.store == "kns")
        prices = [p for seg in kns.segments for _, p in seg]
        self.assertIn(350_000.0, prices)
        self.assertIn(330_000.0, prices)

    def test_15_invalid_null_zero_prices_ignored(self) -> None:
        now = _now()
        start = now - timedelta(days=30)
        events = history_events(
            [
                {"price": 0, "available": 1, "checked_at": (now - timedelta(days=20)).isoformat()},
                {"price": None, "available": 1, "checked_at": (now - timedelta(days=15)).isoformat()},
                {"price": -5, "available": 1, "checked_at": (now - timedelta(days=12)).isoformat()},
                {"price": 210_000, "available": 1, "checked_at": (now - timedelta(days=5)).isoformat()},
            ]
        )
        segs = build_step_segments(
            events,
            start=start,
            now=now,
            current_price=210_000,
            current_available=True,
        )
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][0][1], 210_000.0)

    def test_16_multi_store_separate_series(self) -> None:
        ids = self._seed_cluster()
        from price_history_chart import (
            _load_match_and_histories,
            build_store_series_for_cluster,
            period_start as _ps,
            earliest_valid_timestamp as _earliest,
        )

        match, histories, _ = _load_match_and_histories(self.db, ids["kns"])
        assert match is not None
        start = _ps("90", now=_now(), earliest=_earliest(histories))
        series = build_store_series_for_cluster(
            match, histories, start=start, now=_now()
        )
        stores = {s.store for s in series}
        self.assertEqual(stores, {"kns", "regard", "andpro", "citilink"})
        self.assertTrue(all(isinstance(s, StoreSeries) for s in series))

    # --- 17–20 PNG / no-data / figures ---
    def test_17_png_signature(self) -> None:
        ids = self._seed_cluster()
        result = build_price_history_chart(self.db, ids["kns"], "30", now=_now())
        self.assertTrue(result.ok)
        assert result.png is not None
        self.assertTrue(result.png.startswith(PNG_SIGNATURE))

    def test_18_bytesio_non_empty(self) -> None:
        png = render_price_history_png(
            [
                StoreSeries(
                    store="kns",
                    segments=[
                        [
                            (_now() - timedelta(days=10), 220_000.0),
                            (_now(), 210_000.0),
                        ]
                    ],
                )
            ],
            title="Тест Модель",
            period_label="30 дней",
        )
        self.assertGreater(len(png), 1000)
        self.assertTrue(png.startswith(PNG_SIGNATURE))

    def test_19_no_data_controlled(self) -> None:
        save_products([_product("kns", "empty", price=200_000, sku="EMPTY")], self.db)
        with open_db(self.db) as conn:
            row = get_product(conn, "kns", "empty")
            assert row is not None
            pid = int(row["id"])
            conn.execute("DELETE FROM price_history")
            # unavailable current + no history → no drawable segments
            conn.execute(
                "UPDATE products SET available=0 WHERE id=?",
                (pid,),
            )
            conn.commit()
        result = build_price_history_chart(self.db, pid, "30", now=_now())
        self.assertFalse(result.ok)
        self.assertIsNone(result.png)
        self.assertIn("Недостаточно данных", result.error or "")

    def test_20_rendering_closes_figures(self) -> None:
        before = open_figure_count()
        for _ in range(5):
            render_price_history_png(
                [
                    StoreSeries(
                        store="kns",
                        segments=[[(_now() - timedelta(days=1), 1.0), (_now(), 2.0)]],
                    )
                ],
                title="Leak check",
                period_label="30 дней",
            )
        self.assertEqual(open_figure_count(), before)

    # --- 21–23 Telegram sendPhoto ---
    def test_21_send_photo_success(self) -> None:
        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 1}},
        )
        ok = send_photo(client, "123:ABC", 1, b"\x89PNG\r\n\x1a\n" + b"x" * 20, caption="hi")
        self.assertTrue(ok)
        self.assertTrue(client.post.called)
        kwargs = client.post.call_args.kwargs
        self.assertIn("files", kwargs)
        self.assertEqual(kwargs["files"]["photo"][0], "price_history.png")

    def test_22_send_photo_http_api_failure(self) -> None:
        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=400,
            json=lambda: {"ok": False, "description": "bad request"},
        )
        self.assertFalse(
            send_photo(client, "123:ABC", 1, b"\x89PNG\r\n\x1a\n" + b"x" * 20)
        )
        client.post.side_effect = Exception("boom")
        # httpx.HTTPError path — generic Exception still returns False via HTTPError? 
        # send_photo catches httpx.HTTPError and Timeout; generic Exception would raise.
        # Simulate API ok=false already covered; simulate timeout:
        import httpx

        client.post.side_effect = httpx.TimeoutException("timeout")
        self.assertFalse(
            send_photo(client, "123:ABC", 1, b"\x89PNG\r\n\x1a\n" + b"x" * 20)
        )

    def test_23_token_sanitization(self) -> None:
        tokenish = "https://api.telegram.org/bot123456:AAHideMe/sendPhoto"
        redacted = redact_secrets(tokenish)
        self.assertNotIn("AAHideMe", redacted)
        self.assertIn("[REDACTED]", redacted)
        summary = telegram_http_error_summary(
            method="sendPhoto",
            description=tokenish,
        )
        self.assertNotIn("AAHideMe", summary)
        self.assertNotIn("123456:AAHideMe", safe_exc_message(RuntimeError(tokenish)))

    # --- 24–25 readonly / unknown ---
    def test_24_db_readonly_counts_unchanged(self) -> None:
        ids = self._seed_cluster()
        before = _counts(self.db)
        hist_before = assert_history_readonly(self.db)
        build_price_history_chart(self.db, ids["kns"], "30", now=_now())
        build_price_history_chart(self.db, ids["kns"], "90", now=_now())
        build_price_history_chart(self.db, ids["kns"], "all", now=_now())
        after = _counts(self.db)
        self.assertEqual(before, after)
        self.assertEqual(hist_before, assert_history_readonly(self.db))

    def test_25_unknown_product_graceful(self) -> None:
        result = build_price_history_chart(self.db, 999999, "30", now=_now())
        self.assertFalse(result.ok)
        self.assertIsNone(result.png)
        self.assertIn("не найдена", (result.error or "").lower())


if __name__ == "__main__":
    unittest.main()
