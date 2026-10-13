"""One six-field comparison for CROSS-STORE, rank savings, display, and BUY."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from alerts import evaluate_cross_store, get_current_deals, run_monitor
from buy_opportunity import evaluate_buy_rules
from comparison import Offer, ProductMatch, comparable_offer_groups
from deal_ranking import (
    RankedDeal, effective_deal_score, format_top_deals_message, rank_clusters,
    score_cross_store_saving, score_offer,
)
from identity_sync import save_specs_cache
from models import Product
from product_identity import ProductIdentity
from russian_deals import load_russian_ranked_deals
from storage import (
    count_price_history, count_products, create_pipeline_run, init_db,
    insert_store_run, open_db, save_products,
)
from thailand.job_models import deal_to_russian_context, russian_context_to_deal

NOW = datetime.now(timezone.utc)
GOOD = dict(cpu="Core Ultra 9 275HX", gpu="RTX 5070 Ti", ram_gb=32,
            ssd_gb=1024, screen_size_inch=16, screen_resolution="2560x1600")


def scenario(**different):
    offers = [Offer(store=store, external_id=store, name="Controlled laptop", sku="SAME-SKU",
                    price=price, available=True, url=f"https://example.test/{store}", product_id=pid)
              for store, price, pid in (("regard", 220_000, 1), ("kns", 250_000, 2))]
    identities = {(o.store, o.external_id): ProductIdentity(
        store=o.store, external_id=o.external_id, sku=o.sku, name=o.name, price=o.price,
        available=o.available, url=o.url, **(GOOD | (different if o.store == "kns" else {})),
    ) for o in offers}
    match = ProductMatch("SAME-SKU", "SAME-SKU", "Controlled laptop", offers=offers)
    return match, identities


def ranked(match, identities):
    return rank_clusters([match], specs_by_key=identities,
                         historical_mins={1: 210_000, 2: 230_000})


class SavingComparabilityTests(unittest.TestCase):
    def assert_no_saving(self, **different):
        match, identities = scenario(**different)
        deals = ranked(match, identities)
        self.assertEqual(len(deals), 1)
        deal = deals[0]
        self.assertEqual(deal.store, "regard")
        self.assertEqual(deal.price, 220_000)
        self.assertIsNone(deal.saving_vs_next)
        self.assertIsNone(deal.next_store)
        self.assertIsNone(deal.next_price)
        self.assertFalse(deal.saving_verified)
        self.assertEqual(deal.score_breakdown["saving"], 0)
        self.assertNotIn("Экономия", format_top_deals_message(deals))
        baseline = ranked(replace(match, offers=match.offers[:1]), identities)[0]
        self.assertEqual(deal.score, baseline.score)
        self.assertEqual(deal.cpu, baseline.cpu)
        self.assertEqual(deal.ssd_gb, baseline.ssd_gb)
        with tempfile.TemporaryDirectory() as tmp, open_db(Path(tmp)/"test.db") as conn:
            init_db(conn)
            self.assertEqual(evaluate_cross_store(conn, [match], specs_by_key=identities), [])
        return deal

    def test_same_sku_different_cpu(self):
        self.assert_no_saving(cpu="Core Ultra 9 285HX")

    def test_same_sku_different_ssd(self):
        self.assert_no_saving(ssd_gb=2048)

    def test_each_missing_field_never_proves_a_comparison(self):
        for field in GOOD:
            with self.subTest(field=field):
                self.assert_no_saving(**{field: None})

    def test_each_different_field_never_proves_a_comparison(self):
        for field, value in dict(cpu="Core Ultra 9 285HX", gpu="RTX 5080", ram_gb=64,
                                 ssd_gb=2048, screen_size_inch=18,
                                 screen_resolution="2560x1440").items():
            with self.subTest(field=field):
                self.assert_no_saving(**{field: value})

    def test_full_config_agrees_with_cross_store_score_and_display(self):
        match, identities = scenario()
        deal = ranked(match, identities)[0]
        self.assertEqual(deal.saving_vs_next, 30_000)
        self.assertEqual(deal.next_store, "kns")
        self.assertEqual(deal.next_price, 250_000)
        self.assertTrue(deal.saving_verified)
        self.assertEqual(deal.score_breakdown["saving"], 4)
        self.assertIn("Экономия vs KNS: 30 000 ₽", format_top_deals_message([deal]))
        with tempfile.TemporaryDirectory() as tmp, open_db(Path(tmp)/"test.db") as conn:
            init_db(conn)
            # Persist offers to satisfy event foreign keys.
            for o in match.offers:
                from storage import upsert_product
                upsert_product(conn, Product(o.store, o.external_id, o.url, o.name,
                                             o.sku, o.price, True, NOW))
            events = evaluate_cross_store(conn, [match], specs_by_key=identities)
        self.assertEqual(len(events), 1)
        meta = events[0]["metadata"]
        self.assertEqual((meta["cheapest_store"], meta["other_store"], meta["difference"]),
                         (deal.store, deal.next_store, deal.saving_vs_next))

    def test_normalized_spellings_use_the_same_matcher(self):
        match, identities = scenario(cpu="Intel Core Ultra 9 275HX", gpu="NVIDIA GeForce RTX 5070 Ti Laptop GPU",
                                     screen_resolution="2560 × 1600")
        self.assertEqual(len(comparable_offer_groups(match, identities)), 1)
        self.assertEqual(ranked(match, identities)[0].saving_vs_next, 30_000)

    def test_single_store_model_remains_ranked(self):
        match, identities = scenario()
        match.offers = match.offers[:1]
        deal = ranked(match, identities)[0]
        self.assertIsNone(deal.saving_vs_next)
        self.assertEqual(deal.score_breakdown["saving"], 0)
        self.assertEqual(deal.price, 220_000)

    def test_incomplete_own_cpu_still_passes_hardware_minimum(self):
        match, identities = scenario()
        identities[("regard", "regard")].cpu = None
        deal = ranked(match, identities)[0]
        self.assertIsNone(deal.cpu)
        self.assertIsNone(deal.saving_vs_next)
        self.assertEqual(deal.price, 220_000)

    def test_eligible_partner_chosen_when_cheapest_fails_minimum(self):
        match, identities = scenario()
        identities[("regard", "regard")].ram_gb = 16
        deal = ranked(match, identities)[0]
        self.assertEqual(deal.store, "kns")
        self.assertEqual(deal.ram_gb, 32)
        self.assertIsNone(deal.saving_vs_next)

    def test_skip_incompatible_next_store_use_later_comparable_offer(self):
        match, identities = scenario(cpu="Core Ultra 9 285HX")
        extra = replace(match.offers[1], store="andpro", external_id="a", price=260_000, product_id=3)
        identities[("andpro", "a")] = replace(identities[("regard", "regard")], store="andpro", external_id="a")
        match.offers.append(extra)
        deal = ranked(match, identities)[0]
        self.assertEqual(deal.next_store, "andpro")
        self.assertEqual(deal.saving_vs_next, 40_000)

    def test_same_store_stale_unavailable_and_over_cap_no_saving(self):
        match, identities = scenario()
        for peer in (replace(match.offers[1], available=False), replace(match.offers[1], price=330_001),
                     replace(match.offers[1], store="regard")):
            with self.subTest(peer=peer):
                deals = ranked(replace(match, offers=[match.offers[0], peer]), identities)
                self.assertIsNone(deals[0].saving_vs_next)
        self.assertIsNone(rank_clusters([match], specs_by_key=identities, fresh_stores={"regard"})[0].saving_vs_next)

    def test_unverified_integer_cannot_add_score_or_display_saving(self):
        match, identities = scenario()
        verified = ranked(match, identities)[0]
        self.assertEqual(score_cross_store_saving(30_000)[0], 0)
        self.assertEqual(score_cross_store_saving(30_000, verified=True)[0], 4)
        self.assertEqual(score_offer(price=220_000, gpu="RTX 5070 Ti", saving_vs_next=30_000).breakdown["saving"], 0)
        old = replace(verified, saving_verified=False)
        self.assertEqual(effective_deal_score(old), verified.score - 4)
        self.assertNotIn("Экономия", format_top_deals_message([old]))

    def test_bonus_removal_affects_top_and_buy_at_boundary(self):
        match, identities = scenario()
        confirmed = ranked(match, identities)[0]
        changed = self.assert_no_saving(cpu="Core Ultra 9 285HX")
        self.assertEqual(confirmed.score-changed.score, 4)
        competitor_match = replace(match, normalized_sku="OTHER", name="Another laptop",
                                   offers=[replace(match.offers[0], sku="OTHER", price=218_000)])
        before = rank_clusters([match, competitor_match], specs_by_key=identities,
                               historical_mins={1: 210_000, 2: 230_000})
        identities[("kns", "kns")].cpu = "Core Ultra 9 285HX"
        after = rank_clusters([match, competitor_match], specs_by_key=identities,
                              historical_mins={1: 210_000, 2: 230_000})
        self.assertEqual(before[0].cluster_name, "Controlled laptop")
        self.assertEqual(after[0].cluster_name, "Another laptop")
        boundary = replace(confirmed, score=76, price=240_000, historical_min=235_000)
        self.assertIn("B", evaluate_buy_rules(boundary).rule_ids)
        self.assertIsNone(evaluate_buy_rules(replace(boundary, saving_verified=False)))

    def test_async_round_trip_preserves_proof_and_legacy_score_drops_bonus(self):
        match, identities = scenario()
        deal = ranked(match, identities)[0]
        ctx = deal_to_russian_context(deal)
        restored = russian_context_to_deal(ctx)
        self.assertEqual(effective_deal_score(restored), deal.score)
        self.assertEqual(restored.saving_vs_next, 30_000)
        self.assertTrue(restored.saving_verified)
        for field in ("score_breakdown", "saving_verified", "saving_vs_next", "next_store", "next_price"):
            ctx.pop(field)
        legacy = russian_context_to_deal(ctx)
        self.assertEqual(legacy.score, deal.score-4)
        self.assertIsNone(legacy.saving_vs_next)

    def test_unverified_zero_bonus_is_not_subtracted_twice(self):
        deal = RankedDeal(score=70, saving_vs_next=30_000, score_breakdown={"saving": 0})
        self.assertEqual(effective_deal_score(deal), 70)

    def test_real_loader_pipeline_cross_store_and_history_agree(self):
        for different in ({}, {"cpu": "Core Ultra 9 285HX"}, {"cpu": None}, {"ssd_gb": 2048}):
            with self.subTest(different=different), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                db, cache = root/"copy.db", root/"product_specs.json"
                match, identities = scenario(**different)
                save_products([Product(o.store, o.external_id, o.url, o.name, o.sku,
                                       o.price, True, NOW) for o in match.offers], db)
                save_specs_cache({f"{s}:{e}": spec.to_dict() for (s,e),spec in identities.items()}, cache)
                with open_db(db) as conn:
                    run = create_pipeline_run(conn, started_at=NOW.isoformat(), status="success")
                    for store in ("regard", "kns"):
                        insert_store_run(conn, pipeline_run_id=run, store=store, attempted_at=NOW.isoformat(),
                                         finished_at=NOW.isoformat(), status="ok", products_count=1)
                    before = (count_products(conn), count_price_history(conn))
                deals = load_russian_ranked_deals(db, specs_path=cache, limit=0)
                self.assertTrue(deals)
                with_saving = [d for d in deals if d.saving_vs_next is not None]
                current = get_current_deals(db, specs_path=cache)
                events = [e for e in run_monitor(db, specs_path=cache).created if e["event_type"] == "CROSS_STORE_SAVING"]
                self.assertEqual(bool(with_saving), not different)
                self.assertEqual(bool(current.cross_store), not different)
                self.assertEqual(bool(events), not different)
                if with_saving:
                    self.assertEqual(with_saving[0].saving_vs_next, events[0]["metadata"]["difference"])
                with open_db(db) as conn:
                    self.assertEqual((count_products(conn), count_price_history(conn)), before)
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
