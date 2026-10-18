"""Offline package D implementation acceptance using A fixtures and actual B exports."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from cross_market_comparison import Expense, compare_markets, comparison_view, price_gap
from cross_market_matching import compare_configuration, normalized_field
from market_contracts import ComparisonPolicy, ContractError, fx_observation, russian_snapshot, thai_snapshot
from thai_market_ranking import build_thai_rankings, value_score
from thailand.scoring import international_value_score

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
AT = (NOW - timedelta(minutes=10)).isoformat()
CORE = {'cpu': 'Intel Core Ultra 9 275HX', 'gpu': 'RTX 5070 Ti Laptop',
        'ram_gb': 32, 'ssd_gb': 1024, 'screen_inch': 16, 'screen_resolution': '2560x1600'}


def evidence(source, eid, at=AT, method='structured_api'):
    return {'source': source, 'external_id': eid, 'captured_at': at, 'method': method}


def offer(market='TH', eid='th-one', *, code='G615LR-TEST17', brand='ASUS', price=None,
          source=None, specs=None, extras=None):
    source = source or ('jib' if market == 'TH' else 'regard')
    values = {**CORE, **(specs or {})}
    r = {'offer_id': eid, 'external_id': eid, 'source': source, 'store': source,
         'manufacturer_part_number': code, 'sku_candidates': [code], 'mpn_candidates': [],
         'price_thb': 80000 if price is None else price, 'public_price_rub': 230000 if price is None else price,
         'price_checked_at': AT, 'availability_checked_at': AT, 'observed_at': AT,
         'availability_state': 'in_stock', 'stock_state': 'in_stock', 'availability_confirmed': True,
         'availability_evidence': {**evidence(source, eid, method='store_listing'), 'available': True},
         'seller_trust': 'direct_retailer', 'price_verified_for_variant': True,
         'specs': {k: {'value': v, 'evidence': evidence(source, eid)} for k, v in values.items()},
         'identity_evidence': {'brand': {'value': brand, 'evidence': evidence(source, eid)}},
         'known_extras': {k: {'value': v, 'evidence': evidence(source, eid)} for k, v in (extras or {}).items()},
         'model_id': 'model-' + eid, 'name': brand + ' synthetic laptop', 'product_name': brand + ' synthetic laptop'}
    return r


def snapshot(market, offers, *, instance='test', complete=True):
    sources = sorted({r.get('source') or r['store'] for r in offers}) or ['jib' if market == 'TH' else 'regard']
    return {'schema_version': 1, 'snapshot_id': market + '-snapshot', 'instance_id': instance,
         'created_at': NOW.isoformat(), 'finished_at': NOW.isoformat(), 'discovery_checked_at': AT,
         'discovery_scope_hash': 'scope-target-gpu', 'offers': offers,
         'source_coverage': [{'source': s, 'store': s, 'state': 'ok' if complete else 'partial',
                              'enabled': True, 'checked_at': AT, 'latest_attempt_at': AT} for s in sources],
         'top_model_ids': [], 'personal_profiles': []}


def fx(**kwargs):
    return fx_observation({'fx_id': 'cbr-1', 'source': 'CBR', 'currency': 'THB', 'nominal': 10,
         'rub_per_thb': '2.5', 'fetched_at': AT, 'published_date': '2026-10-14', **kwargs})


def run(ths=None, rus=None, *, th_doc=None, ru_doc=None, rate=None, now=NOW, **kwargs):
    t = thai_snapshot(th_doc or snapshot('TH', ths if ths is not None else [offer()]))
    r = russian_snapshot(ru_doc or snapshot('RU', rus)) if rus is not None or ru_doc is not None else None
    return compare_markets(t, r, rate if rate is not None else fx(), now=now, **kwargs)


class PackageDMatchingTests(unittest.TestCase):
    def pair(self, th=None, ru=None, **kwargs):
        result = run([th or offer()], [ru or offer('RU','ru-one')], **kwargs)
        return result['pairs'][0]

    def test_exact_full_identity_and_price_gap(self):
        p = self.pair()
        self.assertEqual((p['match_class'],p['comparison_status']),('EXACT','confirmed'))
        self.assertEqual(Decimal(p['saving_rub']),30000)
        self.assertEqual(p['price_gap']['band'],'clear')
        self.assertEqual(p['price_gap']['cost_scope'],'known_costs_only')

    def test_all_25_russian_offers_are_inspected_using_fixture_a(self):
        a=json.loads((Path(__file__).parent/'fixtures/stage22/acceptance_cases.json').read_text())
        c=next(c for c in a['cases'] if c['id']=='R1_ALL_RU_NOT_TOP')
        rus=[offer('RU',r['offer_id'],code=r['manufacturer_part_number'],brand=r['brand'],
                   price=r['price_rub'],specs={('screen_inch' if k=='screen_size_inch' else k):v for k,v in r['specs'].items()}) for r in c['ru_offers']]
        rdoc=snapshot('RU',rus);rdoc['top_model_ids']=['model-'+v for v in c['ru_top_offer_ids']]
        t=c['th_offers'][0]
        result=run([offer(eid=t['offer_id'],code=t['manufacturer_part_number'])],ru_doc=rdoc)
        self.assertEqual(len(result['pairs']),25)
        confirmed=[p for p in result['pairs'] if p['comparison_status']=='confirmed']
        self.assertEqual([p['ru_offer_id'] for p in confirmed],c['expected']['ru_match_offer_ids'])
        self.assertFalse(confirmed[0]['ru_membership']['in_general_top'])
        self.assertTrue(confirmed[0]['ru_membership']['in_full_list'])

    def test_fixture_a_conflicting_missing_and_other_brand_examples(self):
        a=json.loads((Path(__file__).parent/'fixtures/stage22/acceptance_cases.json').read_text())
        ids={'M1_EXACT_SAVING','C3_SAME_MPN_RESOLUTION_CONFLICT','M3_CPU_MODEL_DIFFERENT','M7_OTHER_BRAND_SAME_CORE','C4_MISSING_CPU','O1_STOCK_OUT','M2_REGIONAL_FULL_SPEC'}
        for c in a['cases']:
            if c['id'] not in ids: continue
            with self.subTest(case=c['id']):
                built=[]
                for market,side in [('RU','ru_offers'),('TH','th_offers')]:
                    r=c[side][0]
                    o=offer(market,r['offer_id'],code=r['manufacturer_part_number'],brand=r['brand'],
                            price=r.get('price_rub',r.get('price_thb')),
                            specs={('screen_inch' if k=='screen_size_inch' else k):v for k,v in r['specs'].items()})
                    o['availability_state']=o['stock_state']=r['availability_status']
                    if c['id']=='M2_REGIONAL_FULL_SPEC':
                        for key in ('manufacturer_family','manufacturer_generation'):
                            o['identity_evidence'][key]={'value':r[key],'evidence':evidence(o['source'],o['external_id'])}
                    built.append(o)
                p=self.pair(built[1],built[0]); e=c['expected']
                if 'comparison_status' in e:self.assertEqual(p['comparison_status'],e['comparison_status'])
                if 'match_class' in e:self.assertEqual(p['match_class'],e['match_class'])
                if 'saving_rub' in e:self.assertEqual(Decimal(p['saving_rub']) if p['saving_rub'] is not None else None,Decimal(e['saving_rub']) if e['saving_rub'] is not None else None)
                if e.get('current_purchase_eligible') is False:self.assertIsNone(p['saving_pct'])

    def test_each_core_conflict_at_same_sku_blocks_saving(self):
        for key,value in {'cpu':'Core Ultra 9 285HX','gpu':'RTX 5080 Laptop','ram_gb':64,'ssd_gb':2048,
                          'screen_inch':18,'screen_resolution':'3840x2400'}.items():
            with self.subTest(field=key):
                p=self.pair(offer(specs={key:value}))
                self.assertEqual(p['comparison_status'],'conflict');self.assertIsNone(p['saving_rub'])
                self.assertIn(key,p['conflicting_fields']);self.assertNotEqual(p['match_class'],'EXACT')

    def test_each_unknown_core_timestamp_blocks_comparison(self):
        for key in CORE:
            th=offer();th['specs'][key]['evidence']['captured_at']=None
            with self.subTest(field=key):
                p=self.pair(th);self.assertEqual(p['comparison_status'],'incomplete');self.assertIsNone(p['saving_pct'])

    def test_future_and_other_sku_field_evidence_does_not_confirm(self):
        for edit in ({'captured_at':(NOW+timedelta(seconds=1)).isoformat()}, {'external_id':'another-sku'}, {'source':'another-shop'}, {'method':'search_query'}, {'method':'catalog_filter'}):
            th=offer();th['specs']['gpu']['evidence'].update(edit)
            p=self.pair(th);self.assertNotEqual(p['comparison_status'],'confirmed');self.assertIsNone(p['saving_rub'])

    def test_same_family_without_generation_is_not_equivalent(self):
        th,ru=offer(code='G615LR-TH'),offer('RU','ru-one',code='G615LR-XRU')
        for o in (th,ru):o['identity_evidence']['manufacturer_family']={'value':'G615LR','evidence':evidence(o['source'],o['external_id'])}
        p=self.pair(th,ru);self.assertEqual(p['match_class'],'SAME_FAMILY');self.assertEqual(p['comparison_status'],'incomplete');self.assertIsNone(p['saving_pct'])

    def test_family_different_specs_is_same_family_conflict(self):
        th,ru=offer(code='G615LR-TH',specs={'cpu':'Core Ultra 9 285HX'}),offer('RU','ru-one',code='G615LR-XRU')
        for o in (th,ru):
            for key,value in [('manufacturer_family','G615LR'),('manufacturer_generation','2025-G615LR')]:
                o['identity_evidence'][key]={'value':value,'evidence':evidence(o['source'],o['external_id'])}
        p=self.pair(th,ru);self.assertEqual((p['match_class'],p['comparison_status']),('SAME_FAMILY','conflict'))

    def test_known_hardware_extras_conflict(self):
        for key,a,b in [('gpu_vram_gb',12,16),('gpu_tgp_w',115,150),('screen_refresh_hz',165,240),('panel_type','IPS','OLED')]:
            p=self.pair(offer(extras={key:a}),offer('RU','ru-one',extras={key:b}))
            self.assertEqual(p['comparison_status'],'conflict');self.assertIsNone(p['saving_pct'])

    def test_unknown_extra_evidence_is_not_a_confirmed_fact(self):
        th=offer(extras={'gpu_vram_gb':12});th['known_extras']['gpu_vram_gb']['evidence']['captured_at']=None
        p=self.pair(th);self.assertEqual(p['comparison_status'],'incomplete')

    def test_commercial_differences_are_disclosed(self):
        p=self.pair(offer(extras={'os':'DOS'}),offer('RU','ru-one',extras={'os':'Windows 11 Pro'}))
        self.assertEqual(p['comparison_status'],'confirmed');self.assertIn('os',p['commercial_differences'])

    def test_wpro_suffix_not_stripped(self):
        p=self.pair(offer(code='G615LR-TEST17-WPRO'))
        self.assertNotEqual(p['match_class'],'EXACT');self.assertIsNone(p['saving_pct'])

    def test_bare_lenovo_series_is_not_an_exact_sku(self):
        p=self.pair(offer(code='16AFR10H',brand='Lenovo'),offer('RU','ru-one',code='16AFR10H',brand='Lenovo'))
        self.assertNotEqual(p['match_class'],'EXACT')

    def test_nominal_tb_normalization_requires_documentation(self):
        th,ru=offer(specs={'ssd_gb':1000}),offer('RU','ru-one',specs={'ssd_gb':1024})
        self.assertEqual(self.pair(th,ru)['comparison_status'],'conflict')
        th['nominal_ssd_gb']=ru['nominal_ssd_gb']=1000
        self.assertEqual(self.pair(th,ru)['comparison_status'],'confirmed')

    def test_exact_cpu_spellings_are_normalized(self):
        self.assertEqual(self.pair(offer(specs={'cpu':'CORE ULTRA 9 275HX'}))['comparison_status'],'confirmed')

    def test_membership_flags_and_views_preserve_one_calculation(self):
        ro=offer('RU','ru-one');rdoc=snapshot('RU',[ro]);rdoc['top_model_ids']=[ro['model_id']]
        rdoc['personal_profiles']=[{'key':'personal-asus','model_ids':[ro['model_id']],'match_kind':'exact_sku'}]
        result=run(ru_doc=rdoc);p=result['pairs'][0]
        self.assertTrue(p['ru_membership']['in_full_list']);self.assertTrue(p['ru_membership']['in_general_top']);self.assertTrue(p['ru_membership']['in_personal_top3'])
        for view in ('all','general_top','personal_top3'):
            self.assertIs(comparison_view(result,view)[0],p)
        with self.assertRaises(ContractError):comparison_view(result,'unknown')


class PackageDRankingTests(unittest.TestCase):
    def test_thai_top_independent_of_ru_empty_or_changed(self):
        ths=[offer(eid='th-'+str(i),code='G615LR-TEST'+str(i),price=80000+i*1000) for i in range(8)]
        empty=run(ths);different=run(ths,[offer('RU','other',code='OTHER-123456',brand='MSI')])
        self.assertEqual(empty['thai_top_value'],different['thai_top_value'])
        self.assertEqual(len(empty['thai_top_value']),5);self.assertEqual(len(empty['eligible_models']),8)
        self.assertEqual(len(empty['all_offers']),8)

    def test_value_top_can_differ_from_cheapest(self):
        ths=[offer(eid='cheap',price=80000,specs={'cpu':'Intel Core i5 12450H'}),
             offer(eid='strong',code='OTHER-123456',price=82000,specs={'screen_inch':18})]
        r=run(ths);self.assertEqual(r['thai_top_value'][0]['best_offer_id'],'strong')
        self.assertEqual(r['thai_cheapest'][0]['cheapest_offer_id'],'cheap')

    def test_unknown_cpu_can_rank_but_cannot_compare(self):
        th=offer();th['specs']['cpu']['evidence']['captured_at']=None
        r=run([th],[offer('RU','ru-one')]);self.assertEqual(len(r['thai_top_value']),1)
        self.assertEqual(r['thai_top_value'][0]['score_breakdown']['cpu'],'0.0')
        self.assertIsNone(r['pairs'][0]['saving_pct'])

    def test_hardware_gate_and_missing_hard_evidence(self):
        for specs in ({'gpu':'RTX 5070 Laptop'},{'gpu':'RTX 5080 Desktop'},{'ram_gb':16}, {'ssd_gb':512}, {'screen_resolution':'QHD+'},{'screen_resolution':'1920x1080'}):
            self.assertEqual(run([offer(specs=specs)])['thai_top_value'],[])
        th=offer();th['specs']['ram_gb']['evidence']['captured_at']=None
        self.assertEqual(run([th])['thai_top_value'],[])

    def test_exact_configuration_groups_keep_all_prices(self):
        ths=[offer(eid='a',price=80000),offer(eid='b',source='advice',price=81000),offer(eid='c',source='invadeit',price=79000)]
        r=run(ths);self.assertEqual(len(r['eligible_models']),1)
        row=r['eligible_models'][0];self.assertEqual(row['best_offer_id'],'c');self.assertEqual(len(row['offers']),3)
        self.assertEqual(row['member_offer_ids'],['a','b','c'])

    def test_conflicting_cpu_configurations_never_group(self):
        r=run([offer(eid='a'),offer(eid='b',specs={'cpu':'Core Ultra 9 285HX'})])
        self.assertEqual(len(r['eligible_models']),2)
        self.assertEqual(len({m['model_id'] for m in r['eligible_models']}),2)

    def test_unknown_cpu_does_not_bridge_two_configurations(self):
        r=run([offer(eid='a'),offer(eid='b',specs={'cpu':None}),offer(eid='c',specs={'cpu':'Core Ultra 9 285HX'})])
        self.assertEqual(len(r['eligible_models']),3)

    def test_identical_incomplete_offers_have_distinct_ids(self):
        r=run([offer(eid='a',specs={'cpu':None}),offer(eid='b',specs={'cpu':None})])
        self.assertEqual(len({m['model_id'] for m in r['eligible_models']}),2)

    def test_group_id_independent_of_price_winner(self):
        a,b=offer(eid='a'),offer(eid='b',source='advice',price=81000)
        first=run([a,b])['eligible_models'][0]['model_id'];b['price_thb']=79000
        self.assertEqual(first,run([b,a])['eligible_models'][0]['model_id'])

    def test_tie_order_and_input_order_are_stable(self):
        ths=[offer(eid='a',code='G615LR-A111'),offer(eid='b',code='G615LR-B111')]
        self.assertEqual(run(ths)['thai_top_value'],run(list(reversed(ths)))['thai_top_value'])

    def test_full_raw_catalog_retains_rejected_and_unavailable(self):
        th=offer(eid='oos');th['availability_state']='out_of_stock'
        r=run([offer(),th]);self.assertEqual(len(r['all_offers']),2)
        self.assertEqual(len(r['exclusions']),1)

    def test_unknown_brand_does_not_filter_purchase_top(self):
        th=offer(brand='NEW LAPTOP MAKER');self.assertEqual(len(run([th])['thai_top_value']),1)

    def test_disabled_sources_and_unknown_sellers_rejected(self):
        th=offer();th['seller_trust']='unknown';self.assertEqual(run([th])['thai_top_value'],[])
        tdoc=snapshot('TH',[offer()]);tdoc['source_coverage'][0]['enabled']=False
        self.assertEqual(run(th_doc=tdoc)['thai_top_value'],[])

    def test_partial_coverage_does_not_invalidate_a_fresh_pair(self):
        r=run(th_doc=snapshot('TH',[offer()],complete=False),ru_doc=snapshot('RU',[offer('RU','ru-one')]))
        self.assertEqual(r['pairs'][0]['comparison_status'],'confirmed');self.assertFalse(r['market_coverage_complete'])

    def test_score_matches_existing_international_formula_at_integer_prices(self):
        t=thai_snapshot(snapshot('TH',[offer()]));o=t.offers[0]
        for price in (180000,200000,230000,260000,290000):
            score,_,_=value_score(o,Decimal(price),now=NOW,policy=ComparisonPolicy())
            legacy=international_value_score(price_rub=price,gpu=CORE['gpu'],cpu=CORE['cpu'],ram_gb=32,ssd_gb=1024,screen_inch=16,screen_resolution='2560x1600')
            self.assertEqual(score,Decimal(str(legacy.score)))


class PackageDPriceAndFreshnessTests(unittest.TestCase):
    def test_cap_boundary_keeps_fraction_before_rounding(self):
        self.assertEqual(len(run([offer(price='116000')])['thai_top_value']),1)
        self.assertEqual(run([offer(price='116000.004')])['thai_top_value'],[])

    def test_price_gap_boundaries_and_signed_symmetry(self):
        for percent,band in [(Decimal('4.999'),'comparable'),(Decimal(5),'slight'),(Decimal('9.999'),'slight'),(Decimal(10),'clear')]:
            for sign in (-1,1):
                gap=price_gap(Decimal(100000),Decimal(100000)-percent*1000*sign)
                self.assertEqual(gap['band'],band);self.assertEqual(Decimal(gap['saving_pct']),percent*sign)

    def test_only_confirmed_expenses_affect_total(self):
        r=run(rus=[offer('RU','ru-one')],expenses=(Expense('shipping',Decimal(35000),True,'invoice-1'),Expense('customs',Decimal(1000),False)))
        p=r['pairs'][0];self.assertEqual(Decimal(p['saving_rub']),-5000)
        self.assertIn('customs',p['unknown_costs']);self.assertEqual(len(p['confirmed_expenses']),1)

    def test_expense_without_evidence_is_unknown(self):
        r=run(rus=[offer('RU','ru-one')],expenses=(Expense('shipping',Decimal(1000),True),))
        self.assertEqual(Decimal(r['pairs'][0]['saving_rub']),30000)
        self.assertIn('shipping',r['pairs'][0]['unknown_costs'])

    def test_confirmed_cost_category_is_not_also_unknown(self):
        r=run(rus=[offer('RU','ru-one')],expenses=(Expense('delivery',Decimal(1000),True,'invoice'),))
        self.assertNotIn('delivery',r['pairs'][0]['unknown_costs'])
        self.assertIn('customs',r['pairs'][0]['unknown_costs'])

    def test_expense_scope_is_specific_to_offer(self):
        r=run([offer(eid='a'),offer(eid='b',source='advice')],[offer('RU','ru-one')],expenses=(Expense('shipping',Decimal(5000),True,'receipt','a'),))
        self.assertEqual({p['th_offer_id']:Decimal(p['saving_rub']) for p in r['pairs']},{'a':25000,'b':30000})

    def test_cheapest_comparable_ru_is_selected_not_cluster_price(self):
        rus=[offer('RU','wrong',price=170000,specs={'cpu':'Core Ultra 9 285HX'}),offer('RU','right',price=230000),offer('RU','dear',price=270000)]
        r=run(rus=rus);by_id={p['pair_id']:p for p in r['pairs']}
        best=by_id[next(iter(r['best_pairs_by_th_variant_id'].values()))]
        self.assertEqual(best['ru_offer_id'],'right');self.assertEqual(Decimal(best['saving_rub']),30000)

    def test_unknown_and_non_finite_or_non_positive_quotes(self):
        for price in (True,None,'NaN','Infinity',0,-1):
            th=offer();th['price_thb']=price
            self.assertEqual(run([th])['thai_top_value'],[])

    def test_quote_ttl_both_axes_and_future_timestamps(self):
        for key in ('price_checked_at','availability_checked_at'):
            for at in (None,(NOW-timedelta(hours=6,seconds=1)).isoformat(),(NOW+timedelta(seconds=1)).isoformat()):
                th=offer();th[key]=at
                r=run([th],[offer('RU','ru-one')]);self.assertEqual(r['thai_top_value'],[]);self.assertIsNone(r['pairs'][0]['saving_pct'])
        th=offer();th['price_checked_at']=th['availability_checked_at']=(NOW-timedelta(hours=6)).isoformat();th['availability_evidence']['captured_at']=th['availability_checked_at']
        self.assertEqual(len(run([th])['thai_top_value']),1)

    def test_ru_latest_failure_and_three_hour_boundary(self):
        ru=offer('RU','ru-one');ru['price_checked_at']=(NOW-timedelta(hours=3)).isoformat()
        self.assertEqual(run(rus=[ru])['pairs'][0]['comparison_status'],'confirmed')
        ru['price_checked_at']=(NOW-timedelta(hours=3,seconds=1)).isoformat()
        self.assertIsNone(run(rus=[ru])['pairs'][0]['saving_pct'])
        rd=snapshot('RU',[offer('RU','ru-one')]);rd['source_coverage'][0]['state']='failed'
        self.assertIsNone(run(ru_doc=rd)['pairs'][0]['saving_pct'])

    def test_invalid_fx_stale_date_nominal_and_future(self):
        for edit in ({'rub_per_thb':'NaN'},{'rub_per_thb':'0'},{'fetched_at':None},
                     {'fetched_at':(NOW-timedelta(hours=36,seconds=1)).isoformat()},
                     {'fetched_at':(NOW+timedelta(seconds=1)).isoformat()},
                     {'published_date':'2026-10-09'},{'published_date':'2026-10-15'},
                     {'nominal':0},{'nominal':'10'},{'source':'other'},{'error':'failure'}):
            with self.subTest(edit=edit):
                r=run(rus=[offer('RU','ru-one')],rate=fx(**edit));self.assertEqual(r['thai_top_value'],[])
                self.assertIsNone(r['pairs'][0]['saving_pct']);self.assertEqual(len(r['all_offers']),1)

    def test_fx_inclusive_boundaries_moscow_calendar(self):
        self.assertTrue(fx(fetched_at=(NOW-timedelta(hours=36)).isoformat(),published_date='2026-10-10').usable(NOW,ComparisonPolicy()))
        night=NOW.replace(hour=22,minute=30)
        self.assertTrue(fx(published_date='2026-10-15').usable(night,ComparisonPolicy()))

    def test_absent_or_expired_ru_produces_th_only(self):
        r=run();self.assertEqual(r['pairs'],[]);self.assertIsNone(r['ru_snapshot_id'])
        self.assertEqual(r['unmatched_th'][0]['reason'],'limited_coverage')
        rd=snapshot('RU',[offer('RU','ru-one')]);rd['created_at']=(NOW-timedelta(hours=4)).isoformat()
        self.assertEqual(run(ru_doc=rd)['pairs'],[])

    def test_unmatched_value_is_independent_no_buy_no_saving(self):
        r=run([offer(eid='5080',brand='MSI',code='MSI-5080-TH',price=100000,specs={'gpu':'RTX 5080 Laptop'})],[])
        row=r['unmatched_th'][0];self.assertTrue(row['unmatched_value_candidate'])
        self.assertEqual(row['reason'],'not_observed');self.assertIsNone(row['saving_pct']);self.assertIsNone(row['buy_signal'])

    def test_unmatched_value_thresholds_are_configurable(self):
        r=run([offer()],[],policy=ComparisonPolicy(unmatched_min_score=Decimal(100)))
        self.assertFalse(r['unmatched_th'][0]['unmatched_value_candidate'])
        r=run([offer(price=100000)],[])
        self.assertFalse(r['unmatched_th'][0]['unmatched_value_candidate'])

    def test_unmatched_reasons_conflict_incomplete_coverage(self):
        conflict=run([offer(specs={'cpu':'Core Ultra 9 285HX'})],[offer('RU','ru-one')])
        self.assertEqual(conflict['unmatched_th'][0]['reason'],'conflict')
        incomplete=run([offer(specs={'cpu':None})],[offer('RU','ru-one')])
        self.assertEqual(incomplete['unmatched_th'][0]['reason'],'incomplete')
        rd=snapshot('RU',[],complete=False)
        self.assertEqual(run(ru_doc=rd)['unmatched_th'][0]['reason'],'limited_coverage')

    def test_current_inputs_are_not_mutated_and_output_is_json(self):
        td,rd=snapshot('TH',[offer()]),snapshot('RU',[offer('RU','ru-one')]);original=copy.deepcopy((td,rd))
        r=run(th_doc=td,ru_doc=rd);self.assertEqual((td,rd),original)
        json.dumps(r);self.assertEqual(r,run(th_doc=td,ru_doc=rd))

    def test_invalid_schema_duplicate_ids_instance_and_naive_clock(self):
        td=snapshot('TH',[offer(),offer()])
        with self.assertRaises(ContractError):thai_snapshot(td)
        td=snapshot('TH',[offer()]);td['schema_version']=2
        with self.assertRaises(ContractError):thai_snapshot(td)
        rd=snapshot('RU',[offer('RU','ru-one')],instance='different')
        with self.assertRaises(ContractError):run(ru_doc=rd)
        with self.assertRaises(ContractError):run(now=NOW.replace(tzinfo=None))

    def test_no_network_or_stateful_modules_needed_by_kernel(self):
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
            self.assertEqual(run(rus=[offer('RU','ru-one')])['pairs'][0]['comparison_status'],'confirmed')


class PackageDAdditionalGuardTests(unittest.TestCase):
    def test_old_stock_evidence_cannot_be_revived_by_new_timestamp(self):
        th=offer();th['availability_evidence']['captured_at']=(NOW-timedelta(hours=7)).isoformat()
        r=run([th],[offer('RU','ru-one')])
        self.assertEqual(r['thai_top_value'],[]);self.assertIsNone(r['pairs'][0]['saving_pct'])

    def test_decimal_conversion_boundary_survives_low_caller_precision(self):
        with localcontext() as ctx:
            ctx.prec=6
            rate=fx(rub_per_thb='2.5000000000000000000000000000000000000000000001')
            self.assertEqual(run([offer(price=116000)],rate=rate)['thai_top_value'],[])

    def test_snapshot_captured_at_is_not_a_spec_evidence_timestamp(self):
        th=offer();th['specs']['cpu']['evidence']['captured_at']=None
        td=snapshot('TH',[th]);td['finished_at']=NOW.isoformat()
        self.assertIsNone(run(th_doc=td,rus=[offer('RU','ru-one')])['pairs'][0]['saving_pct'])

    def test_exact_mpn_conflicting_generation_is_conflict(self):
        th,ru=offer(),offer('RU','ru-one')
        for o,v in ((th,'generation-2'),(ru,'generation-1')):
            o['identity_evidence']['manufacturer_generation']={'value':v,'evidence':evidence(o['source'],o['external_id'])}
        self.assertEqual(run([th],[ru])['pairs'][0]['comparison_status'],'conflict')

    def test_region_equivalent_offers_are_distinct_thai_variants(self):
        a,b=offer(eid='a',code='G615LR-TH1'),offer(eid='b',code='G615LR-TH2')
        for o in (a,b):
            for k,v in [('manufacturer_family','G615LR'),('manufacturer_generation','2025')]:
                o['identity_evidence'][k]={'value':v,'evidence':evidence(o['source'],o['external_id'])}
        self.assertEqual(len(run([a,b])['eligible_models']),2)

    def test_all_pair_identity_indexes_are_consistent(self):
        r=run([offer(eid='a'),offer(eid='b',source='advice')],[offer('RU','one'),offer('RU','two')])
        ids={p['pair_id'] for p in r['pairs']}
        self.assertEqual(len(ids),4)
        self.assertEqual(set(pid for rows in r['by_ru_offer_id'].values() for pid in rows),ids)
        self.assertEqual(set(pid for rows in r['by_th_variant_id'].values() for pid in rows),ids)

    def test_unmatched_selection_considers_all_models_beyond_th_top5(self):
        ths=[offer(eid='th'+str(i),code='MODEL-SKU-'+str(i),price=80000+i*100) for i in range(7)]
        rus=[offer('RU','ru'+str(i),code='MODEL-SKU-'+str(i)) for i in range(5)]
        r=run(ths,rus)
        self.assertEqual(len(r['unmatched_th']),2)
        self.assertEqual(len(r['unmatched_value_top']),2)
        self.assertTrue(all(row['best_offer_id'] not in [t['best_offer_id'] for t in r['thai_top_value']] for row in r['unmatched_th']))

    def test_known_total_above_budget_does_not_claim_affordability(self):
        r=run(rus=[offer('RU','ru-one')],expenses=(Expense('delivery',Decimal(140000),True,'invoice'),))
        self.assertFalse(r['pairs'][0]['known_total_within_budget'])

    def test_b_adapter_explicit_title_brand_is_supported(self):
        ru=offer('RU','ru-one');ru.pop('identity_evidence');ru['product_name']='Ноутбук ASUS G615LR-TEST17'
        self.assertEqual(run(rus=[ru])['pairs'][0]['comparison_status'],'confirmed')

    def test_future_c_complete_json_fixture(self):
        data=json.loads((Path(__file__).parent/'fixtures/stage22/d_catalog_snapshot.json').read_text())
        self.assertIs(data['all_data_synthetic'],True)
        result=compare_markets(thai_snapshot(data['catalog']),None,fx_observation(data['fx']),now=NOW)
        self.assertEqual(len(result['thai_top_value']),1)
        self.assertEqual(len(result['all_offers']),1)
        self.assertEqual(Decimal(result['thai_top_value'][0]['price_rub']),Decimal(200000))
        self.assertIsNone(result['unmatched_th'][0]['saving_pct'])

    def test_decimal_spec_fields_produce_strict_json(self):
        th=offer(specs={'ram_gb':Decimal(32),'screen_inch':Decimal('16.0')})
        result=run([th],[offer('RU','ru-one')])
        self.assertEqual(result['pairs'][0]['comparison_status'],'confirmed')
        json.dumps(result,allow_nan=False)
        th['specs']['cpu']['value']=float('nan')
        json.dumps(run([th]),allow_nan=False)

    def test_untyped_or_title_like_identifiers_do_not_prove_exact(self):
        th=offer();th['strong_identifiers']=[{'value':'G615LR-TEST17'}]
        with self.assertRaises(ContractError):thai_snapshot(snapshot('TH',[th]))
        th,ru=offer(code='Ноутбук G615LR-TEST17'),offer('RU','ru-one',code='Ноутбук G615LR-TEST17')
        self.assertNotEqual(run([th],[ru])['pairs'][0]['match_class'],'EXACT')

    def test_policy_numeric_inputs_are_normalized_and_invalid_rejected(self):
        policy=ComparisonPolicy(max_price_rub='290000',unmatched_min_score='75',gpu_targets=(('RTX 5070 Ti',230000),))
        self.assertEqual(policy.max_price_rub,Decimal(290000))
        self.assertEqual(len(run([offer()],policy=policy)['thai_top_value']),1)
        for kwargs in ({'max_price_rub':float('inf')},{'unmatched_min_score':Decimal('NaN')},
                       {'quote_hours':True},{'gpu_targets':(('RTX 5080',0),)}):
            with self.assertRaises(ContractError):ComparisonPolicy(**kwargs)

    def test_duplicate_source_coverage_fails_closed(self):
        doc=snapshot('TH',[offer()]);doc['source_coverage'].append(copy.deepcopy(doc['source_coverage'][0]))
        with self.assertRaises(ContractError):thai_snapshot(doc)

    def test_price_gap_negative_cost_and_non_finite_rejected(self):
        for values in ((Decimal(100),Decimal(100),Decimal(-1)),(Decimal('NaN'),Decimal(1),Decimal(0))):
            with self.assertRaises(ContractError):price_gap(*values)



class ActualPackageBAdapterTests(unittest.TestCase):
    def test_real_b_export_unknown_spec_time_is_not_revived_then_confirmed(self):
        import test_market_snapshot as b
        f=b.RussianMarketSnapshotTests('test_exports_every_eligible_model_beyond_top10')
        f.setUp()
        try:
            f._export();raw=f._load()
            ru17=next(o for o in raw['offers'] if 'RU17-MPN-5070TI' in o['mpn_candidates'])
            identity={ru17['offer_id']:{'brand':{'value':'ASUS','evidence':evidence(ru17['store'],ru17['external_id'])}}}
            r=russian_snapshot(raw,identity_evidence=identity)
            td=snapshot('TH',[offer(code='RU17-MPN-5070TI',specs={'gpu':'RTX 5070 Ti'})],instance='snapshot-test')
            result=compare_markets(thai_snapshot(td),r,fx(),now=NOW)
            pair=next(p for p in result['pairs'] if p['ru_offer_id']==ru17['offer_id'])
            self.assertEqual(pair['comparison_status'],'incomplete');self.assertIsNone(pair['saving_pct'])
            cache=json.loads(f.specs_path.read_text());record=cache['regard:base-17']
            fields=['cpu','gpu','ram_gb','ssd_gb','screen_size_inch','screen_resolution','gpu_vram_gb','screen_refresh_hz']
            record['source_identifiers']={'field_captured_at':{k:AT for k in fields},'field_provenance':{k:'structured_api' for k in fields}}
            f.specs_path.write_text(json.dumps(cache));f._export();raw=f._load()
            ru17=next(o for o in raw['offers'] if 'RU17-MPN-5070TI' in o['mpn_candidates'])
            identity={ru17['offer_id']:{'brand':{'value':'ASUS','evidence':evidence(ru17['store'],ru17['external_id'])}}}
            result=compare_markets(thai_snapshot(td),russian_snapshot(raw,identity_evidence=identity),fx(),now=NOW)
            pair=next(p for p in result['pairs'] if p['ru_offer_id']==ru17['offer_id'])
            self.assertEqual(pair['comparison_status'],'confirmed')
            self.assertGreater(len(raw['offers']),25)
        finally:f.tearDown()


class PackageDPackagingTests(unittest.TestCase):
    def test_all_d_modules_ship_and_bundle_imports_pass(self):
        from scripts.build_deploy_bundle import build_deploy_bundle
        from scripts.release_manifest import bundle_relpaths
        from scripts.release_verify import verify_bundle, first_party_closure
        root=Path(__file__).resolve().parent.parent
        modules={'market_contracts.py','thai_market_ranking.py','cross_market_matching.py','cross_market_comparison.py'}
        self.assertTrue(modules <= set(bundle_relpaths(root)))
        reached,missing=first_party_closure(root,roots=[p.removesuffix('.py') for p in modules],repo_root=root)
        self.assertEqual(missing,[]);self.assertTrue(reached <= set(bundle_relpaths(root)))
        with tempfile.TemporaryDirectory() as directory:
            archive,_=build_deploy_bundle(root=root,dist_dir=Path(directory))
            self.assertEqual(verify_bundle(archive,repo_root=root).errors,[])


if __name__ == '__main__':
    unittest.main()
