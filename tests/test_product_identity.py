from __future__ import annotations

import unittest

from product_identity import (
    ProductIdentity,
    compare_identities,
    normalize_cpu,
    normalize_gpu,
    normalize_ram_gb,
    normalize_resolution,
    normalize_ssd_gb,
    normalize_vram_gb,
)


def _identity(**kwargs) -> ProductIdentity:
    base = dict(
        store="regard",
        external_id="1",
        sku="SKU-A",
        name="Test",
        price=100,
        available=True,
        url="https://example.test/a",
        brand="ASUS",
        model="G614PR-RV027",
        manufacturer_part_number="G614PR-RV027",
        alternative_part_numbers=[],
        cpu="RYZEN 9 8940HX",
        gpu="RTX 5070 TI LAPTOP",
        gpu_vram_gb=12,
        ram_gb=16,
        ssd_gb=1024,
        screen_size_inch=16.0,
        screen_resolution="1920x1200",
        screen_refresh_hz=165,
        os="NO_OS",
    )
    base.update(kwargs)
    return ProductIdentity(**base)


class NormalizeTests(unittest.TestCase):
    def test_ram(self) -> None:
        self.assertEqual(normalize_ram_gb("32 GB"), 32)
        self.assertEqual(normalize_ram_gb("32ГБ"), 32)
        self.assertEqual(normalize_ram_gb("16 ГБ DDR5"), 16)

    def test_ssd(self) -> None:
        self.assertEqual(normalize_ssd_gb("1 TB"), 1024)
        self.assertEqual(normalize_ssd_gb("1 ТБ"), 1024)
        self.assertEqual(normalize_ssd_gb("512 ГБ"), 512)

    def test_resolution(self) -> None:
        self.assertEqual(normalize_resolution("2560 x 1600"), "2560x1600")
        self.assertEqual(normalize_resolution("2560×1600 (WQXGA)"), "2560x1600")
        self.assertEqual(normalize_resolution("1920x1200 (16:10)"), "1920x1200")

    def test_gpu(self) -> None:
        self.assertEqual(
            normalize_gpu("GeForce RTX 5070 Ti для ноутбука"),
            "RTX 5070 TI LAPTOP",
        )
        self.assertEqual(normalize_gpu("RTX 5070 Ti Mobile"), "RTX 5070 TI LAPTOP")
        self.assertEqual(normalize_gpu("nVidia GeForce RTX 5080 Mobile"), "RTX 5080 LAPTOP")

    def test_vram(self) -> None:
        self.assertEqual(normalize_vram_gb("12GB"), 12)
        self.assertEqual(normalize_vram_gb(12288), 12)

    def test_cpu(self) -> None:
        self.assertEqual(
            normalize_cpu("AMD Ryzen 9 (8000 Series) 8940HX"),
            "RYZEN 9 8940HX",
        )
        self.assertEqual(normalize_cpu("AMD Ryzen 9 8940HX"), "RYZEN 9 8940HX")


