from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from personal_top import (
    assess_model,
    enqueue_personal_job,
    format_personal_board,
    match_products,
    persist_personal_scan,
    russian_side,
    target_position,
)
from thailand.job_queue import pending_count


NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)

RAIDER = config.PERSONAL_TOP3[0]
ASUS = config.PERSONAL_TOP3[1]
LENOVO = config.PERSONAL_TOP3[2]


def _product(store: str, ext: str, name: str, sku: str, price: int, *, available: bool = True) -> dict:
    return {
        "id": 1,
        "store": store,
        "external_id": ext,
        "name": name,
        "sku": sku,
        "price": price,
        "available": available,
        "url": f"https://example.test/{store}/{ext}",
    }


def _exact_search(price_thb: int = 80000, price_rub: int = 200000) -> dict:
    return {
        "match_level": "EXACT",
        "exact_matches": [
            {
                "match_level": "EXACT",
                "store": "jib",
                "name": "MSI Raider A2XWHG-814XRU",
                "price_thb": price_thb,
                "price_rub": price_rub,
                "out_of_stock": False,
                "url": "https://jib.example/raider",
                "spec_differences": [],
            }
        ],
        "same_family_matches": [],
        "equivalent_matches": [],
        "stores_skipped": [],
    }


def _fx() -> dict:
    return {"rub_per_thb": 2.5, "published_date": "2026-10-11", "stale": False}


class IdentityTests(unittest.TestCase):
    def test_three_models_and_vector_watchlist_stay_separate(self) -> None:
        keys = [item["key"] for item in config.PERSONAL_TOP3]
        self.assertEqual(
            keys,
            [
                "msi-raider-16-a2xwhg-814xru",
                "asus-strix-g16-g615lr-s5348",
                "lenovo-legion-pro-7-16afr10h",
            ],
        )
        watched = [item["model"] for item in config.PRIORITY_MODELS]
        self.assertIn("Vector 17 HX AI A2XWIG-063XRU", watched)

    def test_wpro_code_is_not_the_base_model(self) -> None:
        products = [
            _product("kns", "w", "MSI Raider A2XWHG-814XRU-WPRO", "9S7-15M361-814-WPRO", 274_000),
            _product("regard", "r", "MSI Raider A2XWHG-814XRU", "9S7-15M361-814", 248_000),
        ]
        hits = match_products(RAIDER, products)
        self.assertEqual([row["external_id"] for row in hits], ["r"])

    def test_target_band(self) -> None:
        self.assertEqual(target_position(245_000, RAIDER), "в целевом диапазоне")
        self.assertEqual(target_position(230_000, RAIDER), "ниже цели")
        self.assertEqual(target_position(275_000, LENOVO), "в целевом диапазоне")
        self.assertEqual(target_position(290_000, ASUS), "выше цели")


