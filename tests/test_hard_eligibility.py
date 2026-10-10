from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import config
from buy_opportunity import evaluate_buy_rules
from comparison import Offer, ProductMatch
from deal_ranking import RankedDeal, rank_clusters, score_offer
from laptop_eligibility import (
    HARD_FILTER_REJECTED,
    NOT_AVAILABLE,
    PRICE_OVER_CAP,
    SPECS_MISSING,
    evaluate_hardware,
    parse_resolution,
    ram_meets_minimum,
    screen_meets_minimum,
)
from models import Product
from stores.common import extract_specs_from_name
from storage import create_pipeline_run, init_db, insert_store_run, open_db, save_products

SPECS = ' RTX 5080, 32GB DDR5, 17" 2560x1600'
FULL = ' Intel Core Ultra 9 275HX, RTX 5080, 32GB DDR5, 1TB SSD, 17" 2560x1600'


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _product(store: str, ext: str, *, name: str, sku: str, price: int, available: bool = True) -> Product:
    return Product(
        store=store,
        external_id=ext,
        url=f"https://example.test/{store}/{ext}",
        name=name,
        sku=sku,
        price=price,
        available=available,
        checked_at=_now(),
    )


def _seed_fresh(db: Path, stores: list[str]) -> None:
    now = _now().isoformat()
    with open_db(db) as conn:
        init_db(conn)
        rid = create_pipeline_run(conn, started_at=now, status="success")
        for store in stores:
            insert_store_run(
                conn,
                pipeline_run_id=rid,
                store=store,
                attempted_at=now,
                finished_at=now,
                status="ok",
                products_count=1,
            )
        conn.commit()


def _match(name: str, price: int, *, available: bool = True, store: str = "regard") -> ProductMatch:
    return ProductMatch(
        normalized_sku=name,
        display_sku=name,
        name=name,
        offers=[
            Offer(
                store=store,
                external_id=name,
                name=name,
                sku=name,
                price=price,
                available=available,
                url="https://x",
                product_id=1,
            )
        ],
        match_method="sku",
        matched_identifier=name,
    )


class PriceCapTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        for price, ok in ((299_999, True), (300_000, True), (329_999, True), (330_000, True), (330_001, False)):
            self.assertIs(config.is_price_in_tracking_scope(price), ok, price)

    def test_ranked_with_hard_filters(self) -> None:
        names = {
            d.cluster_name
            for d in rank_clusters(
                [_match(f"N{p}{SPECS}", p) for p in (299_999, 300_000, 329_999, 330_000, 330_001)],
                hard_filters=True,
            )
        }
        self.assertEqual(
            names,
            {f"N{p}{SPECS}" for p in (299_999, 300_000, 329_999, 330_000)},
        )


class RamTests(unittest.TestCase):
    def test_installed_ram(self) -> None:
        self.assertFalse(ram_meets_minimum(16))
        self.assertFalse(ram_meets_minimum(24))
        self.assertTrue(ram_meets_minimum(32))
        self.assertTrue(ram_meets_minimum(64))
        self.assertIsNone(ram_meets_minimum(None))

    def test_unknown_ram_is_not_eligible(self) -> None:
        verdict = evaluate_hardware(gpu="RTX 5080", ram_gb=None, screen_resolution="2560x1600")
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.reason, SPECS_MISSING)
        self.assertIn("ram_unknown", verdict.details)

    def test_upgrade_limit_is_not_installed_ram(self) -> None:
        self.assertEqual(extract_specs_from_name("Ноутбук 16 ГБ DDR5 (до 64 ГБ)").get("ram_gb"), 16)
        self.assertIsNone(extract_specs_from_name("Ноутбук, память до 64GB DDR5").get("ram_gb"))
        self.assertIsNone(extract_specs_from_name("RAM up to 64GB DDR5").get("ram_gb"))


