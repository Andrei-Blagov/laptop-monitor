"""B1 own observation clocks and B2 approved boundaries; no network."""
from datetime import datetime,timedelta,timezone
import unittest
from dataclasses import replace
from models import Product
from identity_sync import merge_identity_into_cache,identity_from_cache,_stamp_structured_provenance
from product_identity import identity_from_collected_product
from comparison import Offer
from market_snapshot import _field_evidence,_identity_evidence
from laptop_eligibility import evaluate_purchase
from market_contracts import ComparisonPolicy
from deal_ranking import score_screen
import config

AT=datetime(2026,10,18,6,tzinfo=timezone.utc)

def product(**metadata):
    return Product('regard','fixture','https://example.test/fixture','Laptop ASUS RTX 5070 Ti, 32GB RAM, 1TB SSD, 17.3 inch, 2560x1600','FIXTURE-001',230000,True,AT,metadata)

class OwnEvidence(unittest.TestCase):
    def test_successful_own_fields_have_own_capture_time(self):
        identity=identity_from_collected_product(product(gpu='RTX 5070 Ti',ram_gb=32,ssd_gb=1024))
        self.assertEqual(identity.source_identifiers['field_captured_at']['ram_gb'],AT.isoformat())
    def test_incomplete_later_observation_does_not_refresh_cached_fields(self):
        cache={};first=identity_from_collected_product(product(cpu='Core Ultra 9 275HX'))
        merge_identity_into_cache(cache,first)
        later=replace(product(),name='Laptop without specifications',checked_at=AT+timedelta(hours=2),metadata={})
        merge_identity_into_cache(cache,identity_from_collected_product(later))
        kept=identity_from_cache(cache,'regard','fixture')
        self.assertEqual(kept.source_identifiers['field_captured_at']['cpu'],AT.isoformat())
    def test_rejected_conflict_cannot_refresh_the_kept_value(self):
        cache={};first=identity_from_collected_product(product(ram_gb=32))
        merge_identity_into_cache(cache,first)
        other=identity_from_collected_product(replace(product(ram_gb=64),checked_at=AT+timedelta(hours=1)))
        merge_identity_into_cache(cache,other);kept=identity_from_cache(cache,'regard','fixture')
        self.assertEqual(kept.ram_gb,32)
        self.assertEqual(kept.source_identifiers['field_captured_at']['ram_gb'],AT.isoformat())
    def test_undated_legacy_cache_does_not_hide_equal_current_own_title(self):
        p=product();identity=identity_from_collected_product(p);identity.source_identifiers.pop('field_captured_at')
        offer=Offer(p.store,p.external_id,p.name,p.sku,p.price,p.available,p.url)
        specs,_=_field_evidence(offer,identity,listing_observed_at=AT.isoformat(),specs_by_key={(p.store,p.external_id):identity})
        self.assertEqual(specs['ram_gb']['evidence']['method'],'title')
        self.assertEqual(specs['ram_gb']['evidence']['captured_at'],AT.isoformat())
        self.assertNotIn('field_captured_at',identity.source_identifiers)
    def test_undated_conflicting_title_stays_unconfirmed(self):
        p=product(ram_gb=64);identity=identity_from_collected_product(p);identity.source_identifiers.pop('field_captured_at')
        offer=Offer(p.store,p.external_id,p.name,p.sku,p.price,p.available,p.url)
        specs,_=_field_evidence(offer,identity,listing_observed_at=AT.isoformat(),specs_by_key={(p.store,p.external_id):identity})
        self.assertIsNone(specs['ram_gb']['evidence']['captured_at'])
    def test_identity_family_generation_pass_with_own_times(self):
        p=product(brand='ASUS',model_family='ROG Strix G16',manufacturer_generation='G615')
        i=identity_from_collected_product(p);out=_identity_evidence(p,i)
        self.assertEqual(out['manufacturer_generation']['value'],'G615')
        self.assertEqual(out['manufacturer_family']['evidence']['captured_at'],AT.isoformat())
    def test_structured_failure_does_not_stamp_pipeline_time(self):
        i=identity_from_collected_product(product());i.source_identifiers={}
        _stamp_structured_provenance(i,'regard.api')
        self.assertEqual(i.source_identifiers['field_captured_at'],{})

class ApprovedPolicy(unittest.TestCase):
    def test_preferred_seventeen_beats_eighteen(self):
        self.assertGreater(score_screen(17.3, "2560x1600")[0],score_screen(18, "2560x1600")[0])
    def test_same_cap_and_targets(self):
        p=ComparisonPolicy();self.assertEqual(p.max_price_rub,290000)
        self.assertEqual(config.MAX_TRACKED_PRICE_RUB,290000)
        self.assertEqual(dict(p.gpu_targets),config.TARGET_PRICES)
        self.assertEqual(config.TARGET_PRICES['RTX 5080'],280000)
    def test_missing_vram_uses_exact_laptop_gpu_but_conflict_fails(self):
        good=dict(gpu='RTX 5070 Ti Laptop',ram_gb=32,ssd_gb=1000,screen_resolution='2560x1600',screen_inch=17.3)
        self.assertTrue(evaluate_purchase(price_rub=230000,**good).eligible)
        self.assertTrue(evaluate_purchase(price_rub=230000,gpu_vram_gb=12,**good).eligible)
        self.assertFalse(evaluate_purchase(price_rub=230000,gpu_vram_gb=16,**good).eligible)
        self.assertFalse(evaluate_purchase(price_rub=230000,**(good|{'gpu':'RTX 5070 Ti Desktop'})).eligible)
        self.assertFalse(evaluate_purchase(price_rub=230000,**(good|{'gpu':None})).eligible)

    def test_inclusive_cap_no_lower_bound_and_diagonal(self):
        good=dict(gpu='RTX 5070 Ti',ram_gb=32,ssd_gb=1000,screen_resolution='2560x1600',screen_inch=15.6)
        for price in (1,200000,290000):self.assertTrue(evaluate_purchase(price_rub=price,**good).eligible)
        self.assertFalse(evaluate_purchase(price_rub=290001,**good).eligible)
        for diagonal in (None,14,15.5):self.assertFalse(evaluate_purchase(price_rub=230000,**(good|{'screen_inch':diagonal})).eligible)

if __name__=='__main__':unittest.main()
