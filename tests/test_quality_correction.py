"""Addressed v0.11.0 regressions. No live shops, FX or Telegram."""
from __future__ import annotations
import copy,json,tempfile,unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from collection import CollectionResult,StoreCollectResult,persist_collection
from market_snapshot import _observation_state,_price_changed_at,_price_checked_at,export_russian_market_snapshot,load_russian_market_snapshot
from models import Product
from storage import init_db,open_db,save_products,get_all_products_readonly,get_price_history_for_product,insert_store_run
from thai_catalog import as_comparison_snapshot,comparison_from_catalog
from thailand.jib import parse_suggestion_discovery
from thailand.speedcom import parse_speedcom_product
from thailand.specs_parse import own_ssd_gb
from thailand.market_telegram import MarketTextSender
from thailand.market_events import _retry_delivery_locked
from thailand.job_queue import _atomic_write
from thailand.market_messages import format_market_messages
from tests.test_stage22_comparison import offer,snapshot,NOW,AT


class Sink:
    def __init__(self):self.messages=[]
    def send_market_message(self,text,**kwargs):
        self.messages.append((text,kwargs))
        class Ack:ok=True
        return Ack()


class PriceObservation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'market.db'
        self.old=NOW-timedelta(days=2)
        self.product=Product('regard','q1','https://example.test/q1','ASUS ROG G18 Intel Core Ultra 9 275HX RTX 5070 Ti 32GB DDR5 1TB SSD 18\" 2560x1600','G815LR-REGION-RU',250000,True,self.old)
        save_products([self.product],self.db)
    def tearDown(self):self.tmp.cleanup()
    def row_history(self):
        row=get_all_products_readonly(self.db)[0]
        with open_db(self.db) as conn:history=get_price_history_for_product(conn,row['id'])
        return row,history
    def test_unchanged_successful_quote_is_fresh_history_unchanged(self):
        _,before=self.row_history();save_products([replace(self.product,checked_at=NOW)],self.db)
        row,history=self.row_history();self.assertEqual(history,before)
        self.assertEqual(_price_checked_at(row,history,row['price']),NOW.isoformat())
        self.assertEqual(_price_changed_at(history),self.old.isoformat())
        self.assertEqual(_observation_state(row,history,NOW),'current')
    def test_changed_price_is_fresh_and_adds_history(self):
        save_products([replace(self.product,price=249000,checked_at=NOW)],self.db)
        row,history=self.row_history();self.assertEqual(len(history),2)
        self.assertEqual(_price_checked_at(row,history,row['price']),NOW.isoformat());self.assertEqual(_price_changed_at(history),NOW.isoformat())
    def test_failed_or_partial_store_without_confirmation_does_not_refresh(self):
        for error in ('failed HTTP','partial without own confirmation'):
            before=self.row_history()
            collection=CollectionResult(stores={'regard':StoreCollectResult('regard',False,[replace(self.product,checked_at=NOW)],error)})
            persist_collection(collection,self.db);self.assertEqual(self.row_history(),before)
    def test_fresh_price_does_not_refresh_old_availability(self):
        save_products([replace(self.product,price_checked_at=NOW)],self.db)
        row,history=self.row_history();self.assertEqual(row['last_checked_at'],self.old.isoformat())
        self.assertEqual(_price_checked_at(row,history,row['price']),NOW.isoformat());self.assertEqual(_observation_state(row,history,NOW),'stale')
    def test_price_change_uses_own_price_clock_without_refreshing_stock(self):
        save_products([replace(self.product,price=249000,price_checked_at=NOW)],self.db)
        row,history=self.row_history()
        self.assertEqual(_price_changed_at(history),NOW.isoformat())
        self.assertEqual(row['last_checked_at'],self.old.isoformat())
        self.assertEqual(_observation_state(row,history,NOW),'stale')
    def test_old_schema_clock_uses_own_observation_not_history_or_pipeline(self):
        row,history=self.row_history();row.pop('price_checked_at');row['last_checked_at']=NOW.isoformat()
        self.assertEqual(_price_checked_at(row,history,row['price']),NOW.isoformat())
        row['last_checked_at']=self.old.isoformat();self.assertEqual(_observation_state(row,history,NOW),'stale')
    def test_stock_only_history_change_does_not_change_price_change_time(self):
        save_products([replace(self.product,available=False,checked_at=NOW)],self.db)
        _,history=self.row_history();self.assertEqual(len(history),2);self.assertEqual(_price_changed_at(history),self.old.isoformat())
    def fresh_snapshot(self):
        save_products([replace(self.product,checked_at=NOW-timedelta(minutes=1))],self.db)
        with open_db(self.db) as conn:
            init_db(conn);insert_store_run(conn,pipeline_run_id=None,store='regard',attempted_at=AT,finished_at=AT,status='ok',products_count=1)
        root=self.root/'ru'
        export_russian_market_snapshot(self.db,specs_path=self.root/'absent-cache.json',snapshot_root=root,instance_id='test',now=NOW,pipeline_lock_held=True)
        return load_russian_market_snapshot(root)
    def test_end_to_end_unchanged_price_snapshot_equivalent_and_three_events(self):
        ru=self.fresh_snapshot();self.assertEqual(len(ru['offers']),1)
        self.assertFalse(any(x['reason']=='stale_observation' for x in ru['exclusions']))
        th=offer(code='G815LR-REGION-TH',specs={'screen_inch':18},eid='quality-th')
        # A documented family/generation is independent evidence, not stripped SKU.
        for row in [ru['offers'][0],th]:
            source=row.get('store') or row['source'];eid=row['external_id']
            ev={'source':source,'external_id':eid,'captured_at':AT,'method':'structured_api'}
            row.setdefault('identity_evidence',{}).update({key:{'value':value,'evidence':ev} for key,value in [('brand','ASUS'),('manufacturer_family','ROG-STRIX-G18'),('manufacturer_generation','2026')]})
        comp=comparison_from_catalog(snapshot('TH',[th]),ru,{'fx_id':'quality-fx','source':'CBR','currency':'THB','nominal':1,'rub_per_thb':'2.5','fetched_at':AT,'published_date':'2026-10-14'},now=NOW)
        self.assertEqual((comp['pairs'][0]['match_class'],comp['pairs'][0]['comparison_status']),('EQUIVALENT','confirmed'))
        deliver_three(self,self.root/'jobs',comp)
        negative=copy.deepcopy(th);negative['specs']['ram_gb']['value']=16
        bad=comparison_from_catalog(snapshot('TH',[negative]),ru,{'fx_id':'quality-fx','source':'CBR','currency':'THB','nominal':1,'rub_per_thb':'2.5','fetched_at':AT,'published_date':'2026-10-14'},now=NOW)
        self.assertEqual(bad['pairs'][0]['comparison_status'],'conflict')
        self.assertIn('ram_gb',bad['pairs'][0]['conflicting_fields'])


