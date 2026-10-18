from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from personal_top import (
    assess_model,
    confirmed_specs,
    enqueue_personal_job,
    format_personal_board,
    main,
    match_products,
    persist_personal_scan,
    russian_side,
    target_position,
)
from thailand.job_queue import pending_count


NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
FULL = ' Intel Core Ultra 7 255HX, RTX 5070 Ti, 32GB DDR5, 1TB SSD, 16" 2560x1600'

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


def _confirmed_row(**overrides) -> dict:
    row = {
        "match_level": "EXACT",
        "store": "jib",
        "name": "MSI Raider A2XWHG-814XRU" + FULL,
        "price_thb": 80000,
        "price_rub": 200000,
        "out_of_stock": False,
        "availability_confirmed": True,
        "verification_status": "VERIFIED",
        "marketplace": False,
        "seller_trust_tier": None,
        "cpu": "Intel Core Ultra 7 255HX",
        "gpu": "RTX 5070 Ti",
        "ram_gb": 32,
        "ssd_gb": 1024,
        "screen_size_inch": 16,
        "screen_resolution": "2560x1600",
                    "screen_size_inch": 16,
        "url": "https://jib.example/raider",
        "spec_differences": [],
        "purchase_eligible": True,
    }
    row.update(overrides)
    return row


def _exact_search(*rows: dict, price_thb: int = 80000, price_rub: int = 200000) -> dict:
    exact = list(rows) or [_confirmed_row(price_thb=price_thb, price_rub=price_rub)]
    return {
        "match_level": "EXACT",
        "exact_matches": exact,
        "same_family_matches": [],
        "equivalent_matches": [],
        "stores_skipped": [],
    }