class CompareIdentityTests(unittest.TestCase):
    def test_same_strict_config_is_candidate(self) -> None:
        left = _identity(
            store="regard",
            external_id="1",
            sku="AAA-111",
            manufacturer_part_number="AAA-111",
            alternative_part_numbers=[],
            model="FAMILY MODEL AAA-111",
        )
        right = _identity(
            store="andpro",
            external_id="2",
            sku="BBB-222",
            manufacturer_part_number="BBB-222",
            alternative_part_numbers=[],
            model="FAMILY MODEL BBB-222",
        )
        cmp = compare_identities(left, right)
        # Models differ and no shared code => REJECTED on model, not candidate.
        # Build a true candidate with identical model string and no shared ids.
        left = _identity(
            store="regard",
            external_id="1",
            sku="NOISE-LEFT",
            manufacturer_part_number="NOISE-LEFT",
            alternative_part_numbers=[],
            model="COMMON FAMILY X",
        )
        right = _identity(
            store="andpro",
            external_id="2",
            sku="NOISE-RIGHT",
            manufacturer_part_number="NOISE-RIGHT",
            alternative_part_numbers=[],
            model="COMMON FAMILY X",
        )
        cmp = compare_identities(left, right)
        self.assertEqual(cmp.status, "CANDIDATE")

    def test_different_cpu_reject(self) -> None:
        left = _identity(cpu="RYZEN 9 8940HX")
        right = _identity(
            store="andpro",
            external_id="2",
            sku="X",
            manufacturer_part_number="X",
            model="Y",
            cpu="RYZEN 9 9955HX",
        )
        self.assertEqual(compare_identities(left, right).status, "REJECTED")

    def test_different_ram_reject(self) -> None:
        left = _identity(ram_gb=16)
        right = _identity(
            store="andpro",
            external_id="2",
            sku="X",
            manufacturer_part_number="X",
            model="Y",
            ram_gb=32,
        )
        self.assertEqual(compare_identities(left, right).status, "REJECTED")

    def test_different_ssd_reject(self) -> None:
        left = _identity(ssd_gb=1024)
        right = _identity(
            store="andpro",
            external_id="2",
            sku="X",
            manufacturer_part_number="X",
            model="Y",
            ssd_gb=2048,
        )
        self.assertEqual(compare_identities(left, right).status, "REJECTED")

    def test_different_screen_reject(self) -> None:
        left = _identity(screen_resolution="1920x1200")
        right = _identity(
            store="andpro",
            external_id="2",
            sku="X",
            manufacturer_part_number="X",
            model="Y",
            screen_resolution="2560x1600",
        )
        self.assertEqual(compare_identities(left, right).status, "REJECTED")

    def test_different_gpu_reject(self) -> None:
        left = _identity(gpu="RTX 5070 TI LAPTOP")
        right = _identity(
            store="andpro",
            external_id="2",
            sku="X",
            manufacturer_part_number="X",
            model="Y",
            gpu="RTX 5080 LAPTOP",
        )
        self.assertEqual(compare_identities(left, right).status, "REJECTED")

    def test_missing_field_not_candidate(self) -> None:
        left = _identity(ram_gb=None)
        right = _identity(
            store="andpro",
            external_id="2",
            sku="X",
            manufacturer_part_number="X",
            model="Y",
            ram_gb=16,
        )
        cmp = compare_identities(left, right)
        self.assertEqual(cmp.status, "REJECTED")
        self.assertIn("ram_gb", cmp.missing_fields)

    def test_shared_ean_strong_match(self) -> None:
        left = _identity(ean="1234567890123", gtin="1234567890123")
        right = _identity(
            store="andpro",
            external_id="2",
            sku="OTHER",
            manufacturer_part_number="OTHER",
            model="OTHER MODEL",
            ean="1234567890123",
            gtin="1234567890123",
            cpu="DIFFERENT CPU",  # even with different cpu, strong id wins
        )
        cmp = compare_identities(left, right)
        self.assertEqual(cmp.status, "CONFIRMED")
        self.assertIn("EAN/GTIN", cmp.reason)

    def test_alternative_part_number_strong_match(self) -> None:
        left = _identity(
            sku="G614PR-RV027",
            manufacturer_part_number="G614PR-RV027",
            alternative_part_numbers=["90NR0NJ7-M001J0"],
        )
        right = _identity(
            store="andpro",
            external_id="2",
            sku="90NR0NJ7-M001J0",
            manufacturer_part_number="90NR0NJ7-M001J0",
            model="ROG STRIX G16 G614PR-RV027",
        )
        cmp = compare_identities(left, right)
        self.assertEqual(cmp.status, "CONFIRMED")
        self.assertIn("90NR0NJ7-M001J0", cmp.reason)


if __name__ == "__main__":
    unittest.main()
