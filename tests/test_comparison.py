from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from comparison import match_products, normalize_sku
from identity_sync import (
    identifiers_from_identity,
    identifiers_from_product_row,
    sync_product_identifiers,
    upsert_identifier_rows,
)
from models import Product
from product_identity import ProductIdentity, find_config_conflicts, normalize_identifier
from storage import (
    count_product_identifiers,
    get_product_identifiers,
    init_db,
    open_db,
    save_products,
    upsert_product_identifier,
)


def _row(
    *,
    store: str,
    external_id: str,
    sku: str | None,
    price: int | None,
    available: bool = True,
    name: str = "Test Laptop",
    url: str = "https://example.test/p",
    product_id: int | None = None,
) -> dict:
    data = {
        "store": store,
        "external_id": external_id,
        "sku": sku,
        "price": price,
        "available": available,
        "name": name,
        "url": url,
    }
    if product_id is not None:
        data["id"] = product_id
    return data


def _identity(
    *,
    store: str,
    external_id: str,
    sku: str | None,
    name: str = "Laptop",
    cpu: str | None = "RYZEN 9 8940HX",
    gpu: str | None = "RTX 5070 TI LAPTOP",
    gpu_vram_gb: int | None = 12,
    ram_gb: int | None = 16,
    ssd_gb: int | None = 1024,
    screen_size_inch: float | None = 16.0,
    screen_resolution: str | None = "1920x1200",
    manufacturer_part_number: str | None = None,
    alternative_part_numbers: list[str] | None = None,
) -> ProductIdentity:
    return ProductIdentity(
        store=store,
        external_id=external_id,
        sku=sku,
        name=name,
        price=100,
        available=True,
        url="https://example.test",
        brand="ASUS",
        model=sku,
        manufacturer_part_number=manufacturer_part_number,
        alternative_part_numbers=alternative_part_numbers or [],
        cpu=cpu,
        gpu=gpu,
        gpu_vram_gb=gpu_vram_gb,
        ram_gb=ram_gb,
        ssd_gb=ssd_gb,
        screen_size_inch=screen_size_inch,
        screen_resolution=screen_resolution,
        source_identifiers={
            "vendorcode": sku if store == "regard" else None,
            "mpn": manufacturer_part_number,
        },
    )


class NormalizeSkuTests(unittest.TestCase):
    def test_strip_and_upper(self) -> None:
        self.assertEqual(normalize_sku("  dxhg4kzcc4sd  "), "DXHG4KZCC4SD")

    def test_whitespace_removed(self) -> None:
        self.assertEqual(normalize_sku("DXHG 4KZ CC4SD"), "DXHG4KZCC4SD")

    def test_none(self) -> None:
        self.assertIsNone(normalize_sku(None))
        self.assertIsNone(normalize_sku("   "))

    def test_keeps_hyphens_and_dots(self) -> None:
        self.assertEqual(normalize_sku("9s7-15fl35-239"), "9S7-15FL35-239")
        self.assertEqual(normalize_sku("nh.qvzcd.007"), "NH.QVZCD.007")


