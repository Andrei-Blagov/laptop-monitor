from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from thailand.fx import (
    fetch_cbr_thb_rate,
    fx_usable_for_verdict,
    mark_stale_if_old,
    parse_cbr_thb_xml,
    thb_to_rub,
)
from thailand.models import FxRate


CBR_XML = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="04.10.2026" name="Foreign Currency Market">
  <Valute ID="R01235">
    <NumCode>764</NumCode>
    <CharCode>THB</CharCode>
    <Nominal>10</Nominal>
    <Name>Thai Baht</Name>
    <Value>25,1234</Value>
    <VunitRate>2,51234</VunitRate>
  </Valute>
</ValCurs>
"""


class FxTests(unittest.TestCase):
    def test_nominal_10(self) -> None:
        fx = parse_cbr_thb_xml(CBR_XML)
        self.assertEqual(fx.nominal, 10)
        self.assertAlmostEqual(fx.official_rate, 25.1234, places=4)
        self.assertAlmostEqual(fx.rub_per_thb, 2.51234, places=5)

    def test_rub_per_thb_calc(self) -> None:
        fx = parse_cbr_thb_xml(CBR_XML)
        self.assertEqual(thb_to_rub(10000, fx), 25123)

    def test_invalid_response_safe(self) -> None:
        with self.assertRaises(ValueError):
            parse_cbr_thb_xml("<ValCurs></ValCurs>")

    def test_network_failure_safe(self) -> None:
        with patch("thailand.fx.httpx.Client") as client_cls:
            client = MagicMock()
            client.get.side_effect = OSError("network")
            client_cls.return_value = client
            fx = fetch_cbr_thb_rate()
            self.assertIsNone(fx)

    def test_stale_cache_marked(self) -> None:
        fx = FxRate(
            source="CBR",
            currency="THB",
            nominal=10,
            official_rate=25.0,
            rub_per_thb=2.5,
            published_date="01.01.2026",
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        mark_stale_if_old(fx)
        self.assertTrue(fx.stale)
        self.assertGreater(fx.rate_age_hours or 0, 36)

    def test_stale_not_used_for_verdict(self) -> None:
        fx = FxRate(
            source="CBR",
            currency="THB",
            nominal=10,
            official_rate=25.0,
            rub_per_thb=2.5,
            published_date="01.01.2026",
            fetched_at=datetime.now(timezone.utc),
            stale=True,
            rate_age_hours=50,
        )
        self.assertFalse(fx_usable_for_verdict(fx))
        self.assertFalse(fx_usable_for_verdict(None))


if __name__ == "__main__":
    unittest.main()
