from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.comparison import (
    VERDICT_CONFIG_DIFFERS,
    VERDICT_EQUAL,
    VERDICT_FX_UNAVAILABLE,
    VERDICT_NO_COMPARABLE,
    VERDICT_RUSSIA_CLEARLY,
    VERDICT_RUSSIA_SLIGHTLY,
    VERDICT_THAI_CLEARLY,
    VERDICT_THAI_SLIGHTLY,
    country_verdict,
)
from thailand.models import CrossCountryMatch, FxRate, ThailandOffer


def _fx() -> FxRate:
    return FxRate(
        source="CBR",
        currency="THB",
        nominal=10,
        official_rate=25.0,
        rub_per_thb=2.5,
        published_date="04.10.2026",
        fetched_at=datetime.now(timezone.utc),
        stale=False,
    )


def _match(**kwargs) -> CrossCountryMatch:
    thai = ThailandOffer(
        store="banana",
        external_id="1",
        name="MSI Vector",
        url="https://example/t",
        price_thb=60000,
        available=True,
        collected_at=datetime.now(timezone.utc),
        gpu="RTX 5070 Ti",
        ram_gb=32,
        ssd_gb=1024,
        screen_size_inch=16.0,
        cpu="Intel Core Ultra 9 275HX",
    )
    base = dict(
        level="EQUIVALENT",
        russian_name="MSI Vector",
        thai_offer=thai,
        russian_price_rub=230000,
        thai_price_thb=60000,
        thai_price_rub=230000,
        delta_rub=0,
        delta_percent=0.0,
        differences=[],
        reason="test",
    )
    base.update(kwargs)
    return CrossCountryMatch(**base)


class VerdictTests(unittest.TestCase):
    def test_roughly_equal(self) -> None:
        # 2% thai higher
        m = _match(thai_price_rub=234600, delta_rub=4600, delta_percent=0.02)
        c = country_verdict(m, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_EQUAL)

    def test_slight(self) -> None:
        m = _match(thai_price_rub=241500, delta_rub=11500, delta_percent=0.05)
        c = country_verdict(m, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_RUSSIA_SLIGHTLY)

    def test_clear(self) -> None:
        m = _match(thai_price_rub=253000, delta_rub=23000, delta_percent=0.10)
        c = country_verdict(m, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_RUSSIA_CLEARLY)

    def test_thailand_cheaper_signed(self) -> None:
        m = _match(thai_price_rub=200000, delta_rub=-30000, delta_percent=-0.13)
        c = country_verdict(m, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_THAI_CLEARLY)
        self.assertTrue(any("Таиланд дешевле" in r for r in c.reasons))

    def test_russia_cheaper_signed(self) -> None:
        m = _match(thai_price_rub=250000, delta_rub=20000, delta_percent=0.087)
        c = country_verdict(m, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_RUSSIA_CLEARLY)
        self.assertTrue(any("Россия дешевле" in r for r in c.reasons))

    def test_config_differs_blocks_price_verdict(self) -> None:
        m = _match(
            level="SAME_FAMILY",
            thai_price_rub=241500,
            delta_rub=11500,
            delta_percent=0.05,
            differences=["ram:32vs64", "ssd:1024vs2048"],
        )
        c = country_verdict(m, fx=_fx())
        self.assertIn(c.verdict, {VERDICT_CONFIG_DIFFERS, "THAILAND_BETTER_SPEC_HIGHER_PRICE"})

    def test_no_comparable(self) -> None:
        c = country_verdict(None, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_NO_COMPARABLE)

    def test_fx_unavailable(self) -> None:
        m = _match()
        c = country_verdict(m, fx=None)
        self.assertEqual(c.verdict, VERDICT_FX_UNAVAILABLE)
        stale = _fx()
        stale.stale = True
        c2 = country_verdict(m, fx=stale)
        self.assertEqual(c2.verdict, VERDICT_FX_UNAVAILABLE)

    def test_slight_thailand(self) -> None:
        m = _match(thai_price_rub=218500, delta_rub=-11500, delta_percent=-0.05)
        c = country_verdict(m, fx=_fx())
        self.assertEqual(c.verdict, VERDICT_THAI_SLIGHTLY)


if __name__ == "__main__":
    unittest.main()