class MatchProductsTests(unittest.TestCase):
    def test_exact_sku_match_method(self) -> None:
        result = match_products(
            [
                _row(store="regard", external_id="1", sku="DXHG4KZCC4SD", price=222190, product_id=1),
                _row(store="andpro", external_id="2", sku="DXHG4KZCC4SD", price=227798, product_id=2),
            ]
        )
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.match_method, "sku")
        self.assertEqual(match.matched_identifier, "DXHG4KZCC4SD")
        self.assertEqual(match.price_difference, 5608)
        self.assertEqual(match.cheapest_available.store, "regard")

    def test_regard_alt_pn_equals_andpro_mpn(self) -> None:
        products = [
            _row(
                store="regard",
                external_id="749493",
                sku="G614PR-RV027",
                price=270190,
                product_id=10,
                name="ASUS G614PR-RV027",
            ),
            _row(
                store="andpro",
                external_id="239106",
                sku="90NR0NJ7-M001J0",
                price=283605,
                product_id=20,
                name="ASUS 90NR0NJ7-M001J0",
            ),
        ]
        identifiers = [
            {
                "product_id": 10,
                "identifier_type": "sku",
                "value": "G614PR-RV027",
                "normalized_value": "G614PR-RV027",
                "store": "regard",
                "external_id": "749493",
            },
            {
                "product_id": 10,
                "identifier_type": "alternative_part_number",
                "value": "90NR0NJ7-M001J0",
                "normalized_value": "90NR0NJ7-M001J0",
                "store": "regard",
                "external_id": "749493",
            },
            {
                "product_id": 20,
                "identifier_type": "sku",
                "value": "90NR0NJ7-M001J0",
                "normalized_value": "90NR0NJ7-M001J0",
                "store": "andpro",
                "external_id": "239106",
            },
            {
                "product_id": 20,
                "identifier_type": "manufacturer_part_number",
                "value": "90NR0NJ7-M001J0",
                "normalized_value": "90NR0NJ7-M001J0",
                "store": "andpro",
                "external_id": "239106",
            },
        ]
        specs = {
            ("regard", "749493"): _identity(
                store="regard",
                external_id="749493",
                sku="G614PR-RV027",
                alternative_part_numbers=["90NR0NJ7-M001J0"],
            ),
            ("andpro", "239106"): _identity(
                store="andpro",
                external_id="239106",
                sku="90NR0NJ7-M001J0",
                manufacturer_part_number="90NR0NJ7-M001J0",
            ),
        }
        result = match_products(products, identifiers, specs_by_key=specs)
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.match_method, "strong_identifier")
        self.assertEqual(match.matched_identifier, "90NR0NJ7-M001J0")
        self.assertEqual(result.stats["sku_matches"], 0)
        self.assertEqual(result.stats["strong_identifier_matches"], 1)

    def test_alt_pn_equals_other_store_sku(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="AAA-1", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="BBB-2", price=110, product_id=2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "BBB-2",
                "normalized_value": "BBB-2",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "BBB-2",
                "normalized_value": "BBB-2",
            },
        ]
        result = match_products(products, identifiers)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].match_method, "strong_identifier")
        self.assertEqual(result.matches[0].matched_identifier, "BBB-2")

    def test_different_identifiers_no_match(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="G614PR-RV027", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="90NR0NJ7-M001J0", price=110, product_id=2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "sku",
                "value": "G614PR-RV027",
                "normalized_value": "G614PR-RV027",
            },
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "90NR0NJ7-OTHER",
                "normalized_value": "90NR0NJ7-OTHER",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "90NR0NJ7-M001J0",
                "normalized_value": "90NR0NJ7-M001J0",
            },
            {
                "product_id": 2,
                "identifier_type": "manufacturer_part_number",
                "value": "90NR0NJ7-M001J0",
                "normalized_value": "90NR0NJ7-M001J0",
            },
        ]
        result = match_products(products, identifiers)
        self.assertEqual(len(result.matches), 0)
        self.assertEqual(len(result.unmatched), 2)

    def test_similar_names_without_identifier_no_match(self) -> None:
        result = match_products(
            [
                _row(
                    store="regard",
                    external_id="1",
                    sku="G614PR-RV027",
                    price=100,
                    product_id=1,
                    name="ASUS ROG Strix G16 RTX 5070 Ti",
                ),
                _row(
                    store="andpro",
                    external_id="2",
                    sku="90NR0NJ7-M001J0",
                    price=110,
                    product_id=2,
                    name="ASUS ROG Strix G16 RTX 5070 Ti",
                ),
            ],
            identifiers=[
                {
                    "product_id": 1,
                    "identifier_type": "sku",
                    "value": "G614PR-RV027",
                    "normalized_value": "G614PR-RV027",
                },
                {
                    "product_id": 2,
                    "identifier_type": "sku",
                    "value": "90NR0NJ7-M001J0",
                    "normalized_value": "90NR0NJ7-M001J0",
                },
            ],
        )
        self.assertEqual(len(result.matches), 0)

    def test_identifier_on_multiple_products_ambiguous(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="A1", price=100, product_id=1),
            _row(store="regard", external_id="2", sku="A2", price=101, product_id=2),
            _row(store="andpro", external_id="3", sku="SHARED", price=110, product_id=3),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "SHARED",
                "normalized_value": "SHARED",
            },
            {
                "product_id": 2,
                "identifier_type": "alternative_part_number",
                "value": "SHARED",
                "normalized_value": "SHARED",
            },
            {
                "product_id": 3,
                "identifier_type": "sku",
                "value": "SHARED",
                "normalized_value": "SHARED",
            },
        ]
        result = match_products(products, identifiers)
        self.assertEqual(len(result.matches), 0)
        self.assertEqual(result.stats["ambiguous"], 1)
        self.assertEqual(result.ambiguous[0]["status"], "AMBIGUOUS")

    def test_one_product_two_counterparts_ambiguous(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="R1", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="A1", price=110, product_id=2),
            _row(store="andpro", external_id="3", sku="A2", price=120, product_id=3),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "A1",
                "normalized_value": "A1",
            },
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "A2",
                "normalized_value": "A2",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "A1",
                "normalized_value": "A1",
            },
            {
                "product_id": 3,
                "identifier_type": "sku",
                "value": "A2",
                "normalized_value": "A2",
            },
        ]
        result = match_products(products, identifiers)
        self.assertEqual(len(result.matches), 0)
        self.assertGreaterEqual(result.stats["ambiguous"], 1)

    def test_strong_id_same_config_confirmed(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="R-SKU", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="LINK", price=110, product_id=2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "LINK",
                "normalized_value": "LINK",
            },
            {
                "product_id": 2,
                "identifier_type": "manufacturer_part_number",
                "value": "LINK",
                "normalized_value": "LINK",
            },
        ]
        specs = {
            ("regard", "1"): _identity(store="regard", external_id="1", sku="R-SKU"),
            ("andpro", "2"): _identity(
                store="andpro",
                external_id="2",
                sku="LINK",
                manufacturer_part_number="LINK",
            ),
        }
        result = match_products(products, identifiers, specs_by_key=specs)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].match_method, "strong_identifier")
        self.assertEqual(result.stats["conflicts"], 0)

    def test_strong_id_different_gpu_conflict(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="R-SKU", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="LINK", price=110, product_id=2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "LINK",
                "normalized_value": "LINK",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "LINK",
                "normalized_value": "LINK",
            },
        ]
        specs = {
            ("regard", "1"): _identity(
                store="regard",
                external_id="1",
                sku="R-SKU",
                gpu="RTX 5070 TI LAPTOP",
            ),
            ("andpro", "2"): _identity(
                store="andpro",
                external_id="2",
                sku="LINK",
                gpu="RTX 5080 LAPTOP",
            ),
        }
        result = match_products(products, identifiers, specs_by_key=specs)
        self.assertEqual(len(result.matches), 0)
        self.assertEqual(result.stats["conflicts"], 1)
        self.assertEqual(result.conflicts[0]["status"], "CONFLICT")
        self.assertIn("gpu", result.conflicts[0]["different_fields"])

    def test_strong_id_different_ram_conflict(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="R-SKU", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="LINK", price=110, product_id=2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "LINK",
                "normalized_value": "LINK",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "LINK",
                "normalized_value": "LINK",
            },
        ]
        specs = {
            ("regard", "1"): _identity(
                store="regard", external_id="1", sku="R-SKU", ram_gb=16
            ),
            ("andpro", "2"): _identity(
                store="andpro", external_id="2", sku="LINK", ram_gb=32
            ),
        }
        result = match_products(products, identifiers, specs_by_key=specs)
        self.assertEqual(len(result.matches), 0)
        self.assertEqual(result.stats["conflicts"], 1)
        self.assertIn("ram_gb", result.conflicts[0]["different_fields"])

    def test_missing_config_field_not_conflict(self) -> None:
        products = [
            _row(store="regard", external_id="1", sku="R-SKU", price=100, product_id=1),
            _row(store="andpro", external_id="2", sku="LINK", price=110, product_id=2),
        ]
        identifiers = [
            {
                "product_id": 1,
                "identifier_type": "alternative_part_number",
                "value": "LINK",
                "normalized_value": "LINK",
            },
            {
                "product_id": 2,
                "identifier_type": "sku",
                "value": "LINK",
                "normalized_value": "LINK",
            },
        ]
        left = _identity(store="regard", external_id="1", sku="R-SKU")
        right = _identity(store="andpro", external_id="2", sku="LINK")
        right.screen_refresh_hz = None
        left.screen_refresh_hz = 165
        # Explicitly clear a conflict field on one side.
        right.gpu_vram_gb = None
        specs = {("regard", "1"): left, ("andpro", "2"): right}
        self.assertEqual(find_config_conflicts(left, right), {})
        result = match_products(products, identifiers, specs_by_key=specs)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.stats["conflicts"], 0)

    def test_case_and_spaces_normalize_to_match(self) -> None:
        result = match_products(
            [
                _row(store="regard", external_id="1", sku="dxhg4kzcc4sd", price=100),
                _row(store="andpro", external_id="2", sku=" DXHG4KZCC4SD ", price=120),
            ]
        )
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].normalized_sku, "DXHG4KZCC4SD")

    def test_price_none_does_not_crash(self) -> None:
        result = match_products(
            [
                _row(store="regard", external_id="1", sku="ABC-1", price=None),
                _row(store="andpro", external_id="2", sku="ABC-1", price=1000),
            ]
        )
        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        self.assertEqual(match.min_price, 1000)
        self.assertEqual(match.match_method, "sku")

    def test_unavailable_cheap_is_not_cheapest_available(self) -> None:
        result = match_products(
            [
                _row(
                    store="regard",
                    external_id="1",
                    sku="SKU-1",
                    price=100,
                    available=False,
                ),
                _row(
                    store="andpro",
                    external_id="2",
                    sku="SKU-1",
                    price=200,
                    available=True,
                ),
            ]
        )
        match = result.matches[0]
        self.assertEqual(match.cheapest_product.store, "regard")
        self.assertEqual(match.cheapest_available.store, "andpro")


class IdentifierStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmpdir.name) / "ids.db"
        self.specs_path = Path(self._tmpdir.name) / "specs.json"
        self.assertNotEqual(
            self.db_path.resolve(),
            (Path("data") / "laptop_monitor.db").resolve(),
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_repeat_sync_does_not_duplicate(self) -> None:
        save_products(
            [
                Product(
                    store="regard",
                    external_id="1",
                    url="https://example.test/1",
                    name="Regard Laptop",
                    sku="G614PR-RV027",
                    price=100,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                ),
                Product(
                    store="andpro",
                    external_id="2",
                    url="https://example.test/2",
                    name="ANDPRO Laptop",
                    sku="90NR0NJ7-M001J0",
                    price=110,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                ),
            ],
            self.db_path,
        )

        # Manual identifiers without live enrich.
        with open_db(self.db_path) as conn:
            init_db(conn)
            products = conn.execute("SELECT id, store, sku FROM products").fetchall()
            for row in products:
                upsert_identifier_rows(
                    conn,
                    int(row["id"]),
                    [("sku", row["sku"], "products.sku")],
                )
                if row["store"] == "regard":
                    upsert_identifier_rows(
                        conn,
                        int(row["id"]),
                        [
                            (
                                "alternative_part_number",
                                "90NR0NJ7-M001J0",
                                "regard.api.alternative_pn",
                            )
                        ],
                    )
                else:
                    upsert_identifier_rows(
                        conn,
                        int(row["id"]),
                        [
                            (
                                "manufacturer_part_number",
                                "90NR0NJ7-M001J0",
                                "andpro.card.mpn",
                            )
                        ],
                    )
            first_count = count_product_identifiers(conn)

        # Second sync without enrich should not add duplicates.
        stats = sync_product_identifiers(
            self.db_path,
            enrich=False,
            specs_path=self.specs_path,
        )
        with open_db(self.db_path) as conn:
            second_count = count_product_identifiers(conn)
            rows = get_product_identifiers(conn)

        self.assertEqual(first_count, second_count)
        self.assertEqual(stats["identifiers_after"], second_count)
        # Unique constraint respects product/type/value
        keys = {
            (r["product_id"], r["identifier_type"], r["normalized_value"])
            for r in rows
        }
        self.assertEqual(len(keys), len(rows))

    def test_identifiers_stored_per_store(self) -> None:
        save_products(
            [
                Product(
                    store="regard",
                    external_id="10",
                    url="https://example.test/10",
                    name="R",
                    sku="G614PR-RV027",
                    price=1,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                ),
                Product(
                    store="andpro",
                    external_id="20",
                    url="https://example.test/20",
                    name="A",
                    sku="90NR0NJ7-M001J0",
                    price=2,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                ),
            ],
            self.db_path,
        )
        with open_db(self.db_path) as conn:
            init_db(conn)
            regard = conn.execute(
                "SELECT id FROM products WHERE store='regard'"
            ).fetchone()
            andpro = conn.execute(
                "SELECT id FROM products WHERE store='andpro'"
            ).fetchone()
            upsert_product_identifier(
                conn,
                product_id=int(regard["id"]),
                identifier_type="alternative_part_number",
                value="90NR0NJ7-M001J0",
                normalized_value="90NR0NJ7-M001J0",
                source="regard.api.alternative_pn",
            )
            upsert_product_identifier(
                conn,
                product_id=int(andpro["id"]),
                identifier_type="manufacturer_part_number",
                value="90NR0NJ7-M001J0",
                normalized_value="90NR0NJ7-M001J0",
                source="andpro.card.mpn",
            )
            rows = get_product_identifiers(conn)

        by_store = {}
        for row in rows:
            by_store.setdefault(row["store"], []).append(row["identifier_type"])
        self.assertIn("alternative_part_number", by_store["regard"])
        self.assertIn("manufacturer_part_number", by_store["andpro"])

    def test_extractors(self) -> None:
        regard = _identity(
            store="regard",
            external_id="1",
            sku="G614PR-RV027",
            alternative_part_numbers=["90NR0NJ7-M001J0"],
        )
        rows = identifiers_from_identity(regard)
        types = {t for t, _, _ in rows}
        self.assertIn("sku", types)
        self.assertIn("alternative_part_number", types)
        self.assertNotIn("manufacturer_part_number", types)

        andpro = _identity(
            store="andpro",
            external_id="2",
            sku="90NR0NJ7-M001J0",
            manufacturer_part_number="90NR0NJ7-M001J0",
        )
        rows = identifiers_from_identity(andpro)
        types = {t for t, _, _ in rows}
        self.assertIn("sku", types)
        self.assertIn("manufacturer_part_number", types)

        base = identifiers_from_product_row({"sku": "ABC"})
        self.assertEqual(base[0][0], "sku")
        self.assertEqual(normalize_identifier(" ab c "), "ABC")


if __name__ == "__main__":
    unittest.main()