def result(comp=None):
    from tests.test_stage22_comparison import run
    comp=comp or run(ths=[offer()],rus=[])
    return {'result_id':'quality-result','scan_id':'quality-result','status':'partial','rankings':comp,'comparison':comp,'fx':{'source':'CBR','currency':'THB','nominal':1,'rub_per_thb':'2.5','fx_id':'quality-fx','fetched_at':AT,'published_date':'2026-10-14'}}


def deliver_three(case,root,comp=None):
    # Real sender + queue receipt paths. Only the HTTP boundary is a recording sink.
    r=result(comp);sink=Sink();sender=MarketTextSender(sink,jobs_dir=root,now_fn=lambda:NOW)
    _atomic_write(root/'results/quality-result.json',r)
    for trigger,view in [('daily','general_top'),('personal','personal_top3'),('manual','all')]:
        from thailand.market_events import build_market_event
        event=build_market_event(instance_id='test',trigger_type=trigger,requested_view=view,event_id=trigger,recipients=['42'],now=NOW)
        event.update(result_id='quality-result',subscription_scan_id='quality-result')
        _atomic_write(root/'delivery'/f'{trigger}.json',event)
    _retry_delivery_locked(root,sender,fallback=NOW)
    texts=[x[0] for x in sink.messages]
    # The comparison fixture has no view membership, so each view's fallback is equal.
    case.assertEqual(len(texts),len(set(texts)))
    case.assertEqual(len(list((root/'archive').glob('*.json'))),3)
    for p in (root/'archive').glob('*.json'):
        e=json.loads(p.read_text());case.assertEqual(e['delivery_state'],'sent');case.assertTrue(e['delivered_part_ids'])
    return sink


