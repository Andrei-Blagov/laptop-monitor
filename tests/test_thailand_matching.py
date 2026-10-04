from __future__ import annotations

import unittest
from datetime import datetime, timezone

from deal_ranking import RankedDeal
from thailand.matching import (
    configs_equivalent,
    group_thai_offers,
    match_russian_to_thai,
    same_family,
)
from thailand.models import ThailandOffer
from thailand.specs_parse import is_purchasable_for_best, strip_regional_sku_suffix


def _thai(**kwargs) -> ThailandOffer:
    base = dict(
        store="banana",
        external_id="t1",
        name="MSI Vector 16 HX AI",
        url="https://www.bnn.in.th/t1",
        price_thb=62990,
        available=True,
        collected_at=datetime.now(timezone.utc),
        sku="83F500NWTA",
        manufacturer_part_number="83F500NWTA",
        brand="Msi",
        gpu="RTX 5070 Ti",
        cpu="Intel Core Ultra 9 275HX",
        ram_gb=32,
        ssd_gb=1024,
        screen_size_inch=16.0,
        availability_status="in_stock",
    )
    base.update(kwargs)
    return ThailandOffer(**base)


def _ru(**kwargs) -> RankedDeal:
    base = dict(
        score=79,
        reasons=[],
        offer={"sku": "83F500NWTA", "store": "kns", "external_id": "r1"},
        cluster_name="MSI Vector 16 HX AI",
        gpu="RTX 5070 Ti",
        store="kns",
        price=229990,
        cpu="Intel Core Ultra 9 275HX",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=16.0,
        confidence=100,
        historical_min=224990,
    )
    base.update(kwargs)
    return RankedDeal(**base)


class MatchingTests(unittest.TestCase):
    def test_exact_mpn(self) -> None:
        matches = match_russian_to_thai(_ru(), [_thai()])
        self.assertTrue(any(m.level == "EXACT" for m in matches))

    def test_regional_suffix_not_exact(self) -> None:
        base, suf = strip_regional_sku_suffix("83F500NWTA")
        self.assertEqual(suf, "TA")
        ru = _ru(offer={"sku": "83F500NW", "store": "kns"})
        # Force different regional forms with same base
        thai = _thai(sku="83F500NWTH", manufacturer_part_number="83F500NWTH")
        # If bases equal after strip and suffixes differ → SAME_FAMILY not EXACT
        matches = match_russian_to_thai(
            _ru(offer={"sku": "83F500NWTA", "store": "kns"}),
            [_thai(sku="83F500NWTH", manufacturer_part_number="83F500NWTH")],
        )
        levels = {m.level for m in matches}
        self.assertNotIn("EXACT", levels)
        self.assertTrue(levels & {"SAME_FAMILY", "EQUIVALENT", "ALTERNATIVE"})

    def test_same_family_equivalent_config(self) -> None:
        ru = _ru(offer={"sku": "OTHERSKU", "store": "kns"})
        thai = _thai(sku="OTHER2", manufacturer_part_number="OTHER2")
        self.assertTrue(same_family(ru, thai))
        eq, diffs = configs_equivalent(ru, thai)
        self.assertTrue(eq)
        matches = match_russian_to_thai(ru, [thai])
        self.assertTrue(any(m.level in {"EQUIVALENT", "SAME_FAMILY"} for m in matches))

    def test_gpu_mismatch_rejected_for_family(self) -> None:
        ru = _ru()
        thai = _thai(gpu="RTX 5080", name="MSI Vector 16 HX AI 5080")
        self.assertFalse(same_family(ru, thai))

    def test_cpu_mismatch(self) -> None:
        eq, diffs = configs_equivalent(
            _ru(cpu="Intel Core Ultra 9 275HX"),
            _thai(cpu="Intel Core i5-13420H"),
        )
        self.assertFalse(eq)
        self.assertTrue(any("cpu" in d for d in diffs))

    def test_ram_mismatch(self) -> None:
        eq, diffs = configs_equivalent(_ru(ram_gb=32), _thai(ram_gb=64))
        self.assertFalse(eq)
        self.assertTrue(any(d.startswith("ram:") for d in diffs))

    def test_ssd_mismatch(self) -> None:
        eq, diffs = configs_equivalent(_ru(ssd_gb=1024), _thai(ssd_gb=2048))
        self.assertFalse(eq)
        self.assertTrue(any(d.startswith("ssd:") for d in diffs))

    def test_resolution_informational(self) -> None:
        ru = _ru()
        ru.screen_resolution = "2560x1600"
        thai = _thai(screen_resolution="1920x1200")
        eq, diffs = configs_equivalent(ru, thai)
        # Still equivalent on core config; resolution noted
        self.assertTrue(eq)
        self.assertTrue(any(d.startswith("resolution:") for d in diffs))

    def test_no_fuzzy_name_only(self) -> None:
        ru = _ru(
            cluster_name="Completely Different Alienware X16",
            offer={"sku": "ZZZ", "store": "kns"},
            gpu="RTX 5070 Ti",
        )
        thai = _thai(
            name="Lenovo Legion Pro 7",
            sku="AAA",
            manufacturer_part_number="AAA111",
            brand="Lenovo",
        )
        matches = match_russian_to_thai(ru, [thai])
        self.assertFalse(any(m.level == "EXACT" for m in matches))
        self.assertFalse(any(m.level == "EQUIVALENT" for m in matches))

    def test_three_stores_grouped(self) -> None:
        offers = [
            _thai(store="banana", external_id="1", price_thb=62990),
            _thai(store="jib", external_id="2", price_thb=64990),
            _thai(store="advice", external_id="3", price_thb=65990),
        ]
        groups = group_thai_offers(offers)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].best.store, "banana")
        self.assertEqual(groups[0].best.price_thb, 62990)

    def test_cheapest_available(self) -> None:
        offers = [
            _thai(store="banana", external_id="1", price_thb=70000),
            _thai(store="jib", external_id="2", price_thb=65000),
        ]
        g = group_thai_offers(offers)[0]
        self.assertEqual(g.best.price_thb, 65000)

    def test_oos_not_best(self) -> None:
        offers = [
            _thai(
                store="banana",
                external_id="1",
                price_thb=50000,
                available=False,
                availability_status="out_of_stock",
            ),
            _thai(store="jib", external_id="2", price_thb=65000),
        ]
        g = group_thai_offers(offers)[0]
        self.assertEqual(g.best.store, "jib")

    def test_pickup_only_retained(self) -> None:
        o = _thai(availability_status="store_pickup_only", available=True)
        self.assertTrue(is_purchasable_for_best(o.availability_status, o.available))
        g = group_thai_offers([o])[0]
        self.assertIsNotNone(g.best)
        self.assertEqual(g.best.availability_status, "store_pickup_only")


if __name__ == "__main__":
    unittest.main()