class ScreenTests(unittest.TestCase):
    def test_minimum(self) -> None:
        rejected = ("1920x1080", "1920x1200", "2240x1400", "2560x1080")
        accepted = ("2400x1600", "2560x1440", "2560x1600", "2880x1800", "3200x2000", "3840x2160")
        for value in rejected:
            self.assertFalse(screen_meets_minimum(value), value)
        for value in accepted:
            self.assertTrue(screen_meets_minimum(value), value)

    def test_normalization(self) -> None:
        for raw in ("2560x1440", "2560×1440", "2560*1440", "2560 х 1440", "2560X1440 (16:9)"):
            self.assertEqual(parse_resolution(raw), (2560, 1440), raw)
        for raw in ("QHD", "WQHD", "2K", "2.5K", None, ""):
            self.assertIsNone(parse_resolution(raw), raw)
            self.assertIsNone(screen_meets_minimum(raw), raw)

    def test_unknown_screen_not_eligible(self) -> None:
        verdict = evaluate_hardware(gpu="RTX 5070 Ti", ram_gb=32, screen_resolution="QHD")
        self.assertEqual(verdict.reason, SPECS_MISSING)
        self.assertIn("resolution_unknown", verdict.details)

    def test_full_hd_rejected_with_reason(self) -> None:
        verdict = evaluate_hardware(gpu="RTX 5070 Ti", ram_gb=16, screen_resolution="1920x1200")
        self.assertEqual(verdict.reason, HARD_FILTER_REJECTED)
        self.assertIn("ram_16gb", verdict.details)
        self.assertIn("resolution_1920x1200", verdict.details)


class ExclusionRecordTests(unittest.TestCase):
    def test_reasons_are_recorded(self) -> None:
        exclusions: list[dict] = []
        kept = rank_clusters(
            [
                _match("Good" + SPECS, 250_000),
                _match('Small RTX 5080, 16GB DDR5, 17" 2560x1600', 250_000),
                _match("Over" + SPECS, 340_000),
                _match("Gone" + SPECS, 250_000, available=False),
                _match('Unknown RTX 5080, 17" 2560x1600', 250_000),
            ],
            hard_filters=True,
            exclusions=exclusions,
        )
        self.assertEqual([d.cluster_name for d in kept], ["Good" + SPECS])
        reasons = {e["cluster_name"].split()[0]: e["reason"] for e in exclusions}
        self.assertEqual(reasons["Small"], HARD_FILTER_REJECTED)
        self.assertEqual(reasons["Over"], PRICE_OVER_CAP)
        self.assertEqual(reasons["Gone"], NOT_AVAILABLE)
        self.assertEqual(reasons["Unknown"], SPECS_MISSING)

    def test_no_artificial_msi_bonus(self) -> None:
        msi = _match("MSI Vector 17 HX AI A2XWIG-063XRU" + SPECS, 309_900)
        other = _match("Other Brand X1" + SPECS, 309_900)
        ranked = {d.cluster_name: d.score for d in rank_clusters([msi, other], hard_filters=True)}
        self.assertEqual(len(set(ranked.values())), 1)
        expected = score_offer(
            price=309_900,
            gpu="RTX 5080",
            ram_gb=32,
            screen_inch=17.0,
            screen_resolution="2560x1600",
        ).score
        self.assertEqual(ranked[msi.name], expected)


def _regard_item(pid: int, *, title: str, brief: str = "", gpu_model: str | None = None, show_flag: int = 1, price: int = 309900) -> dict:
    item = {
        "id": pid,
        "title": title,
        "full_title": "Ноутбук " + title,
        "vendorcode": "9S7-17S372-063",
        "price": price,
        "show_flag": show_flag,
        "preorder": 0,
        "seo_url": "noutbuk",
        "brief": brief,
        "characteristics": [],
    }
    if gpu_model:
        item["characteristics"] = [
            {"title": "Графика", "data": [{"name": "Модель", "value": gpu_model}]}
        ]
    return item


