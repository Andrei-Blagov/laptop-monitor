"""Purchase policy regressions across alert, ranking, BUY and Thailand paths.

Prices are controlled fixtures, not current market quotes. ASUS identifiers and
installed 16 GB reproduce the reported failures; a passing screen isolates RAM.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alerts import (
    EVENT_PRICE_DROP, evaluate_cross_store, evaluate_historical_low,
    evaluate_price_drop, evaluate_target_price, get_current_deals, run_monitor,
)
from buy_opportunity import evaluate_buy_rules
from comparison import Offer, ProductMatch
from deal_ranking import RankedDeal, rank_clusters
from deliver import deliver_alert_events
from identity_sync import save_specs_cache
from laptop_eligibility import evaluate_purchase
from models import Product
from personal_top import russian_side
from product_identity import ProductIdentity
from purchase_recommendation import (
    INSUFFICIENT_DATA, THAILAND_BETTER, build_recommendation,
    format_purchase_recommendation,
)
from storage import (
    count_price_history, count_products, get_all_products, init_db, open_db,
    save_alert_event, save_products,
)
from telegram_sender import FakeTelegramSender
from thailand.comparison import VERDICT_INSUFFICIENT_THAI, country_verdict
from thailand.eligibility import offer_user_facing_eligible
from thailand.formatting import format_thailand_alternatives_message, format_target_model_message
from thailand.models import CrossCountryMatch, FxRate, ThailandOffer
from thailand.scanner import build_cheapest_eligible, build_thai_top, build_value_ranked_eligible
from thailand.target_model import search_collected_offers
from thailand.verification import compute_verification

NOW = datetime.now(timezone.utc)
GOOD = dict(gpu="RTX 5070 Ti", ram_gb=32, ssd_gb=1024, screen_resolution="2560x1600")
BAD = (
    ("small_ram", {"ram_gb": 16}),
    ("24gb_ram", {"ram_gb": 24}),
    ("unknown_ram", {"ram_gb": None}),
    ("512gb_ssd", {"ssd_gb": 512}),
    ("unknown_ssd", {"ssd_gb": None}),
    ("full_hd", {"screen_resolution": "1920x1200"}),
    ("unknown_screen", {"screen_resolution": None}),
    ("marketing_screen_only", {"screen_resolution": "QHD"}),
    ("wrong_gpu", {"gpu": "RTX 5070"}),
    ("unknown_gpu", {"gpu": None}),
    ("non_rtx_number", {"gpu": "Radeon 5080"}),
)


def deal(**fields) -> RankedDeal:
    return RankedDeal(
        score=90, cluster_name="Any manufacturer Eligible test laptop",
        price=220_000, confidence=100, historical_min=220_000,
        history_started_at="2020-01-01T00:00:00+00:00", **(GOOD | fields),
    )


def thai(**fields) -> ThailandOffer:
    return compute_verification(ThailandOffer(**(
        dict(
            store="jib", external_id="1", name="Eligible test laptop",
            url="https://example.test/1", price_thb=80_000, available=True,
            collected_at=NOW, sku="TEST-1", cpu="Intel Core Ultra 9 275HX",
            screen_size_inch=16, availability_confirmed=True,
            availability_status="in_stock", gpu_source="structured_api",
        ) | GOOD | fields
    )))


def fx() -> FxRate:
    return FxRate(source="test", currency="THB", nominal=1, official_rate=2.5,
                  rub_per_thb=2.5, published_date=None, fetched_at=NOW)


class UnifiedHardwareTests(unittest.TestCase):
    def test_unknown_and_below_minimum_never_buy_or_recommend(self):
        for label, fields in BAD:
            with self.subTest(label=label):
                d = deal(**fields)
                self.assertIsNone(evaluate_buy_rules(d))
                rec = build_recommendation(d)
                self.assertEqual(rec.verdict, INSUFFICIENT_DATA)
                self.assertIsNone(rec.buy_level)

    def test_installed_ram_not_upgrade_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            p = Product("regard", "1", "https://example.test", "Any laptop RTX 5070 Ti, RAM up to 64GB DDR5, 1TB SSD, 2560x1600", "TEST", 220_000, True, NOW)
            save_products([p], db)
            self.assertEqual(run_monitor(db, specs_path=Path(tmp) / "missing.json").created, [])

    def test_price_and_ssd_boundary_representations(self):
        for gpu in ("RTX 5070 Ti", "RTX 5080"):
            for ssd in (1000, 1024, 2048):
                for price, ok in ((330_000, True), (330_001, False), (0, False), (None, False), (True, False)):
                    with self.subTest(gpu=gpu, ssd=ssd, price=price):
                        self.assertEqual(evaluate_purchase(price_rub=price, **(GOOD | dict(gpu=gpu, ssd_gb=ssd))).eligible, ok)

    def test_any_manufacturer_passes_without_personal_top_membership(self):
        self.assertIsNotNone(evaluate_buy_rules(deal()))
        offers = [thai(brand="Another manufacturer")]
        self.assertEqual(len(build_thai_top(offers, fx=fx())), 1)


class RussianPathTests(unittest.TestCase):
    def _scenario(self, fields, title="Controlled test laptop"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db, specs_path = Path(temporary.name) / "test.db", Path(temporary.name) / "specs.json"
        products = [Product(store, "1", f"https://example.test/{store}", title, "TEST-1", price, True, NOW)
                    for store, price in (("regard", 220_000), ("kns", 240_000))]
        save_products([replace(p, price=300_000, checked_at=NOW-timedelta(days=1)) for p in products], db)
        save_products(products, db)
        identities = {
            (p.store, p.external_id): ProductIdentity(
                store=p.store, external_id=p.external_id, name=p.name, sku=p.sku,
                price=p.price, available=True, url=p.url, **(GOOD | fields),
            ) for p in products
        }
        save_specs_cache({f"{s}:{e}": i.to_dict() for (s, e), i in identities.items()}, specs_path)
        rows = get_all_products(db)
        match = ProductMatch(normalized_sku="TEST-1", display_sku="TEST-1", name=title,
                             offers=[Offer(store=p["store"], external_id=p["external_id"], name=p["name"],
                                           sku=p["sku"], price=p["price"], available=True, url=p["url"], product_id=p["id"]) for p in rows])
        return db, specs_path, rows, identities, match

    def test_every_russian_alert_and_ranking_path_uses_same_gate(self):
        for label, fields in (("eligible", {}), *BAD):
            with self.subTest(label=label):
                db, specs, rows, identities, match = self._scenario(fields)
                expected = label == "eligible"
                self.assertEqual(bool(rank_clusters([match], specs_by_key=identities)), expected)
                row = next(p for p in rows if p["store"] == "regard")
                identity = identities[(row["store"], row["external_id"])]
                with open_db(db) as conn:
                    self.assertEqual(evaluate_price_drop(conn, row, identity=identity) is not None, expected)
                    self.assertEqual(evaluate_historical_low(conn, row, identity=identity) is not None, expected)
                    self.assertEqual(evaluate_target_price(conn, row, "RTX 5070 Ti", identity=identity) is not None, expected)
                    self.assertEqual(bool(evaluate_cross_store(conn, [match], specs_by_key=identities)), expected)
                    self.assertEqual(count_products(conn), 2)
                    self.assertEqual(count_price_history(conn), 4)
                current = get_current_deals(db, specs_path=specs)
                self.assertEqual(bool(current.target_price), expected)
                self.assertEqual(bool(current.cross_store), expected)

    def test_reported_asus_16gb_no_alert_and_history_retained(self):
        for sku in ("G614PR-RV027", "G615LR-S5162"):
            with self.subTest(sku=sku):
                db, specs, rows, identities, match = self._scenario({"ram_gb": 16}, f"ASUS ROG Strix G16 {sku}")
                self.assertEqual(run_monitor(db, specs_path=specs).created, [])
                self.assertEqual(rank_clusters([match], specs_by_key=identities), [])
                self.assertIsNone(evaluate_buy_rules(replace(deal(ram_gb=16), cluster_name=match.name)))
                with open_db(db) as conn:
                    self.assertEqual(count_products(conn), 2)
                    self.assertEqual(count_price_history(conn), 4)

    def test_pending_legacy_event_blocked_then_can_send_after_confirmation(self):
        db, specs, rows, identities, _ = self._scenario({"ram_gb": None})
        p = rows[0]
        with open_db(db) as conn:
            init_db(conn)
            save_alert_event(conn, event_type=EVENT_PRICE_DROP, product_id=p["id"], dedupe_key="legacy",
                             old_price=300_000, new_price=p["price"], metadata={"name": p["name"]})
        sender = FakeTelegramSender()
        result = deliver_alert_events(db, specs_path=specs, sender=sender, dry_run=True)
        self.assertEqual(result["stats"]["excluded_eligibility"], 1)
        self.assertEqual(result["messages"], [])
        self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["sent"], 0)
        self.assertEqual(sender.sent, [])
        for identity in identities.values():
            identity.ram_gb = 32
        save_specs_cache({f"{s}:{e}": i.to_dict() for (s, e), i in identities.items()}, specs)
        self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["sent"], 1)

    def test_same_store_listings_cannot_create_cross_store_saving(self):
        db, _, _, identities, match = self._scenario({})
        match.offers[1] = replace(match.offers[1], store=match.offers[0].store, external_id="2")
        identities[(match.offers[0].store, "2")] = next(iter(identities.values()))
        with open_db(db) as conn:
            self.assertEqual(evaluate_cross_store(conn, [match], specs_by_key=identities), [])

    def test_personal_exact_reference_also_requires_minimum_ssd(self):
        model = {"exact_skus": ("TEST-1",), "model": "Test"}
        title = "Test Intel Core Ultra 9 275HX RTX 5070 Ti, 32GB DDR5, 512GB SSD, 16\" 2560x1600"
        db, _, rows, identities, _ = self._scenario({"ssd_gb": 512}, title)
        self.assertFalse(russian_side(model, rows, specs_by_key=identities)["found"])


class ThailandPathTests(unittest.TestCase):
    def test_top_cheapest_and_value_rank_all_fail_closed(self):
        for label, fields in (("eligible", {}), *BAD):
            with self.subTest(label=label):
                offer = thai(**fields)
                expected = label == "eligible"
                self.assertEqual(offer_user_facing_eligible(offer, fx=fx())[0], expected)
                for build in (build_thai_top, build_cheapest_eligible, build_value_ranked_eligible):
                    self.assertEqual(bool(build([offer], fx=fx())), expected)

    def test_exact_identifier_does_not_bypass_hardware_or_recommend_cheaper(self):
        target = dict(model_codes=["TEST-1"], cluster_name="Test", price=220_000, **GOOD)
        for label, fields in (("eligible", {}), *BAD):
            with self.subTest(label=label):
                offer = thai(price_thb=60_000, **fields)
                search = search_collected_offers(target, [offer], fx=fx())
                # Preserve discovery and diagnostics even when not purchasable.
                row = search["exact_matches"][0]
                self.assertEqual(row["purchase_eligible"], label == "eligible")
                rec = build_recommendation(deal(), search)
                self.assertEqual(rec.verdict == THAILAND_BETTER, label == "eligible")
                if label != "eligible":
                    self.assertFalse(rec.country_comparison_trusted)
                    self.assertNotIn("Таиланд дешевле на", format_target_model_message(target, search))

    def test_cached_legacy_alternatives_are_rechecked(self):
        top = build_thai_top([thai()], fx=fx())
        self.assertTrue(format_thailand_alternatives_message(top))
        for label, fields in BAD:
            with self.subTest(label=label):
                old = dict(top[0]) | fields
                self.assertEqual(format_thailand_alternatives_message([old]), "")
                text = format_purchase_recommendation(deal(), build_recommendation(deal()), alternatives=[old])
                self.assertNotIn("Альтернативы в Таиланде:", text)

    def test_pre_upgrade_search_rows_need_fresh_purchase_confirmation(self):
        target = dict(model_codes=["TEST-1"], cluster_name="Test", price=220_000, **GOOD)
        search = search_collected_offers(target, [thai(price_thb=60_000)], fx=fx())
        search["exact_matches"][0].pop("purchase_eligible")
        self.assertFalse(build_recommendation(deal(), search).country_comparison_trusted)
        self.assertNotIn("Таиланд дешевле на", format_target_model_message(target, search))

    def test_thai_price_cap_uses_converted_rubles_inclusively(self):
        for price, eligible in ((132_000, True), (132_001, False)):
            with self.subTest(price_thb=price):
                offer = thai(price_thb=price)
                self.assertEqual(offer_user_facing_eligible(offer, fx=fx())[0], eligible)
                self.assertEqual(bool(build_thai_top([offer], fx=fx())), eligible)

    def test_cross_country_verdict_cannot_recommend_16gb_offer(self):
        match = CrossCountryMatch("EXACT", "Test", thai(ram_gb=16), 220_000, 80_000, 200_000,
                                  delta_rub=-20_000, delta_percent=-20_000/220_000)
        self.assertEqual(country_verdict(match, fx=fx()).verdict, VERDICT_INSUFFICIENT_THAI)

    def test_fx_and_trust_guards_remain_required(self):
        offer = thai()
        self.assertFalse(offer_user_facing_eligible(offer, fx=None)[0])
        self.assertFalse(offer_user_facing_eligible(offer, fx=replace(fx(), stale=True))[0])
        self.assertFalse(offer_user_facing_eligible(thai(availability_confirmed=False), fx=fx())[0])
        self.assertFalse(offer_user_facing_eligible(thai(marketplace=True, price_verified_for_variant=False), fx=fx())[0])


if __name__ == "__main__":
    unittest.main()