class VerdictTests(unittest.TestCase):
    def test_exact_thailand_is_cheaper_after_confirmed_extra(self) -> None:
        russian = {"found": True, "price": 248_000, "store": "regard", "url": "https://ru", "history_min": 240_000, "offers": [], "available": True, "name": "Raider"}
        with mock.patch.object(config, "PERSONAL_TOP_CONFIRMED_EXTRAS_RUB", ({"name": "fee", "rub": 5_000},)):
            card = assess_model(
                RAIDER,
                russian,
                _exact_search(price_rub=200_000),
                fx=_fx(),
                checked_at=NOW.isoformat(),
                now=NOW,
            )
        self.assertEqual(card["verdict"], "thailand")
        self.assertEqual(card["thai_total_rub"], 205_000)
        self.assertEqual(card["delta_rub"], 43_000)
        text = "\n".join(format_personal_board([card]).splitlines())
        self.assertIn("Вывод: выгоднее Таиланд", text)
        self.assertIn("Подтверждённые расходы: 5 000 ₽", text)
        self.assertIn("авиаперелёт", text)
        self.assertIn("2026-10-11", text)
        self.assertIn("80 000 ฿", text)

    def test_stale_thai_price_is_not_current(self) -> None:
        russian = {"found": True, "price": 248_000, "store": "regard", "url": None, "history_min": None, "offers": [], "available": True, "name": "Raider"}
        old = (NOW - timedelta(days=3)).isoformat()
        card = assess_model(
            RAIDER,
            russian,
            _exact_search(),
            fx=_fx(),
            checked_at=old,
            now=NOW,
        )
        self.assertEqual(card["verdict"], "no_fresh_thai")
        self.assertFalse(card["fresh"])
        text = format_personal_board([card])
        self.assertIn("свежей цены нет", text)
        self.assertNotIn("EXACT", text)
        self.assertIn("не используется как актуальная", text)

    def test_analog_is_not_a_price_verdict(self) -> None:
        russian = {"found": True, "price": 255_000, "store": "kns", "url": None, "history_min": None, "offers": [], "available": True, "name": "Strix"}
        search = {
            "exact_matches": [],
            "same_family_matches": [
                {
                    "match_level": "SAME_FAMILY",
                    "name": "ROG Strix G615LR-S5162",
                    "store": "jib",
                    "price_thb": 90000,
                    "price_rub": 220000,
                    "out_of_stock": False,
                    "spec_differences": ["RAM: 32 ГБ и 64 ГБ"],
                    "url": "https://jib.example/analog",
                }
            ],
            "equivalent_matches": [],
        }
        card = assess_model(ASUS, russian, search, fx=_fx(), checked_at=NOW.isoformat(), now=NOW)
        self.assertEqual(card["verdict"], "analog_only")
        text = format_personal_board([card])
        self.assertIn("Ближайший аналог", text)
        self.assertIn("RAM: 32 ГБ и 64 ГБ", text)
        self.assertIn("не заменяет", text)
        self.assertNotIn("выгоднее Таиланд", text)


class QueueTests(unittest.TestCase):
    def test_daily_enqueue_is_one_job_and_respects_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs"
            products = [_product("regard", "r", "MSI Raider A2XWHG-814XRU", "9S7-15M361-814", 248_000)]
            first = enqueue_personal_job(manual=False, products=products, jobs_dir=jobs, state={"checked_at": None}, now=NOW)
            second = enqueue_personal_job(manual=False, products=products, jobs_dir=jobs, state={"checked_at": None}, now=NOW)
            self.assertTrue(first["ok"])
            self.assertEqual(pending_count(jobs), 1)
            self.assertFalse(second["ok"])
            self.assertEqual(second["error"], "already_queued")
            fresh = enqueue_personal_job(
                manual=False,
                products=products,
                jobs_dir=Path(tmp) / "other",
                state={"checked_at": (NOW - timedelta(hours=2)).isoformat()},
                now=NOW,
            )
            self.assertEqual(fresh["error"], "too_soon")
            manual = enqueue_personal_job(
                manual=True,
                chat_id=1,
                products=products,
                jobs_dir=Path(tmp) / "manual",
                state={"checked_at": (NOW - timedelta(hours=7)).isoformat()},
                now=NOW,
            )
            self.assertTrue(manual["ok"])

    def test_persist_keeps_history_without_replacing_it_as_the_only_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "personal_top.json"
            scan = {
                "finished_at": NOW.isoformat(),
                "fx": _fx(),
                "personal_searches": [{"key": RAIDER["key"], "search": _exact_search()}],
            }
            state = persist_personal_scan(scan, path=path)
            self.assertEqual(state["models"][RAIDER["key"]]["checked_at"], NOW.isoformat())
            self.assertEqual(len(state["history"][RAIDER["key"]]), 1)
            products = [_product("regard", "r", "MSI Raider A2XWHG-814XRU", "A2XWHG-814XRU", 248_000)]
            cards = __import__("personal_top").build_cards(products=products, state=state, now=NOW)
            self.assertEqual(cards[0]["verdict"], "thailand")
            side = russian_side(RAIDER, products, history_mins={1: 239_000})
            self.assertEqual(side["history_min"], 239_000)
