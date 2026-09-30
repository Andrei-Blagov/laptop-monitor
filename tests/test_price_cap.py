from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import config
from alerts import (
    EVENT_CROSS_STORE,
    EVENT_PRICE_DROP,
    evaluate_cross_store,
    evaluate_price_drop,
)
from comparison import Offer, ProductMatch
from deal_ranking import format_top_deals_message, rank_clusters
from models import Product
from storage import init_db, open_db, upsert_product


class PriceCapHelperTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        self.assertEqual(config.MAX_TRACKED_PRICE_RUB, 300_000)
        self.assertTrue(config.is_price_in_tracking_scope(299_999))
        self.assertTrue(config.is_price_in_tracking_scope(300_000))
        self.assertFalse(config.is_price_in_tracking_scope(300_001))
        self.assertFalse(config.is_price_in_tracking_scope(None))
        self.assertFalse(config.is_price_in_tracking_scope(0))
        self.assertFalse(config.is_price_in_tracking_scope(-1))


class PriceCapAlertTests(unittest.TestCase):
    def _seed_product(
        self,
        conn,
        *,
        store: str,
        external_id: str,
        price: int,
        sku: str = "SKU-CAP",
    ) -> dict:
        now = datetime.now(timezone.utc)
        p = Product(
            store=store,
            external_id=external_id,
            url=f"https://example/{store}/{external_id}",
            name=f"Laptop {sku} RTX 5070 Ti",
            sku=sku,
            price=price,
            available=True,
            checked_at=now,
        )
        upsert_product(conn, p)
        row = conn.execute(
            "SELECT * FROM products WHERE store=? AND external_id=?",
            (store, external_id),
        ).fetchone()
        return dict(row)

    def test_drop_above_cap_no_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                product = self._seed_product(
                    conn, store="regard", external_id="1", price=500_000
                )
                conn.execute(
                    "UPDATE products SET price=? WHERE id=?",
                    (400_000, product["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO price_history (product_id, price, available, checked_at)
                    VALUES (?, 500000, 1, ?), (?, 400000, 1, ?)
                    """,
                    (
                        product["id"],
                        "2026-01-01T00:00:00+00:00",
                        product["id"],
                        "2026-01-02T00:00:00+00:00",
                    ),
                )
                product = dict(
                    conn.execute(
                        "SELECT * FROM products WHERE id=?", (product["id"],)
                    ).fetchone()
                )
                self.assertIsNone(evaluate_price_drop(conn, product))

    def test_drop_into_scope_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                # First observation at 320k
                product = self._seed_product(
                    conn, store="regard", external_id="2", price=320_000
                )
                # Second observation drops into scope
                now = datetime.now(timezone.utc)
                upsert_product(
                    conn,
                    Product(
                        store="regard",
                        external_id="2",
                        url="https://example/regard/2",
                        name="Laptop SKU-CAP RTX 5070 Ti",
                        sku="SKU-CAP",
                        price=295_000,
                        available=True,
                        checked_at=now,
                    ),
                )
                product = dict(
                    conn.execute(
                        "SELECT * FROM products WHERE id=?", (product["id"],)
                    ).fetchone()
                )
                event = evaluate_price_drop(conn, product)
                self.assertIsNotNone(event)
                assert event is not None
                self.assertEqual(event["event_type"], EVENT_PRICE_DROP)
                self.assertEqual(event["new_price"], 295_000)

    def test_drop_305k_still_out_of_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                product = self._seed_product(
                    conn, store="regard", external_id="3", price=320_000
                )
                conn.execute(
                    "UPDATE products SET price=? WHERE id=?",
                    (305_000, product["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO price_history (product_id, price, available, checked_at)
                    VALUES (?, 320000, 1, ?), (?, 305000, 1, ?)
                    """,
                    (
                        product["id"],
                        "2026-01-01T00:00:00+00:00",
                        product["id"],
                        "2026-01-02T00:00:00+00:00",
                    ),
                )
                product = dict(
                    conn.execute(
                        "SELECT * FROM products WHERE id=?", (product["id"],)
                    ).fetchone()
                )
                self.assertIsNone(evaluate_price_drop(conn, product))


class PriceCapCrossStoreTests(unittest.TestCase):
    def _offer(self, store: str, price: int, *, pid: int = 1) -> Offer:
        return Offer(
            store=store,
            external_id=f"{store}-{price}",
            name="Same model RTX 5070 Ti",
            sku="SAME-SKU",
            price=price,
            available=True,
            url=f"https://x/{store}",
            product_id=pid,
        )

    def test_two_in_scope_allowed(self) -> None:
        offers = [
            self._offer("kns", 280_000, pid=1),
            self._offer("regard", 299_000, pid=2),
        ]
        match = ProductMatch(
            normalized_sku="SAMESKU",
            display_sku="SAME-SKU",
            name="Model",
            offers=offers,
            match_method="sku",
            matched_identifier="SAME-SKU",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                for o in offers:
                    upsert_product(
                        conn,
                        Product(
                            store=o.store,
                            external_id=o.external_id,
                            url=o.url or "",
                            name=o.name,
                            sku=o.sku,
                            price=o.price,
                            available=True,
                            checked_at=datetime.now(timezone.utc),
                        ),
                    )
                refreshed: list[Offer] = []
                for o in offers:
                    row = conn.execute(
                        "SELECT id FROM products WHERE store=? AND external_id=?",
                        (o.store, o.external_id),
                    ).fetchone()
                    refreshed.append(
                        Offer(
                            store=o.store,
                            external_id=o.external_id,
                            name=o.name,
                            sku=o.sku,
                            price=o.price,
                            available=o.available,
                            url=o.url,
                            product_id=int(row["id"]),
                        )
                    )
                match.offers = refreshed
                created = evaluate_cross_store(conn, [match])
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0]["event_type"], EVENT_CROSS_STORE)

    def test_one_in_scope_no_cross(self) -> None:
        match = ProductMatch(
            normalized_sku="SAMESKU",
            display_sku="SAME-SKU",
            name="Model",
            offers=[
                self._offer("kns", 290_000),
                self._offer("regard", 310_000),
            ],
            match_method="sku",
            matched_identifier="SAME-SKU",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                self.assertEqual(evaluate_cross_store(conn, [match]), [])

    def test_all_above_cap_excluded(self) -> None:
        match = ProductMatch(
            normalized_sku="SAMESKU",
            display_sku="SAME-SKU",
            name="Model",
            offers=[
                self._offer("kns", 310_000),
                self._offer("regard", 320_000),
            ],
            match_method="sku",
            matched_identifier="SAME-SKU",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            with open_db(db) as conn:
                init_db(conn)
                self.assertEqual(evaluate_cross_store(conn, [match]), [])


class PriceCapRankingTests(unittest.TestCase):
    def _match(self, name: str, offers: list[tuple[str, int]]) -> ProductMatch:
        return ProductMatch(
            normalized_sku=name.replace(" ", ""),
            display_sku=name,
            name=name,
            offers=[
                Offer(
                    store=store,
                    external_id=f"{store}-{i}",
                    name=name,
                    sku=name,
                    price=price,
                    available=True,
                    url=f"https://x/{store}",
                    product_id=i + 1,
                )
                for i, (store, price) in enumerate(offers)
            ],
            match_method="sku",
            matched_identifier=name,
        )

    def test_top_excludes_above_cap(self) -> None:
        matches = [
            self._match("Cheap", [("kns", 250_000)]),
            self._match("AtCap", [("regard", 300_000)]),
            self._match("JustOver", [("andpro", 300_001)]),
            self._match("Expensive", [("citilink", 400_000)]),
            self._match("Mixed", [("kns", 289_000), ("regard", 315_000)]),
        ]
        ranked = rank_clusters(matches, limit=10)
        prices = [d.price for d in ranked]
        self.assertTrue(all(p is not None and p <= 300_000 for p in prices))
        names = {d.cluster_name for d in ranked}
        self.assertIn("Cheap", names)
        self.assertIn("AtCap", names)
        self.assertIn("Mixed", names)
        self.assertNotIn("JustOver", names)
        self.assertNotIn("Expensive", names)
        mixed = next(d for d in ranked if d.cluster_name == "Mixed")
        self.assertEqual(mixed.store, "kns")
        self.assertEqual(mixed.price, 289_000)
        self.assertIsNone(mixed.next_store)

    def test_telegram_top_title_and_empty(self) -> None:
        text = format_top_deals_message([])
        self.assertIn("300 000", text)
        self.assertIn("Нет свежих предложений до", text)
        ranked = rank_clusters(
            [self._match("Ok", [("kns", 220_000)])], limit=5
        )
        msg = format_top_deals_message(ranked)
        self.assertIn("ТОП ПРЕДЛОЖЕНИЙ ДО 300 000 ₽", msg)


class PriceCapN8nPayloadTests(unittest.TestCase):
    def test_rank_for_n8n_excludes_over_cap(self) -> None:
        match = ProductMatch(
            normalized_sku="S18",
            display_sku="S18",
            name="Stealth 18",
            offers=[
                Offer(
                    store="citilink",
                    external_id="1",
                    name="Stealth 18",
                    sku="S18",
                    price=419_900,
                    available=True,
                    url="https://c/1",
                    product_id=1,
                )
            ],
            match_method="sku",
            matched_identifier="S18",
        )
        self.assertEqual(rank_clusters([match], limit=10), [])


if __name__ == "__main__":
    unittest.main()
