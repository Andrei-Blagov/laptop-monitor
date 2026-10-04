from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.models import FxRate, ThailandOffer
from thailand.scanner import build_thai_top
from thailand.verification import (
    GPU_SOURCE_EXPLICIT_TITLE,
    GPU_SOURCE_SEARCH_QUERY,
    GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
    VERIFIED,
    compute_verification,
    international_confidence,
)


def _fx() -> FxRate:
    return FxRate(
        source="CBR",
        currency="THB",
        nominal=10,
        official_rate=25.0,
        rub_per_thb=2.5,
        published_date="04.10.2026",
        fetched_at=datetime.now(timezone.utc),
    )


def _offer(**kwargs) -> ThailandOffer:
    base = dict(
        store="jib",
        external_id="1",
        name="ASUS ROG STRIX G16 RTX 5070 Ti",
        url="https://www.jib.co.th/1",
        price_thb=84990,
        available=True,
        availability_status="in_stock",
        availability_confirmed=True,
        gpu="RTX 5070 Ti",
        gpu_source=GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
        cpu="AMD Ryzen 9 8940HX",
        ram_gb=16,
        ssd_gb=1024,
        screen_size_inch=16.0,
        collected_at=datetime.now(timezone.utc),
    )
    base.update(kwargs)
    o = ThailandOffer(**base)
    return compute_verification(o)


class TopHardeningTests(unittest.TestCase):
    def test_verified_only_default(self) -> None:
        verified = _offer(external_id="v1", price_thb=90000)
        unverified = _offer(
            external_id="u1",
            name="ASUS TUF A18",
            gpu=None,
            gpu_source=None,
            candidate_gpu="RTX 5080",
            price_thb=57590,
            availability_confirmed=False,
            availability_status="unknown",
            available=False,
        )
        top = build_thai_top([verified, unverified], fx=_fx())
        ids = {r["external_id"] for r in top}
        self.assertIn("v1", ids)
        self.assertNotIn("u1", ids)

    def test_unknown_availability_excluded(self) -> None:
        o = _offer(
            availability_confirmed=False,
            availability_status="unknown",
            available=False,
        )
        self.assertEqual(build_thai_top([o], fx=_fx()), [])

    def test_explicit_gpus_included(self) -> None:
        a = _offer(external_id="a", gpu="RTX 5070 Ti", price_thb=90000)
        b = _offer(
            external_id="b",
            name="ROG STRIX G18 RTX 5080",
            gpu="RTX 5080",
            price_thb=120000,
        )
        top = build_thai_top([a, b], fx=_fx(), limit=5)
        self.assertEqual(len(top), 2)

    def test_ordering_after_exclusions(self) -> None:
        cheap_unverified = _offer(
            external_id="cheap",
            price_thb=10000,
            gpu=None,
            availability_confirmed=False,
            available=False,
            availability_status="unknown",
        )
        mid = _offer(external_id="mid", price_thb=90000, gpu="RTX 5070 Ti")
        # Keep under 300k RUB at 2.5 RUB/THB (cap = 120000 THB).
        high = _offer(
            external_id="high",
            name="X RTX 5080",
            gpu="RTX 5080",
            price_thb=110000,
        )
        top = build_thai_top([cheap_unverified, mid, high], fx=_fx())
        # 5080 GPU component outweighs cheaper 5070 Ti on intl score.
        ids = [r["external_id"] for r in top]
        self.assertEqual(set(ids), {"high", "mid"})
        self.assertNotIn("cheap", ids)

    def test_intl_confidence_low_without_gpu_avail(self) -> None:
        o = _offer(
            gpu=None,
            gpu_source=GPU_SOURCE_SEARCH_QUERY,
            availability_confirmed=False,
            available=False,
            availability_status="unknown",
        )
        conf = international_confidence(o)
        self.assertLessEqual(conf, 40)

    def test_intl_confidence_high_when_verified(self) -> None:
        o = _offer()
        self.assertEqual(o.verification_status, VERIFIED)
        self.assertGreaterEqual(international_confidence(o), 70)


if __name__ == "__main__":
    unittest.main()
