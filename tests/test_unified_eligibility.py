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
from comparison import Offer, ProductMatch, match_products
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
    save_alert_event, save_products, get_alert_events, get_delivery,
    count_unsent_alert_events, get_all_identifiers, upsert_product_identifier,
)
from telegram_sender import FakeTelegramSender, SendResult
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
                price=p.price, available=True, url=p.url, cpu="Core Ultra 9 275HX", screen_size_inch=16, **(GOOD | fields),
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

    def _cache(self, identities, specs):
        save_specs_cache({f"{s}:{e}": i.to_dict() for (s, e), i in identities.items()}, specs)

    def _cross_event(self):
        db, specs, rows, identities, match = self._scenario({})
        with open_db(db) as conn:
            events = evaluate_cross_store(conn, [match], specs_by_key=identities)
        self.assertEqual(len(events), 1)
        return db, specs, rows, identities, events[0]

    def _rescan(self, db, row, **changes):
        values = {key: row.get(key) for key in ("store", "external_id", "url", "name", "sku", "price", "available")}
        save_products([Product(**(values | changes), checked_at=datetime.now(timezone.utc))], db)

    def _assert_skipped(self, db, event, specs, reason):
        sender = FakeTelegramSender()
        dry = deliver_alert_events(db, specs_path=specs, sender=sender, dry_run=True)
        self.assertEqual(dry["stats"]["excluded_stale"], 1)
        self.assertEqual(dry["messages"], [])
        with open_db(db) as conn:
            self.assertEqual(count_unsent_alert_events(conn, channel="telegram", destination="default", max_attempts=5), 1)
        result = deliver_alert_events(db, specs_path=specs, sender=sender)
        self.assertEqual(result["stats"]["excluded_stale"], 1)
        self.assertEqual(sender.calls, 0)
        with open_db(db) as conn:
            delivery = get_delivery(conn, alert_event_id=event["id"], channel="telegram", destination="default")
            self.assertEqual(delivery["status"], "skipped")
            self.assertEqual(delivery["last_error"], reason)
            self.assertEqual(len(get_alert_events(conn)), 1)
            self.assertEqual(count_products(conn), 2)
            self.assertGreaterEqual(count_price_history(conn), 4)
        again = deliver_alert_events(db, specs_path=specs, sender=sender)
        self.assertEqual(again["stats"]["unsent_alert_events"], 0)

    def test_same_sku_different_cpu_never_cross_store(self):
        self._assert_different_config("cpu", "Core Ultra 9 285HX")

    def test_same_sku_different_ssd_never_cross_store(self):
        self._assert_different_config("ssd_gb", 2048)

    def _assert_different_config(self, field, value):
        db, specs, rows, identities, match = self._scenario({})
        setattr(identities[("kns", "1")], field, value)
        self._cache(identities, specs)
        with open_db(db) as conn:
            self.assertEqual(evaluate_cross_store(conn, [match], specs_by_key=identities), [])
        self.assertEqual(get_current_deals(db, specs_path=specs).cross_store, [])
        result = run_monitor(db, specs_path=specs)
        self.assertFalse(any(e["event_type"] == "CROSS_STORE_SAVING" for e in result.created))

    def test_every_config_field_must_be_confirmed_and_equal(self):
        for field, wrong in (
            ("cpu", "Core Ultra 9"), ("gpu", "RTX 5080"), ("ram_gb", 64),
            ("ssd_gb", 2048), ("screen_size_inch", 18), ("screen_resolution", "2560x1440"),
        ):
            for value in (wrong, None):
                with self.subTest(field=field, value=value):
                    self._assert_different_config(field, value)

    def test_equivalent_catalog_spellings_pass_existing_normalizers(self):
        db, specs, rows, identities, match = self._scenario({})
        identities[("kns", "1")].cpu = "Intel Core Ultra 9 275HX"
        identities[("kns", "1")].gpu = "NVIDIA GeForce RTX 5070 Ti Laptop GPU"
        identities[("kns", "1")].screen_resolution = "2560 × 1600"
        self._cache(identities, specs)
        with open_db(db) as conn:
            self.assertEqual(len(evaluate_cross_store(conn, [match], specs_by_key=identities)), 1)
        self.assertEqual(len(get_current_deals(db, specs_path=specs).cross_store), 1)

    def test_cheaper_incompatible_offer_does_not_hide_valid_pair(self):
        db, specs, rows, identities, match = self._scenario({})
        third = replace(match.offers[0], store="andpro", external_id="3", price=210_000, product_id=None)
        identities[("andpro", "3")] = replace(identities[("regard", "1")], store="andpro", external_id="3", cpu="Core Ultra 9 285HX")
        match.offers.append(third)
        with open_db(db) as conn:
            events = evaluate_cross_store(conn, [match], specs_by_key=identities)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["cheapest_store"], "regard")
        self.assertEqual({o["store"] for o in events[0]["metadata"]["offers"]}, {"regard", "kns"})

    def test_stale_price_events_terminal_and_history_preserved(self):
        for event_type in ("PRICE_DROP", "NEW_HISTORICAL_LOW", "TARGET_PRICE"):
            with self.subTest(event_type=event_type):
                db, specs, rows, identities, _ = self._scenario({})
                p = rows[0]
                with open_db(db) as conn:
                    event = save_alert_event(conn, event_type=event_type, product_id=p["id"],
                                             dedupe_key="old", old_price=300_000, new_price=p["price"])
                self._rescan(db, p, price=p["price"]+1000)
                self._assert_skipped(db, event, specs, "stale_price")

    def test_compared_price_change_invalidates_cross_event(self):
        db, specs, rows, identities, event = self._cross_event()
        self._rescan(db, next(p for p in rows if p["store"] == "kns"), price=250_000)
        self._assert_skipped(db, event, specs, "stale_compared_offer")

    def test_compared_offer_unavailable_invalidates_cross_event(self):
        db, specs, rows, identities, event = self._cross_event()
        self._rescan(db, next(p for p in rows if p["store"] == "kns"), available=False)
        self._assert_skipped(db, event, specs, "unavailable_offer")

    def test_same_sku_compared_cpu_change_defers_then_expires(self):
        db, specs, rows, identities, event = self._cross_event()
        identities[("kns", "1")].cpu = "Core Ultra 9 285HX"
        self._cache(identities, specs)
        sender = FakeTelegramSender()
        self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["excluded_eligibility"], 1)
        self.assertEqual(sender.calls, 0)
        with open_db(db) as conn:
            conn.execute("UPDATE alert_events SET created_at=?", ((NOW-timedelta(hours=25)).isoformat(),))
        self._assert_skipped(db, event, specs, "expired_event")

    def test_characteristics_corrected_without_price_change_keep_event(self):
        db, specs, rows, identities, event = self._cross_event()
        identities[("kns", "1")].cpu = None
        self._cache(identities, specs)
        sender = FakeTelegramSender()
        self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["sent"], 0)
        identities[("kns", "1")].cpu = "Intel Core Ultra 9 275HX"
        self._cache(identities, specs)
        # A subsequent snapshot/enrichment has a new timestamp, the same prices.
        with open_db(db) as conn:
            conn.execute("UPDATE products SET last_checked_at=?", (datetime.now(timezone.utc).isoformat(),))
        self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["sent"], 1)
        with open_db(db) as conn:
            self.assertEqual(len(get_alert_events(conn)), 1)
            self.assertEqual(count_price_history(conn), 4)

    def test_missing_specs_cannot_accumulate_forever(self):
        db, specs, rows, identities, _ = self._scenario({"ram_gb": None})
        with open_db(db) as conn:
            event = save_alert_event(conn, event_type=EVENT_PRICE_DROP, product_id=rows[0]["id"],
                                     dedupe_key="unconfirmed", old_price=300_000, new_price=rows[0]["price"],
                                     created_at=(NOW-timedelta(hours=25)).isoformat())
        self._assert_skipped(db, event, specs, "expired_event")

    def test_old_snapshot_is_not_current_even_if_price_unchanged(self):
        db, specs, rows, identities, event = self._cross_event()
        with open_db(db) as conn:
            conn.execute("UPDATE products SET last_checked_at=? WHERE store='kns'", ((NOW-timedelta(hours=4)).isoformat(),))
        self._assert_skipped(db, event, specs, "stale_snapshot")

    def test_legacy_cross_event_resolves_exact_saved_urls(self):
        db, specs, rows, identities, event = self._cross_event()
        meta = dict(event["metadata"])
        for key in ("cheapest_external_id", "other_external_id", "offers"):
            meta.pop(key, None)
        import json
        with open_db(db) as conn:
            conn.execute("UPDATE alert_events SET metadata_json=?", (json.dumps(meta),))
        sender = FakeTelegramSender()
        self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["sent"], 1)

    def test_price_changed_during_429_wait_not_retried(self):
        db, specs, rows, identities, event = self._cross_event()
        sender = FakeTelegramSender(responses=[SendResult(ok=False, status_code=429, retry_after=1)])
        def change_price(_seconds):
            with open_db(db) as conn:
                conn.execute("UPDATE products SET price=250000 WHERE store='kns'")
        result = deliver_alert_events(db, specs_path=specs, sender=sender, sleep=change_price)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(result["stats"]["excluded_stale"], 1)
        self.assertEqual(result["stats"]["sent"], 0)
        with open_db(db) as conn:
            self.assertEqual(count_unsent_alert_events(conn, channel="telegram", destination="default", max_attempts=5), 0)
            skipped = get_delivery(conn, alert_event_id=event["id"], channel="telegram", destination="default")
            self.assertEqual(skipped["status"], "skipped")

    def test_stale_cross_removed_from_failed_group_valid_drop_still_delivered(self):
        db, specs, rows, identities, cross = self._cross_event()
        p = next(r for r in rows if r["store"] == "regard")
        with open_db(db) as conn:
            drop = save_alert_event(conn, event_type=EVENT_PRICE_DROP, product_id=p["id"],
                                    dedupe_key="drop", old_price=300_000, new_price=p["price"], metadata={"name": p["name"]})
        sender = FakeTelegramSender(fail_times=1)
        first = deliver_alert_events(db, specs_path=specs, sender=sender)
        self.assertEqual(first["stats"]["failed"], 1)
        with open_db(db) as conn:
            conn.execute("UPDATE products SET price=250000 WHERE store='kns'")
        second = deliver_alert_events(db, specs_path=specs, sender=sender)
        self.assertEqual(second["stats"]["sent"], 1)
        self.assertNotIn("CROSS", sender.sent[-1])
        with open_db(db) as conn:
            rejected = get_delivery(conn, alert_event_id=cross["id"], channel="telegram", destination="default")
            accepted = get_delivery(conn, alert_event_id=drop["id"], channel="telegram", destination="default")
            self.assertEqual(rejected["status"], "skipped")
            self.assertEqual(accepted["status"], "sent")
            self.assertEqual(accepted["attempts"], 2)
            self.assertEqual(len(get_alert_events(conn)), 2)


    def test_strong_identifier_comparison_delivers_and_removed_link_is_skipped(self):
        for removed in (False, True):
            with self.subTest(removed=removed):
                db, specs, rows, identities, _ = self._scenario({})
                for row in rows:
                    self._rescan(db, row, sku=f"REGIONAL-{row['store']}")
                rows = get_all_products(db)
                with open_db(db) as conn:
                    for row in rows:
                        upsert_product_identifier(conn, product_id=row["id"],
                                                  identifier_type="manufacturer_part_number", value="SHARED-123456",
                                                  normalized_value="SHARED-123456", source="test")
                comparison = match_products(rows, get_all_identifiers(db), specs_by_key=identities)
                self.assertEqual(comparison.matches[0].match_method, "strong_identifier")
                with open_db(db) as conn:
                    events = evaluate_cross_store(conn, comparison.matches, specs_by_key=identities)
                    self.assertEqual(len(events), 1)
                    if removed:
                        conn.execute("DELETE FROM product_identifiers")
                if removed:
                    self._assert_skipped(db, events[0], specs, "comparison_identity_changed")
                else:
                    sender = FakeTelegramSender()
                    self.assertEqual(deliver_alert_events(db, specs_path=specs, sender=sender)["stats"]["sent"], 1)

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
