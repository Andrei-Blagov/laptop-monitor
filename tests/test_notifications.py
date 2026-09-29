from __future__ import annotations

import unittest

from alerts import (
    EVENT_CROSS_STORE,
    EVENT_HISTORICAL_LOW,
    EVENT_PRICE_DROP,
    EVENT_TARGET_PRICE,
)
from notifications import (
    format_alert_event,
    format_cross_store,
    format_historical_low,
    format_price_drop,
    format_target_price,
    format_test_message,
)


def _event(event_type: str, **kwargs) -> dict:
    meta = kwargs.pop("metadata", {})
    return {
        "id": kwargs.get("id", 1),
        "event_type": event_type,
        "old_price": kwargs.get("old_price"),
        "new_price": kwargs.get("new_price"),
        "metadata": meta,
    }


class NotificationFormatTests(unittest.TestCase):
    def test_target_price_formatting(self) -> None:
        text = format_target_price(
            _event(
                EVENT_TARGET_PRICE,
                new_price=222_190,
                metadata={
                    "name": "Gigabyte Gaming A16 Pro GA6DH",
                    "gpu": "RTX 5070 Ti",
                    "store": "regard",
                    "threshold": 230_000,
                    "sku": "DXHG4KZCC4SD",
                    "url": "https://www.regard.ru/product/1",
                },
            )
        )
        self.assertIn("TARGET PRICE", text)
        self.assertIn("Gigabyte Gaming A16 Pro GA6DH", text)
        self.assertIn("RTX 5070 Ti", text)
        self.assertIn("Regard", text)
        self.assertIn("222 190", text)
        self.assertIn("230 000", text)
        self.assertIn("DXHG4KZCC4SD", text)
        self.assertIn("https://www.regard.ru/product/1", text)

    def test_price_drop_formatting(self) -> None:
        text = format_price_drop(
            _event(
                EVENT_PRICE_DROP,
                old_price=250_000,
                new_price=234_000,
                metadata={
                    "name": "MSI Vector 16",
                    "store": "regard",
                    "drop_percent": 6.4,
                    "drop_amount": 16_000,
                    "url": "https://example.test/p",
                },
            )
        )
        self.assertIn("PRICE DROP", text)
        self.assertIn("250 000", text)
        self.assertIn("234 000", text)
        self.assertIn("16 000", text)
        self.assertIn("6.4", text)
        self.assertIn("Regard", text)
        self.assertNotIn("исторический минимум", text)

    def test_price_drop_with_historical_low_note(self) -> None:
        text = format_price_drop(
            _event(
                EVENT_PRICE_DROP,
                old_price=293_821,
                new_price=262_512,
                metadata={
                    "name": "ASUS",
                    "store": "andpro",
                    "drop_percent": 10.66,
                    "drop_amount": 31_309,
                    "is_historical_low": True,
                    "previous_historical_low": 293_821,
                    "url": "https://example.test/p",
                },
            )
        )
        self.assertIn("Также это новый исторический минимум", text)
        self.assertIn("293 821", text)

    def test_historical_low_formatting(self) -> None:
        text = format_historical_low(
            _event(
                EVENT_HISTORICAL_LOW,
                old_price=279_990,
                new_price=269_990,
                metadata={
                    "name": "ASUS ROG",
                    "store": "andpro",
                    "previous_historical_min": 279_990,
                    "url": "https://andpro.ru/x",
                },
            )
        )
        self.assertIn("NEW HISTORICAL LOW", text)
        self.assertIn("269 990", text)
        self.assertIn("279 990", text)
        self.assertIn("ANDPRO", text)

    def test_cross_store_formatting(self) -> None:
        text = format_cross_store(
            _event(
                EVENT_CROSS_STORE,
                metadata={
                    "name": "ASUS ROG Strix G16",
                    "cheapest_store": "regard",
                    "cheapest_price": 270_190,
                    "other_store": "andpro",
                    "other_price": 283_605,
                    "difference": 13_415,
                    "cheapest_url": "https://regard.ru/a",
                    "other_url": "https://andpro.ru/b",
                },
            )
        )
        self.assertIn("CROSS-STORE SAVING", text)
        self.assertIn("270 190", text)
        self.assertIn("283 605", text)
        self.assertIn("13 415", text)
        self.assertIn("Дешевле: Regard", text)
        self.assertIn("https://regard.ru/a", text)
        self.assertIn("https://andpro.ru/b", text)

    def test_html_escaping(self) -> None:
        text = format_target_price(
            _event(
                EVENT_TARGET_PRICE,
                new_price=100,
                metadata={
                    "name": 'Laptop <b>Bold</b> & "Quote"',
                    "gpu": "RTX 5070 Ti",
                    "store": "regard",
                    "threshold": 230_000,
                    "url": "https://example.test/?a=1&b=2",
                },
            )
        )
        self.assertNotIn("<b>Bold</b>", text)
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("&quot;Quote&quot;", text)
        # Outer title tag remains.
        self.assertIn("<b>TARGET PRICE</b>", text)

    def test_format_alert_event_dispatch(self) -> None:
        text = format_alert_event(
            _event(EVENT_TARGET_PRICE, new_price=1, metadata={"name": "X", "store": "regard"})
        )
        self.assertIn("TARGET PRICE", text)

    def test_test_message(self) -> None:
        text = format_test_message()
        self.assertIn("Laptop Monitor", text)
        self.assertIn("Telegram подключён", text)


if __name__ == "__main__":
    unittest.main()
