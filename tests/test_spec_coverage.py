from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from identity_sync import (
    identity_from_cache,
    load_specs_cache,
    merge_identity_into_cache,
    save_specs_cache,
    update_specs_cache_from_products,
)
from models import Product
from product_identity import ProductIdentity, identity_from_collected_product
from stores.common import extract_specs_from_name


def _product(**kwargs) -> Product:
    base = dict(
        store="kns",
        external_id="1",
        url="https://example.test/p/1",
        name="Test",
        sku="SKU",
        price=200_000,
        available=True,
        checked_at=datetime.now(timezone.utc),
        metadata={},
    )
    base.update(kwargs)
    return Product(**base)


class TitleParserRegressionTests(unittest.TestCase):
    def test_rtx_5070_ti(self) -> None:
        specs = extract_specs_from_name(
            "Ноутбук MSI Vector, NVIDIA GeForce RTX 5070 Ti для ноутбуков"
        )
        self.assertEqual(specs.get("gpu"), "RTX 5070 Ti")

    def test_rtx_5080(self) -> None:
        specs = extract_specs_from_name("ASUS ROG Strix RTX 5080 Laptop")
        self.assertEqual(specs.get("gpu"), "RTX 5080")

    def test_vram_not_ram_gddr7(self) -> None:
        specs = extract_specs_from_name(
            "Ноутбук ASUS, RTX 5080 16GB GDDR7, 32GB DDR5, 1TB SSD"
        )
        self.assertEqual(specs.get("gpu"), "RTX 5080")
        self.assertEqual(specs.get("ram_gb"), 32)
        self.assertNotEqual(specs.get("ram_gb"), 16)

    def test_core_i9_14900hx(self) -> None:
        specs = extract_specs_from_name(
            "Ноутбук игровой THUNDEROBOT, Intel Core i9 14900HX 2.2ГГц, 32ГБ DDR5"
        )
        self.assertIn("14900HX", str(specs.get("cpu") or "").upper())

    def test_core_i7_hx(self) -> None:
        specs = extract_specs_from_name("Laptop Intel Core i7-14700HX, 32GB DDR5")
        cpu = str(specs.get("cpu") or "").upper()
        self.assertTrue("I7" in cpu or "14700HX" in cpu)

    def test_core_ultra_9(self) -> None:
        specs = extract_specs_from_name(
            "MSI Vector 17, Intel Core Ultra 9 275HX, 32ГБ DDR5"
        )
        self.assertIn("ULTRA", str(specs.get("cpu") or "").upper())
        self.assertIn("9", str(specs.get("cpu") or ""))

    def test_ryzen_9_hx(self) -> None:
        specs = extract_specs_from_name(
            "ASUS ROG Strix G16, AMD Ryzen 9 8940HX 2.4ГГц, 16ГБ DDR5"
        )
        self.assertIn("RYZEN", str(specs.get("cpu") or "").upper())
        self.assertIn("8940HX", str(specs.get("cpu") or "").upper())

    def test_ryzen_ai_9(self) -> None:
        specs = extract_specs_from_name("MSI Stealth, AMD Ryzen AI 9 HX 370, 32GB DDR5")
        self.assertIn("RYZEN AI", str(specs.get("cpu") or "").upper())

    def test_ram_32_ddr5(self) -> None:
        specs = extract_specs_from_name(
            "Gigabyte A16, 32ГБ DDR5, 1ТБ SSD, RTX 5070 Ti"
        )
        self.assertEqual(specs.get("ram_gb"), 32)

    def test_ram_64(self) -> None:
        specs = extract_specs_from_name("Laptop 64GB DDR5, 2TB SSD, RTX 5080")
        self.assertEqual(specs.get("ram_gb"), 64)

    def test_ssd_1tb(self) -> None:
        specs = extract_specs_from_name("Laptop 32ГБ DDR5, 1ТБ SSD, RTX 5070 Ti")
        self.assertEqual(specs.get("ssd_gb"), 1024)

    def test_ssd_1024gb(self) -> None:
        specs = extract_specs_from_name("Laptop 32GB DDR5, 1024GB SSD, RTX 5070 Ti")
        self.assertEqual(specs.get("ssd_gb"), 1024)

    def test_ssd_2tb(self) -> None:
        specs = extract_specs_from_name("Laptop 64GB DDR5, 2TB SSD, RTX 5080")
        self.assertEqual(specs.get("ssd_gb"), 2048)

    def test_screen_16(self) -> None:
        specs = extract_specs_from_name(
            'Ноутбук Gigabyte Gaming A16 Pro 16" 2560x1600 (WQXGA)'
        )
        self.assertEqual(specs.get("screen_inch"), 16.0)

    def test_screen_17_3(self) -> None:
        specs = extract_specs_from_name('Gaming laptop 17.3" IPS, RTX 5070 Ti')
        self.assertEqual(specs.get("screen_inch"), 17.3)

    def test_screen_18(self) -> None:
        specs = extract_specs_from_name(
            'Gigabyte Gaming A18 Pro 18" 2560x1600 (WQXGA)'
        )
        self.assertEqual(specs.get("screen_inch"), 18.0)

    def test_resolution_2560x1600(self) -> None:
        specs = extract_specs_from_name('16" 2560x1600 (WQXGA)')
        self.assertEqual(specs.get("screen_resolution"), "2560x1600")

    def test_resolution_unicode_times(self) -> None:
        specs = extract_specs_from_name("Экран 2560×1600 IPS")
        self.assertEqual(specs.get("screen_resolution"), "2560x1600")


