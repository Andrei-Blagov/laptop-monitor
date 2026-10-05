from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.eligibility import offer_user_facing_eligible, sort_cheapest_rows
from thailand.formatting import format_thailand_alternatives_message
from thailand.fx import FxRate
from thailand.grouping import dedupe_user_facing_rows
from thailand.models import ThailandOffer
from thailand.verification import compute_verification


def _fx() -> FxRate:
    return FxRate(
        source="test",
        currency="THB",
        nominal=1,
        official_rate=1.0,
        rub_per_thb=1.0,
        published_date=None,
        fetched_at=datetime.now(timezone.utc),
    )


def _offer(store: str, price: int, *, mpn: str = "G614PR-TS113W", available: bool = True) -> ThailandOffer:
    offer = ThailandOffer(
        store=store,
        external_id=f"{store}-{price}",
        name="ASUS ROG Strix G16 G614PR-TS113W",
        url=f"https://example.test/{store}",
        price_thb=price,
        available=available,
        collected_at=datetime.now(timezone.utc),
        sku=mpn,
        manufacturer_part_number=mpn,
        gpu="RTX 5070 Ti",
        gpu_source="structured_api",
        cpu="AMD Ryzen 9 8940HX",
        ram_gb=16,
        ssd_gb=1024,
        screen_size_inch=16.0,
        availability_status="in_stock" if available else "unknown",
        availability_confirmed=available,
        availability_source="structured_api",
        price_source="structured_api",
        channel="direct",
    )
    return compute_verification(offer)


def _row(offer: ThailandOffer, rub: int) -> dict:
    return {
        "store": offer.store,
        "store_label": {"jib": "JIB", "speedcom": "SpeedCom", "invadeit": "InvadeIT"}.get(offer.store, offer.store),
        "external_id": offer.external_id,
        "name": offer.name,
        "gpu": offer.gpu,
        "cpu": offer.cpu,
        "ram_gb": offer.ram_gb,
        "ssd_gb": offer.ssd_gb,
        "screen_size_inch": offer.screen_size_inch,
        "manufacturer_part_number": offer.manufacturer_part_number,
        "sku": offer.sku,
        "price_thb": offer.price_thb,
        "price_rub": rub,
        "url": offer.url,
        "international_score": 70,
        "international_confidence": 90,
        "marketplace": False,
    }


class CrossStoreTests(unittest.TestCase):
    def test_same_mpn_grouped_and_cheapest_direct_wins(self) -> None:
        rows = [
            _row(_offer("jib", 100000), 250000),
            _row(_offer("speedcom", 90000), 220000),
        ]
        deduped, removed = dedupe_user_facing_rows(rows)
        self.assertEqual(removed, 1)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["store"], "speedcom")
        self.assertEqual(deduped[0]["price_rub"], 220000)

    def test_duplicate_not_repeated_and_alternative_listed(self) -> None:
        rows = [
            _row(_offer("speedcom", 90000), 220000),
            _row(_offer("jib", 100000), 250000),
            _row(_offer("invadeit", 110000), 270000),
        ]
        deduped, removed = dedupe_user_facing_rows(rows)
        self.assertEqual(removed, 2)
        text = format_thailand_alternatives_message(deduped, price_cap={"over_cap_count": 0, "max_tracked_price_rub": 300000})
        self.assertEqual(text.count("G614PR-TS113W"), 1)
        self.assertIn("Другие магазины:", text)
        self.assertIn("JIB", text)
        self.assertLessEqual(len(deduped[0]["alt_channels"]), 2)

    def test_over_cap_and_unknown_availability_excluded(self) -> None:
        priced = _offer("jib", 300001)
        unknown = _offer("jib", 200000, mpn="OTHER-1", available=False)
        unknown.availability_status = "unknown"
        unknown.availability_confirmed = False
        compute_verification(unknown)
        fx = _fx()
        self.assertFalse(offer_user_facing_eligible(priced, fx=fx)[0])
        self.assertFalse(offer_user_facing_eligible(unknown, fx=fx)[0])

    def test_strict_price_sort_preserved(self) -> None:
        rows = [
            _row(_offer("jib", 300000, mpn="A-300000"), 300000),
            _row(_offer("speedcom", 100000, mpn="B-100000"), 100000),
            _row(_offer("invadeit", 299999, mpn="C-299999"), 299999),
        ]
        ordered = sort_cheapest_rows(rows)
        self.assertEqual([r["price_rub"] for r in ordered], [100000, 299999, 300000])


if __name__ == "__main__":
    unittest.main()
