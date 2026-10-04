from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.eligibility import (
    EXCLUDED_FX_UNUSABLE,
    EXCLUDED_PRICE_ABOVE_CAP,
    annotate_offer_price_scope,
    effective_thb_cap,
    offer_user_facing_eligible,
    price_within_cap,
)
from thailand.formatting import format_thailand_alternatives_message
from thailand.models import FxRate, ThailandOffer
from thailand.scanner import build_cheapest_eligible, build_thai_top, summarize_price_cap
from thailand.verification import (
    GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
    compute_verification,
)


def _fx(rub_per_thb: float = 2.5) -> FxRate:
    return FxRate(
        source="CBR",
        currency="THB",
        nominal=10,
        official_rate=rub_per_thb * 10,
        rub_per_thb=rub_per_thb,
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
    return compute_verification(ThailandOffer(**base))


class PriceCapTests(unittest.TestCase):
    def test_299999_eligible(self) -> None:
        self.assertTrue(price_within_cap(299_999))

    def test_300000_eligible(self) -> None:
        self.assertTrue(price_within_cap(300_000))

    def test_300001_excluded(self) -> None:
        self.assertFalse(price_within_cap(300_001))

    def test_top_excludes_over_cap(self) -> None:
        # 2.5 RUB/THB → 120000 THB = 300000 RUB (in); 120001 → out
        ok = _offer(external_id="ok", price_thb=120_000)
        bad = _offer(external_id="bad", price_thb=120_001, name="X RTX 5080", gpu="RTX 5080")
        top = build_thai_top([ok, bad], fx=_fx(2.5))
        ids = {r["external_id"] for r in top}
        self.assertIn("ok", ids)
        self.assertNotIn("bad", ids)

    def test_alternatives_message_title(self) -> None:
        row = {
            "name": "Test",
            "gpu": "RTX 5070 Ti",
            "ram_gb": 16,
            "ssd_gb": 1024,
            "screen_size_inch": 16,
            "price_thb": 84990,
            "price_rub": 210000,
            "store": "jib",
            "store_label": "JIB",
            "international_score": 70,
            "url": "https://example.com",
        }
        text = format_thailand_alternatives_message(
            [row],
            price_cap={
                "max_tracked_price_rub": 300000,
                "verified_count": 2,
                "eligible_count": 1,
                "over_cap_count": 1,
                "fx_usable": True,
            },
        )
        self.assertIn("ДО 300 000", text)
        self.assertIn("дороже лимита: 1", text)
        self.assertIn("Показаны только предложения до 300 000", text)

    def test_best_excludes_over_cap(self) -> None:
        bad = _offer(price_thb=200_000)  # 500k RUB at 2.5
        self.assertEqual(build_cheapest_eligible([bad], fx=_fx(2.5)), [])

    def test_over_cap_retained_in_annotation(self) -> None:
        o = _offer(price_thb=200_000)
        annotate_offer_price_scope(o, fx=_fx(2.5))
        self.assertFalse(o.in_tracking_scope)
        self.assertEqual(o.excluded_reason, EXCLUDED_PRICE_ABOVE_CAP)
        self.assertEqual(o.metadata.get("price_rub"), 500_000)

    def test_no_fx_no_unsafe_top(self) -> None:
        o = _offer(price_thb=50_000)
        self.assertEqual(build_thai_top([o], fx=None), [])
        ok, reason, _ = offer_user_facing_eligible(o, fx=None)
        self.assertFalse(ok)
        self.assertEqual(reason, EXCLUDED_FX_UNUSABLE)
        text = format_thailand_alternatives_message([], fx_usable=False)
        self.assertIn("курс THB/RUB недоступен", text)

    def test_over_cap_count(self) -> None:
        offers = [
            _offer(external_id="a", price_thb=80_000),
            _offer(external_id="b", price_thb=150_000, name="Y RTX 5080", gpu="RTX 5080"),
        ]
        summary = summarize_price_cap(offers, fx=_fx(2.5))
        self.assertEqual(summary["over_cap_count"], 1)
        self.assertGreaterEqual(summary["eligible_count"], 1)
        self.assertAlmostEqual(effective_thb_cap(_fx(2.5)) or 0, 120_000.0)

    def test_sort_value_then_price(self) -> None:
        cheap_5070 = _offer(external_id="c", price_thb=70_000, gpu="RTX 5070 Ti")
        mid_5080 = _offer(
            external_id="m",
            price_thb=100_000,
            name="Z RTX 5080",
            gpu="RTX 5080",
        )
        top = build_thai_top([cheap_5070, mid_5080], fx=_fx(2.5))
        self.assertEqual(len(top), 2)
        # 5080 typically higher intl score than cheaper 5070 Ti
        self.assertEqual(top[0]["external_id"], "m")


if __name__ == "__main__":
    unittest.main()