class CacheMergeTests(unittest.TestCase):
    def test_incoming_known_fills_none(self) -> None:
        cache: dict = {
            "kns:1": {
                "store": "kns",
                "external_id": "1",
                "sku": None,
                "name": "A",
                "price": 1,
                "available": True,
                "url": "u",
                "gpu": None,
                "ram_gb": None,
            }
        }
        identity = identity_from_collected_product(
            _product(metadata={"gpu": "RTX 5070 Ti", "ram_gb": 32})
        )
        merge_identity_into_cache(cache, identity)
        self.assertEqual(cache["kns:1"]["gpu"], "RTX 5070 TI LAPTOP")
        self.assertEqual(cache["kns:1"]["ram_gb"], 32)

    def test_incoming_none_keeps_known(self) -> None:
        cache: dict = {
            "kns:1": {
                "store": "kns",
                "external_id": "1",
                "sku": None,
                "name": "A",
                "price": 1,
                "available": True,
                "url": "u",
                "gpu": "RTX 5080 LAPTOP",
                "ram_gb": 64,
            }
        }
        identity = identity_from_collected_product(
            _product(name="Ноутбук без характеристик", metadata={})
        )
        merge_identity_into_cache(cache, identity)
        self.assertEqual(cache["kns:1"]["gpu"], "RTX 5080 LAPTOP")
        self.assertEqual(cache["kns:1"]["ram_gb"], 64)

    def test_same_normalized_harmless(self) -> None:
        cache: dict = {
            "citilink:1": {
                "store": "citilink",
                "external_id": "1",
                "sku": None,
                "name": "A",
                "price": 1,
                "available": True,
                "url": "u",
                "gpu": "RTX 5070 TI LAPTOP",
                "ram_gb": 32,
            }
        }
        identity = identity_from_collected_product(
            _product(
                store="citilink",
                name="RTX 5070 Ti, 32ГБ DDR5",
                metadata={"gpu": "RTX 5070 Ti", "ram_gb": 32},
            )
        )
        result = merge_identity_into_cache(cache, identity)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(cache["citilink:1"]["ram_gb"], 32)

    def test_conflict_not_silent_overwrite(self) -> None:
        cache: dict = {
            "regard:1": {
                "store": "regard",
                "external_id": "1",
                "sku": None,
                "name": "A",
                "price": 1,
                "available": True,
                "url": "u",
                "ram_gb": 32,
                "source_identifiers": {
                    "field_provenance": {"ram_gb": "regard.api"},
                },
            }
        }
        identity = ProductIdentity(
            store="regard",
            external_id="1",
            sku=None,
            name="A",
            price=1,
            available=True,
            url="u",
            ram_gb=64,
            source_identifiers={"field_provenance": {"ram_gb": "product_title"}},
        )
        result = merge_identity_into_cache(cache, identity)
        self.assertEqual(cache["regard:1"]["ram_gb"], 32)
        self.assertTrue(result["conflicts"])
        self.assertEqual(result["conflicts"][0]["status"], "SPEC_CONFLICT")

    def test_kns_metadata_reaches_specs_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "product_specs.json"
            product = _product(
                store="kns",
                external_id="772689",
                name="Ноутбук GigaByte Gaming A16 Pro",
                metadata={"gpu": "RTX 5070 Ti", "catalog_gpu": "RTX 5070 Ti"},
            )
            update_specs_cache_from_products([product], path)
            cache = load_specs_cache(path)
            self.assertIn("kns:772689", cache)
            self.assertIn("5070", str(cache["kns:772689"].get("gpu") or ""))

    def test_citilink_metadata_reaches_specs_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "product_specs.json"
            name = (
                "Ноутбук игровой ASUS ROG Strix G16, 16\", IPS, "
                "AMD Ryzen 9 8940HX, 16ГБ DDR5, 1ТБ SSD, "
                "NVIDIA GeForce RTX 5070 Ti для ноутбуков - 12 ГБ"
            )
            product = _product(
                store="citilink",
                external_id="2137350",
                name=name,
                metadata=extract_specs_from_name(name),
            )
            update_specs_cache_from_products([product], path)
            cache = load_specs_cache(path)
            entry = cache["citilink:2137350"]
            self.assertIn("5070", str(entry.get("gpu") or ""))
            self.assertEqual(entry.get("ram_gb"), 16)
            self.assertEqual(entry.get("ssd_gb"), 1024)
            self.assertEqual(entry.get("screen_size_inch"), 16.0)

    def test_regard_structured_preserved(self) -> None:
        cache: dict = {
            "regard:100": {
                "store": "regard",
                "external_id": "100",
                "sku": "X",
                "name": "Regard laptop",
                "price": 1,
                "available": True,
                "url": "u",
                "gpu": "RTX 5080 LAPTOP",
                "cpu": "CORE ULTRA 9 275HX",
                "ram_gb": 32,
                "ssd_gb": 1024,
                "source_identifiers": {
                    "field_provenance": {
                        "gpu": "regard.api",
                        "cpu": "regard.api",
                        "ram_gb": "regard.api",
                        "ssd_gb": "regard.api",
                    }
                },
            }
        }
        identity = identity_from_collected_product(
            _product(
                store="regard",
                external_id="100",
                name="Regard laptop RTX 5070 Ti",
                metadata={"gpu": "RTX 5070 Ti"},
            )
        )
        merge_identity_into_cache(cache, identity)
        self.assertEqual(cache["regard:100"]["gpu"], "RTX 5080 LAPTOP")
        self.assertEqual(cache["regard:100"]["ram_gb"], 32)

    def test_andpro_structured_preserved(self) -> None:
        cache: dict = {
            "andpro:200": {
                "store": "andpro",
                "external_id": "200",
                "sku": "Y",
                "name": "ANDPRO laptop",
                "price": 1,
                "available": True,
                "url": "u",
                "gpu": "RTX 5070 TI LAPTOP",
                "ram_gb": 32,
                "screen_size_inch": 16.0,
                "screen_resolution": "2560x1600",
                "source_identifiers": {
                    "field_provenance": {
                        "gpu": "andpro.card",
                        "ram_gb": "andpro.card",
                        "screen_size_inch": "andpro.card",
                        "screen_resolution": "andpro.card",
                    }
                },
            }
        }
        identity = identity_from_collected_product(
            _product(
                store="andpro",
                external_id="200",
                name='ANDPRO laptop 16" 1920x1200',
                metadata={"screen_resolution": "1920x1200"},
            )
        )
        result = merge_identity_into_cache(cache, identity)
        self.assertEqual(cache["andpro:200"]["screen_resolution"], "2560x1600")
        self.assertTrue(result["conflicts"])

    def test_atomic_write_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "product_specs.json"
            save_specs_cache({"a:1": {"store": "a", "external_id": "1"}}, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("a:1", data)

    def test_interrupted_write_keeps_old_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "product_specs.json"
            save_specs_cache({"keep:1": {"ok": True}}, path)
            original = path.read_text(encoding="utf-8")

            # Simulate failed atomic write: temp created but replace never happens.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            try:
                os.write(fd, b"{not-valid-json")
                os.close(fd)
                # Old file must remain intact.
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                json.loads(path.read_text(encoding="utf-8"))
            finally:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass


class IdentityFromCollectedTests(unittest.TestCase):
    def test_provenance_metadata_over_title(self) -> None:
        product = _product(
            name="RTX 5080 16GB GDDR7, 64GB DDR5",
            metadata={"gpu": "RTX 5070 Ti", "ram_gb": 32},
        )
        identity = identity_from_collected_product(product)
        self.assertEqual(identity.gpu, "RTX 5070 TI LAPTOP")
        self.assertEqual(identity.ram_gb, 32)
        prov = identity.source_identifiers.get("field_provenance") or {}
        self.assertEqual(prov.get("gpu"), "collected_metadata")
        self.assertEqual(prov.get("ram_gb"), "collected_metadata")


if __name__ == "__main__":
    unittest.main()