class RegardPriorityFetchTests(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_targeted_fetch_when_search_misses(self) -> None:
        from parsers.regard import fetch_missing_priority_products

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json=_regard_item(492577, title="MSI Vector 17 HX AI A2XWIG-063XRU", gpu_model="GeForce RTX 5080 для ноутбука"),
            )

        with self._client(handler) as client:
            out = fetch_missing_priority_products(set(), client=client)
        self.assertEqual(calls, ["/api/site/goods/492577"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].external_id, "492577")
        self.assertEqual(out[0].price, 309900)
        self.assertTrue(out[0].available)

    def test_no_request_when_search_found_it(self) -> None:
        from parsers.regard import fetch_missing_priority_products

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        with self._client(handler) as client:
            self.assertEqual(fetch_missing_priority_products({"492577"}, client=client), [])

    def test_gpu_from_structured_specs_only(self) -> None:
        from parsers.regard import fetch_missing_priority_products, structured_gpu_text

        item = _regard_item(492577, title="MSI Vector 17", brief="17 inch", gpu_model="GeForce RTX 5080 для ноутбука")
        self.assertEqual(structured_gpu_text(item), "GeForce RTX 5080 для ноутбука")
        with self._client(lambda r: httpx.Response(200, json=item)) as client:
            self.assertEqual(len(fetch_missing_priority_products(set(), client=client)), 1)
        wrong = _regard_item(492577, title="MSI Vector 17", gpu_model="GeForce RTX 5060 для ноутбука")
        with self._client(lambda r: httpx.Response(200, json=wrong)) as client:
            self.assertEqual(fetch_missing_priority_products(set(), client=client), [])

    def test_hidden_card_stays_as_unavailable(self) -> None:
        from parsers.regard import fetch_missing_priority_products

        item = _regard_item(492577, title="MSI Vector 17 RTX 5080", show_flag=0)
        with self._client(lambda r: httpx.Response(200, json=item)) as client:
            out = fetch_missing_priority_products(set(), client=client)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].available)

    def test_full_collect_adds_once_and_db_has_no_duplicates(self) -> None:
        import parsers.regard as regard

        search_item = _regard_item(779918, title="MSI Raider 16 RTX 5070 Ti", price=261090)
        priority_item = _regard_item(492577, title="MSI Vector 17 HX AI A2XWIG-063XRU RTX 5080")
        gets: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"data": [search_item], "recordsFiltered": 1})
            gets.append(request.url.path)
            return httpx.Response(200, json=priority_item)

        with patch.object(regard, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))), patch.object(
            regard, "_save_debug", lambda *a, **k: None
        ):
            first = regard.fetch_target_laptops()
            second = regard.fetch_target_laptops()
        self.assertEqual(sorted(p.external_id for p in first), ["492577", "779918"])
        self.assertEqual(gets, ["/api/site/goods/492577", "/api/site/goods/492577"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "r.db"
            save_products(first, db)
            save_products(second, db)
            with open_db(db) as conn:
                count = conn.execute(
                    "select count(*) from products where store='regard' and external_id='492577'"
                ).fetchone()[0]
            self.assertEqual(count, 1)


class ConsistencyTests(unittest.TestCase):
    """TOP, recommendation picker, BUY source and history picker read one list."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "c.db"
        self.specs = Path(self._tmp.name) / "specs.json"
        rows = [
            ("good", "Good One" + SPECS, "GOOD-1", 250_000),
            ("small", 'Small One RTX 5080, 16GB DDR5, 17" 2560x1600', "SMALL-1", 220_000),
            ("fhd", 'Fhd One RTX 5080, 32GB DDR5, 16" 1920x1200', "FHD-1", 210_000),
            ("over", "Over One" + SPECS, "OVER-1", 340_000),
            ("unk", 'Unk One RTX 5080, 17"', "UNK-1", 200_000),
        ]
        save_products(
            [
                _product(store, f"{store}-{ext}", name=name, sku=sku, price=price + bump)
                for ext, name, sku, price in rows
                for store, bump in (("regard", 0), ("kns", 5_000))
            ],
            self.db,
        )
        _seed_fresh(self.db, ["regard", "kns"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_selection_agrees(self) -> None:
        from control_bot import build_top_text, unique_russian_top
        from model_price_history import build_history_picker_items
        from russian_deals import load_russian_ranked_deals

        with patch("identity_sync.SPECS_CACHE_PATH", self.specs):
            deals = load_russian_ranked_deals(self.db, specs_path=self.specs)
            top = build_top_text(self.db)
            picker = unique_russian_top(self.db, limit=5)
            history = build_history_picker_items(self.db)
        self.assertEqual([d.cluster_name for d in deals], ["Good One" + SPECS])
        self.assertEqual([d.cluster_name for d in picker], ["Good One" + SPECS])
        self.assertEqual([i.name for i in history], ["Good One" + SPECS])
        self.assertIn("Good One", top)
        for name in ("Small One", "Fhd One", "Over One", "Unk One"):
            self.assertNotIn(name, top)
        self.assertIn("ТОП ПРЕДЛОЖЕНИЙ ДО 330 000 ₽", top)

    def test_best_recommendation_is_eligible(self) -> None:
        from control_bot import handle_recommendation_pick

        with patch("identity_sync.SPECS_CACHE_PATH", self.specs), patch(
            "buy_thailand_flow.enqueue_recommendation_job", return_value={"ok": True}
        ) as enq, patch("control_bot.send_message"):
            handle_recommendation_pick(object(), "t", 1, "best", db_path=self.db)
        self.assertEqual(enq.call_args.args[0].cluster_name, "Good One" + SPECS)

    def test_excluded_products_cannot_trigger_buy(self) -> None:
        from russian_deals import load_russian_ranked_deals

        small = RankedDeal(
            score=90.0,
            cluster_name="Small One",
            gpu="RTX 5080",
            store="kns",
            price=220_000,
            ram_gb=16,
            screen_resolution="2560x1600",
            confidence=100,
            historical_min=220_000,
        )
        self.assertIsNotNone(evaluate_buy_rules(small))
        with patch("identity_sync.SPECS_CACHE_PATH", self.specs):
            names = [d.cluster_name for d in load_russian_ranked_deals(self.db, specs_path=self.specs, limit=0)]
        self.assertFalse(any(n.startswith("Small One") for n in names))

    def test_buy_rules_and_maturity_unchanged(self) -> None:
        self.assertEqual(config.BUY_MIN_HISTORY_DAYS, 14.0)
        self.assertEqual(config.TARGET_PRICES, {"RTX 5070 Ti": 230_000, "RTX 5080": 300_000})
        self.assertEqual(
            (
                config.BUY_RULE_A_MAX_OVER_HIST_PCT,
                config.BUY_RULE_B_MIN_SCORE,
                config.BUY_RULE_B_MAX_OVER_HIST_PCT,
                config.BUY_RULE_C_MIN_SCORE,
                config.BUY_RULE_C_MAX_OVER_HIST_PCT,
                config.BUY_STRONG_MAX_OVER_HIST_PCT,
                config.BUY_STRONG_MIN_SCORE,
                config.BUY_MIN_CONFIDENCE,
            ),
            (0.05, 75.0, 0.03, 65.0, 0.01, 0.03, 75.0, 80),
        )

    def test_unknown_ram_after_enrichment_stays_excluded(self) -> None:
        from identity_sync import sync_product_identifiers
        from product_identity import ProductIdentity
        from russian_deals import load_russian_ranked_deals

        save_products(
            [
                _product("regard", "nospec", name="NoSpec One RTX 5080", sku="NOSPEC-1", price=240_000),
                _product("kns", "nospec-k", name="NoSpec One RTX 5080", sku="NOSPEC-1", price=245_000),
            ],
            self.db,
        )
        enriched = ProductIdentity(
            store="regard",
            external_id="nospec",
            sku="NOSPEC-1",
            name="NoSpec One RTX 5080",
            price=240_000,
            available=True,
            url="u",
            gpu="RTX 5080 LAPTOP",
        )
        with patch("identity_sync.enrich_regard_product", return_value=enriched) as enrich:
            sync_product_identifiers(self.db, enrich=True, specs_path=self.specs)
        self.assertTrue(any(c.args[0]["external_id"] == "nospec" for c in enrich.call_args_list))
        exclusions: list[dict] = []
        names = [
            d.cluster_name
            for d in load_russian_ranked_deals(self.db, specs_path=self.specs, limit=0, exclusions=exclusions)
        ]
        self.assertNotIn("NoSpec One RTX 5080", names)
        reason = next(e for e in exclusions if e["cluster_name"].startswith("NoSpec"))
        self.assertEqual(reason["reason"], SPECS_MISSING)


class PriorityWatchlistTests(unittest.TestCase):
    MODEL = {
        "key": "msi-v17",
        "brand": "MSI",
        "model": "Vector 17 HX AI A2XWIG-063XRU",
        "identifiers": ("9S7-17S372-063", "A2XWIG-063XRU"),
        "store_ids": {"regard": "492577"},
        "url": "https://www.regard.ru/product/492577/x",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "p.db"
        self.specs = Path(self._tmp.name) / "specs.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _status(self, products: list[Product]):
        from priority_watchlist import build_priority_statuses

        if products:
            save_products(products, self.db)
        _seed_fresh(self.db, ["regard", "kns"])
        return build_priority_statuses(self.db, specs_path=self.specs, models=[self.MODEL])[0]

    def _msi(self, price: int, *, available: bool = True, store: str = "regard", ext: str = "492577") -> Product:
        return _product(
            store,
            ext,
            name="Ноутбук MSI Vector 17 HX AI A2XWIG-063XRU" + SPECS,
            sku="9S7-17S372-063",
            price=price,
            available=available,
        )

    def _pair(self, price: int, *, available: bool = True) -> list[Product]:
        return [
            self._msi(price, available=available),
            self._msi(price + 10_000, available=available, store="kns", ext="k-492577"),
        ]

    def test_in_top_with_price_and_status(self) -> None:
        from priority_watchlist import IN_TOP, format_priority_block

        status = self._status(self._pair(309_900))
        self.assertEqual(status.status, IN_TOP)
        self.assertEqual(status.rank, 1)
        self.assertEqual(status.best_price, 309_900)
        text = format_priority_block([status])
        self.assertIn("Приоритетная модель", text)
        self.assertIn("309 900 ₽ — Regard, в наличии", text)
        self.assertIn("Статус: в ТОП, №1", text)

    def test_over_cap_shows_reason_and_new_limit(self) -> None:
        from priority_watchlist import format_priority_block

        status = self._status(self._pair(340_000))
        self.assertEqual(status.status, PRICE_OVER_CAP)
        self.assertEqual(status.best_price, 340_000)
        self.assertIn("выше лимита 330 000 ₽", format_priority_block([status]))

    def test_unavailable(self) -> None:
        status = self._status(self._pair(309_900, available=False))
        self.assertEqual(status.status, NOT_AVAILABLE)

    def test_single_store_model_is_ranked(self) -> None:
        from priority_watchlist import IN_TOP, format_priority_block

        status = self._status([self._msi(309_900)])
        self.assertEqual(status.status, IN_TOP)
        self.assertEqual(status.rank, 1)
        self.assertIn("single_store", status.details)
        self.assertEqual(status.best_price, 309_900)
        self.assertIn("Статус: в ТОП, №1", format_priority_block([status]))

    def test_price_change_and_history_min(self) -> None:
        save_products(self._pair(319_720), self.db)
        save_products(self._pair(309_900), self.db)
        status = self._status([])
        self.assertEqual(status.previous_price, 319_720)
        self.assertEqual(status.history_min, 309_900)

    def test_not_discovered(self) -> None:
        from laptop_eligibility import NOT_DISCOVERED

        status = self._status([_product("kns", "x", name="Other" + SPECS, sku="OTHER", price=200_000)])
        self.assertEqual(status.status, NOT_DISCOVERED)

    def test_found_by_strong_id_in_other_store(self) -> None:
        status = self._status(
            [
                _product(
                    "kns",
                    "k1",
                    name="MSI Vector 17 HX AI A2XWIG-063XRU" + SPECS,
                    sku="9S7-17S372-063",
                    price=305_000,
                ),
                self._msi(309_900),
            ]
        )
        self.assertNotEqual(status.status, "NOT_DISCOVERED")
        self.assertEqual(status.best_store, "kns")
        self.assertEqual(status.best_price, 305_000)
        self.assertEqual(status.duplicate_clusters, 0)
        self.assertEqual(len(status.offers), 2)


class SingleStoreRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "s.db"
        self.specs = Path(self._tmp.name) / "specs.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _deals(self, products: list[Product], *, merges=None, ambiguous=None):
        from russian_deals import load_russian_ranked_deals

        save_products(products, self.db)
        _seed_fresh(self.db, ["regard", "kns", "citilink"])
        exclusions: list[dict] = []
        deals = load_russian_ranked_deals(
            self.db,
            specs_path=self.specs,
            limit=0,
            exclusions=exclusions,
            merges=merges,
            ambiguous=ambiguous,
        )
        return deals, exclusions

    def test_eligible_single_store_is_ranked_without_saving(self) -> None:
        deals, _ = self._deals(
            [_product("citilink", "c1", name="CHUWI GameBook" + SPECS, sku="CHUWI-1", price=192_990)]
        )
        self.assertEqual(len(deals), 1)
        deal = deals[0]
        self.assertEqual(deal.store, "citilink")
        self.assertIsNone(deal.saving_vs_next)
        self.assertIsNone(deal.next_store)
        self.assertEqual(deal.score_breakdown["saving"], 0.0)
        self.assertIsNotNone(deal.history_started_at)

    def test_single_store_must_pass_filters(self) -> None:
        deals, exclusions = self._deals(
            [
                _product("regard", "r16", name='ASUS RV027 RTX 5070 Ti, 16GB DDR5, 16" 1920x1200', sku="RV027", price=258_820),
                _product("regard", "rover", name="ASUS TS412" + SPECS, sku="TS412", price=335_340),
                _product("regard", "rok", name="ASUS S5348" + SPECS, sku="S5348", price=286_660),
            ]
        )
        self.assertEqual([d.cluster_name for d in deals], ["ASUS S5348" + SPECS])
        reasons = {e["cluster_name"].split()[1]: e["reason"] for e in exclusions}
        self.assertEqual(reasons["RV027"], HARD_FILTER_REJECTED)
        self.assertEqual(reasons["TS412"], PRICE_OVER_CAP)

    def test_regional_suffix_is_not_folded_into_cluster(self) -> None:
        base = "MSI Raider 16 HX AI A2XWHG-814XRU"
        deals, exclusions = self._deals(
            [
                _product("regard", "r1", name=base + SPECS, sku="9S7-15M361-814", price=261_090),
                _product("kns", "k1", name=base + SPECS, sku="9S7-15M361-814", price=258_800),
                _product("kns", "k2", name=base + "-wpro" + SPECS, sku="A2XWHG-814XRU-WPRO", price=274_580),
            ]
        )
        self.assertEqual(sorted(d.price for d in deals), [258_800, 274_580])
        cluster = next(d for d in deals if d.price == 258_800)
        self.assertEqual(cluster.store, "kns")
        self.assertEqual(cluster.next_store, "regard")
        self.assertEqual(cluster.next_price, 261_090)
        self.assertEqual(cluster.saving_vs_next, 2_290)
        self.assertFalse(any(e["reason"] == "DUPLICATE_MODEL" for e in exclusions))

    def test_partial_sku_does_not_replace_cluster(self) -> None:
        name = "ASUS ROG Strix G18 G815LR-TT344 90NR0LT1-M00H30" + SPECS
        deals, _ = self._deals(
            [
                _product("kns", "k1", name=name, sku="90NR0LT1-M00H30", price=322_178),
                _product("citilink", "c1", name=name, sku="90NR0LT1-M00H30", price=324_055),
                _product("regard", "r1", name="ASUS G815LR (TT344)" + SPECS, sku="90NR0LT1-M00H30-R", price=320_400),
            ]
        )
        by_price = {d.price: d for d in deals}
        self.assertEqual(set(by_price), {320_400, 322_178})
        cluster = by_price[322_178]
        self.assertEqual(cluster.store, "kns")
        self.assertEqual(cluster.next_store, "citilink")
        self.assertEqual(cluster.next_price, 324_055)
        self.assertEqual(cluster.saving_vs_next, 1_877)
        self.assertIsNone(by_price[320_400].saving_vs_next)

    def test_different_config_with_same_code_is_kept(self) -> None:
        base = "ASUS G614PR 90NR0NJ7-M001J0"
        deals, _ = self._deals(
            [
                _product("regard", "r1", name=base + ' RTX 5070 Ti, 32GB DDR5, 16" 2560x1600', sku="90NR0NJ7-M001J0", price=262_990),
                _product("citilink", "c1", name=base + ' RTX 5070 Ti, 32GB DDR5, 16" 2560x1600', sku="90NR0NJ7-M001J0", price=265_000),
                _product("kns", "k1", name=base + '_64 RTX 5070 Ti, 64GB DDR5, 16" 2560x1600', sku="90NR0NJ7-M001J0_64", price=290_000),
            ]
        )
        self.assertEqual(sorted(d.ram_gb for d in deals), [32, 64])

    def test_same_store_same_sku_counted_once(self) -> None:
        deals, _ = self._deals(
            [
                _product("regard", "a", name="Twin X" + SPECS, sku="TWIN-1", price=250_000),
                _product("regard", "b", name="Twin X" + SPECS, sku="TWIN-1", price=251_000),
            ]
        )
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].price, 250_000)
        self.assertIsNone(deals[0].saving_vs_next)


class ProvenIdentityMergeTests(SingleStoreRankingTests):
    def _named(self, store: str, ext: str, code: str, price: int, *, specs: str = FULL, sku: str | None = None) -> Product:
        return _product(
            store,
            ext,
            name=f"MSI Vector 17 HX AI {code}{specs}",
            sku=sku or f"{store.upper()}-{ext}",
            price=price,
        )

    def test_same_model_two_sellers_keeps_prices_and_history(self) -> None:
        code = "A2XWIG-063XRU"
        deals, _ = self._deals(
            [
                self._named("kns", "k1", code, 305_000),
                self._named("regard", "r1", code, 309_900),
                self._named("citilink", "c1", code, 320_000),
            ]
        )
        self.assertEqual(len(deals), 1)
        deal = deals[0]
        self.assertEqual(deal.store, "kns")
        self.assertEqual(deal.price, 305_000)
        self.assertEqual(deal.next_store, "regard")
        self.assertEqual(deal.next_price, 309_900)
        self.assertEqual(deal.saving_vs_next, 4_900)
        older = _now() - timedelta(days=4)
        with open_db(self.db) as conn:
            citi = conn.execute(
                "SELECT id FROM products WHERE store = ? AND external_id = ?",
                ("citilink", "c1"),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO price_history (product_id, price, available, checked_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(citi["id"]), 270_000, 1, older.isoformat()),
            )
        merges: list[dict] = []
        from russian_deals import load_russian_ranked_deals

        reloaded = load_russian_ranked_deals(
            self.db, specs_path=self.specs, limit=0, merges=merges
        )
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].historical_min, 270_000)
        started = datetime.fromisoformat(reloaded[0].history_started_at)
        self.assertEqual(started, older)
        self.assertEqual(len(merges), 1)
        self.assertEqual(set(merges[0]["stores"]), {"citilink", "kns", "regard"})

    def test_different_cpu_stays_separate(self) -> None:
        other = ' Intel Core Ultra 7 255HX, RTX 5080, 32GB DDR5, 1TB SSD, 17" 2560x1600'
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "A2XWIG-063XRU", 309_900),
                self._named("kns", "k1", "A2XWIG-063XRU", 299_000, specs=other),
            ]
        )
        self.assertEqual(sorted(d.price for d in deals), [299_000, 309_900])
        self.assertTrue(all(d.saving_vs_next is None for d in deals))

    def test_different_ssd_stays_separate(self) -> None:
        other = ' Intel Core Ultra 9 275HX, RTX 5080, 32GB DDR5, 2TB SSD, 17" 2560x1600'
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "A2XWHG-814XRU", 261_000),
                self._named("kns", "k1", "A2XWHG-814XRU", 280_000, specs=other),
            ]
        )
        self.assertEqual(sorted((d.price, d.ssd_gb) for d in deals), [(261_000, 1024), (280_000, 2048)])

    def test_different_ram_stays_separate(self) -> None:
        other = ' Intel Core Ultra 9 275HX, RTX 5080, 64GB DDR5, 1TB SSD, 17" 2560x1600'
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "G815LR-TT344", 250_000),
                self._named("kns", "k1", "G815LR-TT344", 290_000, specs=other),
            ]
        )
        self.assertEqual(sorted(d.ram_gb for d in deals), [32, 64])

    def test_regional_modification_stays_separate(self) -> None:
        ambiguous: list[dict] = []
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "A2XWIG-063XRU", 309_900),
                self._named("kns", "k1", "A2XWIG-063US", 301_000),
            ],
            ambiguous=ambiguous,
        )
        self.assertEqual(len(deals), 2)
        self.assertEqual(ambiguous, [])

    def test_partial_sku_overlap_stays_separate(self) -> None:
        ambiguous: list[dict] = []
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "90NR0LT1-M00H30", 262_990, sku="90NR0LT1-M00H30"),
                self._named("kns", "k1", "90NR0LT1-M00H30-R", 270_000, sku="90NR0LT1-M00H30-R"),
            ],
            ambiguous=ambiguous,
        )
        self.assertEqual(len(deals), 2)
        self.assertEqual(ambiguous, [])

    def test_leading_underscore_sku_is_not_a_partial_match(self) -> None:
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "90NR0LT1-M00H30", 262_990, sku="90NR0LT1-M00H30"),
                self._named("kns", "k1", "_90NR0LT1-M00H30", 270_000, sku="_90NR0LT1-M00H30"),
            ]
        )
        self.assertEqual(len(deals), 2)

    def test_incomplete_cluster_does_not_absorb_another_listing(self) -> None:
        code = "A2XWHG-814XRU"
        merges: list[dict] = []
        deals, _ = self._deals(
            [
                self._named("regard", "r1", code, 261_090, sku="9S7-15M361-814"),
                _product("kns", "k1", name=f"MSI Raider 16 HX AI {code}", sku="9S7-15M361-814", price=258_800),
                self._named("citilink", "c1", code, 270_000, sku="CITI-ONLY-1"),
            ],
            merges=merges,
        )
        self.assertEqual(merges, [])
        self.assertEqual(len(deals), 2)
        cluster = next(d for d in deals if d.saving_vs_next is not None)
        self.assertEqual(cluster.price, 258_800)
        self.assertEqual(cluster.next_store, "regard")
        self.assertEqual(cluster.next_price, 261_090)
        lone = next(d for d in deals if d.store == "citilink")
        self.assertEqual(lone.price, 270_000)
        self.assertIsNone(lone.saving_vs_next)

    def test_missing_specs_are_not_merged(self) -> None:
        ambiguous: list[dict] = []
        merges: list[dict] = []
        deals, _ = self._deals(
            [
                self._named("regard", "r1", "A2XWIG-063XRU", 309_900, specs=SPECS),
                self._named("kns", "k1", "A2XWIG-063XRU", 305_000, specs=SPECS),
            ],
            merges=merges,
            ambiguous=ambiguous,
        )
        self.assertEqual(sorted(d.price for d in deals), [305_000, 309_900])
        self.assertEqual(merges, [])
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["status"], "AMBIGUOUS")
        self.assertIn("A2XWIG-063XRU", ambiguous[0]["shared_identifiers"])
        self.assertIn("cpu", ambiguous[0]["missing"]["left"])
        self.assertIn("ssd_gb", ambiguous[0]["missing"]["left"])

    def test_single_store_model_stays_one_offer(self) -> None:
        merges: list[dict] = []
        deals, _ = self._deals(
            [self._named("citilink", "c1", "CHUWI-GAMEBOOK-1", 192_990)],
            merges=merges,
        )
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].store, "citilink")
        self.assertIsNone(deals[0].saving_vs_next)
        self.assertEqual(merges, [])


class DailyDigestCapTests(unittest.TestCase):
    def test_workflow_uses_config_cap(self) -> None:
        import scripts.generate_daily_digest_workflows as gen

        code = gen.digest_js()
        self.assertIn(f"const MAX_PRICE = {config.MAX_TRACKED_PRICE_RUB};", code)
        self.assertIn("ТОП ДО 330 000 ₽:", code)
        self.assertNotIn("300 000", code)
        repo = json.loads((gen.DIGEST).read_text(encoding="utf-8"))
        node = next(n for n in repo["nodes"] if n["name"] == "Build and send digest")
        self.assertEqual(node["parameters"]["jsCode"], code)
        self.assertEqual(repo["id"], "LmDailyDigest01")
        self.assertNotIn("staticData", repo)
        self.assertEqual(repo["settings"]["timezone"], "Europe/Moscow")
        schedule = next(n for n in repo["nodes"] if n["name"] == "Schedule 09:00 MSK")
        self.assertEqual(
            schedule["parameters"]["rule"]["interval"][0]["expression"],
            "0 9 * * *",
        )
        self.assertTrue(
            all("credentials" not in n for n in repo["nodes"]),
            "digest reads Telegram from env, so import must not carry credential ids",
        )
        self.assertIn("staticData.lastDigestSentDate === dateKey", code)
        self.assertIn("staticData.lastDigestSentDate = dateKey", code)
        self.assertIn("LmDailyDigest01", repo["meta"]["templateNote"])
        self.assertEqual(
            repo["connections"]["Schedule 09:00 MSK"]["main"][0][0]["node"],
            "Mode production",
        )


class ThailandCapTests(unittest.TestCase):
    def test_new_cap_and_unchanged_conversion(self) -> None:
        from thailand.eligibility import max_tracked_price_rub, price_within_cap
        from thailand.formatting import footer_cap
        from thailand.fx import thb_to_rub
        from thailand.models import FxRate

        self.assertEqual(max_tracked_price_rub(), 330_000)
        self.assertTrue(price_within_cap(330_000))
        self.assertFalse(price_within_cap(330_001))
        fx = FxRate(
            source="t",
            currency="THB",
            nominal=1,
            official_rate=2.48146,
            rub_per_thb=2.48146,
            published_date=None,
            fetched_at=datetime.now(timezone.utc),
        )
        self.assertEqual(thb_to_rub(86_990, fx), 215_862)
        self.assertIn("330 000", footer_cap())


if __name__ == "__main__":
    unittest.main()