class ThaiAndDelivery(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((Path(__file__).parent/'fixtures/quality_scan_f2035f4a.json').read_text())
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_actual_saved_vga_names_never_become_laptop_offers(self):
        for row in self.fixture['components']:
            self.assertEqual(parse_suggestion_discovery([{'id':row['external_id'],'title':row['title'],'price':59900}],search_context_gpu='RTX 5080',collected_at=NOW),[])
            doc=snapshot('TH',[offer()]);doc['offers'][0]['name']=row['title'];self.assertEqual(as_comparison_snapshot(doc).offers,())
    def test_actual_speedcom_titles_do_not_confirm_ram_as_ssd(self):
        for row in self.fixture['speedcom']:
            for ram in (32,64,96):
                # The title is factual; body is a labelled reconstruction of the boundary.
                body=f'Graphics: RTX 5070 Ti Laptop GPU RAM {ram}GB SSD'
                product={'id':100,'title':row['title'],'product_type':'NOTEBOOK','body_html':body,'variants':[{'id':1,'sku':'OWN-SKU-123','price':'119990','available':True}]}
                o=parse_speedcom_product(product,collected_at=NOW)[0];self.assertIsNone(o.ssd_gb)
                self.assertIsNone(o.metadata.get('field_evidence',{}).get('ssd_gb'))
                product['body_html']=body+' 1TB';o=parse_speedcom_product(product,collected_at=NOW)[0];self.assertEqual(o.ssd_gb,1024)
    def test_explicit_own_ssd_field_and_ram_are_separate(self):
        self.assertEqual(own_ssd_gb('RAM 64GB SSD 1TB'),1024)
        self.assertIsNone(own_ssd_gb('RAM 96GB SSD'))
        self.assertEqual(own_ssd_gb('SSD: 64GB'),64)
        self.assertIsNone(own_ssd_gb('up to SSD 8TB'))
    def test_three_equal_views_one_physical_send_and_all_events_archived(self):
        sink=deliver_three(self,self.root);self.assertEqual(len(sink.messages),1)
    def test_other_recipient_or_result_is_not_deduplicated(self):
        sink=Sink();sender=MarketTextSender(sink,jobs_dir=self.root,now_fn=lambda:NOW)
        for rid,chat in [('one','42'),('one','43'),('two','42')]:
            r=result();r['result_id']=rid
            sender.send({'event_id':rid+chat,'requested_view':'all','recipients':[chat]},r,current=False,historical=True)
        self.assertEqual(len(sink.messages),3)
    def test_different_text_does_not_reuse_content_receipt(self):
        sink=Sink();sender=MarketTextSender(sink,jobs_dir=self.root,now_fn=lambda:NOW)
        r=result();e={'requested_view':'all','recipients':['42']}
        sender.send(copy.deepcopy(e),r,current=False,historical=True)
        r['rankings']['thai_top_value'][0]['offers'][0]['price']='81000'
        sender.send(copy.deepcopy(e),r,current=False,historical=True);self.assertEqual(len(sink.messages),2)
    def test_content_ack_survives_failure_of_view_receipt_without_second_send(self):
        sink=Sink();sender=MarketTextSender(sink,jobs_dir=self.root,now_fn=lambda:NOW);r=result()
        with patch('thailand.market_telegram._persist_delivery',side_effect=OSError('view receipt failure')):
            with self.assertRaises(OSError):sender.send({'requested_view':'all','recipients':['42']},r,current=False,historical=True)
        sender.send({'requested_view':'personal_top3','recipients':['42']},r,current=False,historical=True);self.assertEqual(len(sink.messages),1)
    def test_whole_presentation_required_not_just_one_equal_card(self):
        sink=Sink();sender=MarketTextSender(sink,jobs_dir=self.root,now_fn=lambda:NOW);r=result()
        sender.send({'requested_view':'all','recipients':['42']},r,current=False,historical=True)
        first=sink.messages[0][0]
        row=copy.deepcopy(r['rankings']['thai_top_value'][0]);row['model_id']='other';row['variant_id']='other';row['name']='Different second card'
        row['offers'][0]['offer_id']='other-offer';row['best_offer_id']='other-offer'
        r['rankings']['thai_top_value'].append(row)
        sender.send({'requested_view':'personal_top3','recipients':['42']},r,current=False,historical=True)
        self.assertEqual(sink.messages[1][0],first)
        self.assertEqual(len(sink.messages),3)
    def test_corrupt_content_receipt_stops_before_another_http(self):
        sink=Sink();sender=MarketTextSender(sink,jobs_dir=self.root,now_fn=lambda:NOW);r=result()
        sender.send({'requested_view':'all','recipients':['42']},r,current=False,historical=True)
        path=next((self.root/'receipts').glob('content-*.json'));path.write_text('corrupt')
        with self.assertRaisesRegex(RuntimeError,'unreadable_content_receipt'):
            sender.send({'requested_view':'general_top','recipients':['42']},r,current=False,historical=True)
        self.assertEqual(len(sink.messages),1)
    def test_ru_empty_and_human_readable_times_units_coverage(self):
        r=result();r['comparison']['ru_input_summary']={'offer_count':0,'exclusion_reasons':{'stale_observation':200,'price_over_cap':1}}
        r['coverage']=[{'source':'advice','state':'failed'},{'source':'speedcom','state':'429'},{'source':'banana','state':'deferred','enabled':False,'policy_disabled':True}]
        text='\n'.join(p['text'] for p in format_market_messages(r,{'requested_view':'all'},now=NOW))
        for term in ('Российское сравнение сейчас недоступно','не прошло ни одного предложения','RAM 32 ГБ','SSD 1024 ГБ','МСК','со сбоем','ограничил запросы','отключён политикой'):self.assertIn(term,text)
        for term in ('+00:00','inspected_coverage_incomplete','partial','deferred'):self.assertNotIn(term,text)

if __name__=='__main__':unittest.main()


class EvidenceCacheRevision(unittest.TestCase):
    def test_pre_fix_catalog_scope_cannot_be_reused(self):
        import hashlib
        import thai_catalog as tc
        from thailand.catalog_reuse import evaluate_catalog_reuse
        from unittest.mock import patch
        from datetime import datetime, timezone
        old_payload={"gpu_queries":list(tc.GPU_SEARCH_QUERIES),"line_queries":list(tc.LINE_SEARCH_QUERIES),"query_budget":tc.QUERY_BUDGET,"stores":[{"source":a.slug,"enabled":bool(a.enabled)} for a in tc.get_thailand_adapters()]}
        old_scope=hashlib.sha256(json.dumps(old_payload,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        self.assertNotEqual(old_scope,tc.discovery_scope_hash())
        # Test only the saved old catalogue read; the planner and scope checks are real.
        with patch("thailand.catalog_reuse.load_thai_catalog",return_value={"snapshot_id":"old","discovery_scope_hash":old_scope}):
            decision=evaluate_catalog_reuse("unused",now=datetime(2026,10,17,tzinfo=timezone.utc),instance_id="test")
        self.assertIn(decision["action"],("DISCOVER","DEFERRED"))
        self.assertIn("discovery_scope_incompatible",decision["reasons"])