def _russian(price: int = 248_000) -> dict:
    name = "MSI Raider A2XWHG-814XRU" + FULL
    return {
        "found": True,
        "price": price,
        "store": "regard",
        "url": "https://ru",
        "history_min": 240_000,
        "offers": [],
        "available": True,
        "name": name,
        "confirmed_specs": confirmed_specs({"name": name, "sku": "A2XWHG-814XRU"}),
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

    def test_lenovo_series_variants_stay_candidates(self) -> None:
        products = [
            _product("regard", "l1", "Lenovo Legion Pro 7 16AFR10H" + FULL, "83RU003JRK", 275_000),
            _product("kns", "w", "Lenovo Legion Pro 7 16AFR10H" + FULL, "83RU003JRK-wpro", 280_000),
            _product(
                "citilink",
                "small",
                'Lenovo Legion Pro 7 16AFR10H RTX 5070 Ti, 16GB DDR5, 16" 2560x1600',
                "83RU0099RK",
                260_000,
            ),
            _product(
                "regard",
                "big",
                'Lenovo Legion Pro 7 16AFR10H RTX 5080, 32GB DDR5, 1TB SSD, 16" 2560x1600',
                "83RU0088RK",
                390_000,
            ),
        ]
        self.assertEqual(match_products(LENOVO, products), [])
        side = russian_side(LENOVO, products, fresh_stores={"regard", "kns", "citilink"})
        self.assertFalse(side["found"])
        skus = [row["sku"] for row in side["candidates"]]
        self.assertEqual(skus, ["83RU003JRK", "83RU003JRK-wpro"])
        wpro = side["candidates"][1]
        self.assertIn("вариант -wpro", wpro["differences"])
        card = assess_model(
            LENOVO,
            side,
            {
                "exact_matches": [],
                "same_family_matches": [],
                "equivalent_matches": [],
                "series_candidates": [
                    {
                        "match_level": "CANDIDATE",
                        "sku": "LEN-TH",
                        "store": "jib",
                        "price_thb": 90_000,
                        "price_rub": 220_000,
                        "out_of_stock": False,
                        "differences": ["SSD не подтверждён"],
                        "url": "https://jib.example/legion",
                    }
                ],
            },
            fx=_fx(),
            checked_at=NOW.isoformat(),
            now=NOW,
        )
        self.assertEqual(card["verdict"], "candidates_only")
        self.assertIsNone(card["winner"])
        text = format_personal_board([card])
        self.assertIn("83RU003JRK", text)
        self.assertNotIn("LEN-TH", text)  # Legacy candidate has unconfirmed SSD.
        self.assertIn("не EXACT", text)
        self.assertNotIn("Таиланд EXACT", text)
        self.assertNotIn("выгоднее", text)

    def test_fresh_available_confirmed_offer_only(self) -> None:
        name = "MSI Raider A2XWHG-814XRU" + FULL
        products = [
            _product("regard", "stale", name, "A2XWHG-814XRU", 230_000),
            _product("kns", "gone", name, "9S7-15M361-814", 240_000, available=False),
            _product("citilink", "ok", name, "A2XWHG-814XRU", 248_000),
            _product("citilink", "bare", "MSI Raider A2XWHG-814XRU", "A2XWHG-814XRU", 210_000),
        ]
        side = russian_side(RAIDER, products, fresh_stores={"citilink", "kns"})
        self.assertEqual(side["price"], 248_000)
        self.assertEqual(side["store"], "citilink")

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
        russian = _russian()
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
        russian = _russian()
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
                    "name": "Suitable test Strix variant",
                    "gpu": "RTX 5070 Ti",
                    "ram_gb": 64,
                    "ssd_gb": 1024,
                    "screen_resolution": "2560x1600",
                    "screen_size_inch": 16,
                    "availability_confirmed": True,
                    "verification_status": "VERIFIED",
                    "purchase_eligible": True,
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

    def test_cheapest_confirmed_store_ignores_regional_variant(self) -> None:
        russian = _russian(255_000)
        regional = _confirmed_row(
            store="invadeit",
            name="ROG Strix G615LR-S5162",
            price_rub=180_000,
            price_thb=70000,
            match_level="SAME_FAMILY",
            spec_differences=["RAM: 32 ГБ и 64 ГБ"],
        )
        dearer = _confirmed_row(store="jib", price_rub=230_000, price_thb=90000)
        cheaper = _confirmed_row(store="itcity", price_rub=210_000, price_thb=82000)
        search = {
            "exact_matches": [dearer, cheaper],
            "same_family_matches": [regional],
            "equivalent_matches": [],
        }
        card = assess_model(RAIDER, russian, search, fx=_fx(), checked_at=NOW.isoformat(), now=NOW)
        self.assertEqual(card["winner"]["store"], "itcity")
        self.assertEqual(card["winner"]["price_rub"], 210_000)
        self.assertEqual(card["verdict"], "thailand")

    def test_missing_verification_blocks_the_verdict(self) -> None:
        row = _confirmed_row(verification_status="UNVERIFIED")
        card = assess_model(
            RAIDER,
            _russian(),
            _exact_search(row),
            fx=_fx(),
            checked_at=NOW.isoformat(),
            now=NOW,
        )
        self.assertEqual(card["verdict"], "unconfirmed")
        text = format_personal_board([card])
        self.assertIn("Сравнение не подтверждено", text)
        self.assertNotIn("выгоднее", text)


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
            later = enqueue_personal_job(
                manual=False,
                products=products,
                jobs_dir=Path(tmp) / "next-day",
                state={"checked_at": (NOW - timedelta(hours=30)).isoformat()},
                now=NOW,
            )
            self.assertTrue(later["ok"])
            self.assertEqual(pending_count(Path(tmp) / "next-day"), 1)

    def test_worker_scan_covers_all_three_models(self) -> None:
        from buy_thailand_flow import execute_thailand_job
        from personal_top import target_for
        from thailand.models import ThailandOffer

        targets = [target_for(model, {"name": "", "confirmed_specs": {}}) for model in config.PERSONAL_TOP3]
        self.assertEqual([item["personal_key"] for item in targets], [model["key"] for model in config.PERSONAL_TOP3])
        legion = next(item for item in targets if item.get("required_gpu"))
        offer = ThailandOffer(
            store="jib",
            external_id="legion-1",
            name="Lenovo Legion Pro 7 16AFR10H RTX 5070 Ti 32GB",
            url="https://jib.example/legion",
            price_thb=110_000,
            available=True,
            collected_at=NOW,
            sku="83RU003JRK",
            ssd_gb=1024,
            gpu_source="structured_api",
            verification_status="VERIFIED",
            availability_confirmed=True,
            availability_status="in_stock",
            gpu="RTX 5070 Ti",
            ram_gb=32,
            screen_resolution="2560x1600",
            screen_size_inch=16,
        )
        from personal_top import attach_series_candidates
        from thailand.models import FxRate
        rate = FxRate(source="test", currency="THB", nominal=1, official_rate=2.5, rub_per_thb=2.5, published_date=None, fetched_at=NOW)

        attached = attach_series_candidates(legion, {"exact_matches": []}, [offer], rate)
        self.assertEqual(attached["series_candidates"][0]["match_level"], "CANDIDATE")
        self.assertNotEqual(attached["series_candidates"][0]["match_level"], "EXACT")

        def scan_fn(**kwargs):
            self.assertEqual(len(kwargs["personal_targets"]), 3)
            return {
                "status": "ok",
                "finished_at": NOW.isoformat(),
                "fx": _fx(),
                "stores": [{"store": "jib", "ok": True}],
                "personal_searches": [
                    {
                        "key": item["personal_key"],
                        "search": attach_series_candidates(item, {"exact_matches": [], "same_family_matches": [], "equivalent_matches": []}, [offer], rate),
                    }
                    for item in kwargs["personal_targets"]
                ],
            }

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("personal_top.datetime", wraps=datetime) as clock, mock.patch("personal_top.state_path", return_value=Path(tmp) / "state.json"), mock.patch(
                "storage.get_all_products_readonly", return_value=[]
            ):
                clock.now.return_value = NOW
                result = execute_thailand_job(
                    {
                        "job_id": "personal-test",
                        "trigger_type": "personal_daily",
                        "personal_targets": targets,
                        "signals": [],
                        "russian_context": [],
                    },
                    deliver=False,
                    scan_fn=scan_fn,
                )
        text = result["messages"][0]
        self.assertIn("Raider 16", text)
        self.assertIn("G615LR-S5348", text)
        self.assertIn("16AFR10H", text)
        self.assertIn("83RU003JRK", text)
        self.assertNotIn("выгоднее", text)

    def test_timer_is_pinned_to_moscow(self) -> None:
        text = Path("deploy/systemd/laptop-monitor-personal-top.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 08:00:00 Europe/Moscow", text)

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
            products = [
                _product("regard", "r", "MSI Raider A2XWHG-814XRU" + FULL, "A2XWHG-814XRU", 248_000)
            ]
            cards = __import__("personal_top").build_cards(
                products=products, state=state, now=NOW, fresh_stores={"regard"}
            )
            self.assertEqual(cards[0]["verdict"], "thailand")
            side = russian_side(
                RAIDER, products, history_mins={1: 239_000}, fresh_stores={"regard"}
            )
            self.assertEqual(side["history_min"], 239_000)


class ProductionDbPathTests(unittest.TestCase):
    def test_enqueue_command_opens_the_production_database(self) -> None:
        from storage import DEFAULT_DB_PATH

        with (
            mock.patch("storage.get_all_products_readonly", return_value=[]),
            mock.patch("storage.get_latest_store_runs_readonly", return_value={}) as latest,
            mock.patch("store_freshness.get_fresh_store_slugs", return_value=set()),
            mock.patch("personal_top.specs_for_products", return_value={}),
            mock.patch(
                "personal_top.enqueue_personal_job",
                return_value={"ok": True, "thailand_scan_status": "queued"},
            ),
        ):
            code = main(["enqueue", "--manual"])
        self.assertEqual(code, 0)
        latest.assert_called_once_with(DEFAULT_DB_PATH)

    def test_telegram_personal_handlers_open_the_production_database(self) -> None:
        from storage import DEFAULT_DB_PATH
        from control_bot import handle_personal_refresh, handle_personal_top

        with (
            mock.patch("storage.get_all_products_readonly", return_value=[]),
            mock.patch("storage.get_latest_store_runs_readonly", return_value={}) as latest,
            mock.patch("storage.get_price_history_for_products_readonly", return_value={}),
            mock.patch("store_freshness.get_fresh_store_slugs", return_value=set()),
            mock.patch("personal_top.specs_for_products", return_value={}),
            mock.patch("personal_top.load_state", return_value={"history": {}}),
            mock.patch("personal_top.build_cards", return_value=[]),
            mock.patch("personal_top.format_personal_board", return_value="board"),
            mock.patch("personal_top.enqueue_personal_job", return_value={"ok": False, "error": "too_soon"}),
            mock.patch("control_bot._reply_or_edit"),
            mock.patch("control_bot.send_message"),
        ):
            handle_personal_top(mock.Mock(), "token", 1)
            handle_personal_refresh(mock.Mock(), "token", 1)
        self.assertEqual(latest.call_args_list, [mock.call(DEFAULT_DB_PATH), mock.call(DEFAULT_DB_PATH), mock.call(DEFAULT_DB_PATH)])
