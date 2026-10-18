import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from ops.state_retention import plan,apply
from ops.state_copy import Refused
AT=datetime(2026,10,18,tzinfo=timezone.utc)
class RetentionGraph(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.root.chmod(0o700)
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value));return p
    def result(self,ident,snapshot):
        return self.write(f'thailand_jobs/results/{ident}.json',{'result_id':ident,'scan_id':ident,'snapshot_id':snapshot,'generated_at':(AT-timedelta(days=31)).isoformat()})
    def snapshot(self,ident):
        return self.write(f'thai_catalog/snapshots/{ident}.json',{'snapshot_id':ident,'created_at':(AT-timedelta(days=31)).isoformat()})
    def test_deferred_and_ack_protect_shared_dependencies(self):
        self.result('one','shared');self.result('two','shared');self.snapshot('shared')
        self.write('thailand_jobs/deferred/future.json',{'result_id':'one','not_before':'2099-01-01T00:00:00+00:00'})
        self.write('thailand_jobs/receipts/content-ack.json',{'result_id':'two','acknowledged_indexes':[0]})
        self.assertEqual(plan(self.root,now=AT),[])
    def test_unreferenced_pair_deleted_latest_and_history_preserved(self):
        old=self.result('unused','unused-snapshot');snap=self.snapshot('unused-snapshot');latest=self.snapshot('latest')
        self.write('thai_catalog/latest.json',{'snapshot_id':'latest'})
        history=self.write('buy_state.json',{'history':[1,2,3],'retry_after':'2099-01-01T00:00:00+00:00'})
        before=history.read_bytes();result=apply(self.root,now=AT)
        self.assertEqual(result['removed_count'],2);self.assertFalse(old.exists());self.assertFalse(snap.exists())
        self.assertTrue(latest.exists());self.assertEqual(history.read_bytes(),before)
    def test_corrupt_dependency_stops_before_delete(self):
        old=self.result('unused','x');p=self.write('thailand_jobs/pending/bad.json',{});p.write_text('not JSON')
        with self.assertRaises(ValueError):apply(self.root,now=AT)
        self.assertTrue(old.exists())
    def test_symlink_refused(self):
        target=self.result('unused','x');p=self.root/'thailand_jobs/pending/link.json';p.parent.mkdir();p.symlink_to(target)
        with self.assertRaises(Refused):apply(self.root,now=AT)
        self.assertTrue(target.exists())
    def test_pending_source_ru_snapshot_is_a_dependency_before_result_exists(self):
        snap=self.write('russian_market/snapshots/source-ru.json',{'snapshot_id':'source-ru','created_at':(AT-timedelta(days=40)).isoformat()})
        self.write('thailand_jobs/pending/daily.json',{'source_ru_snapshot_id':'source-ru'})
        self.assertEqual(apply(self.root,now=AT)['removed_count'],0)
        self.assertTrue(snap.exists())
    def test_transitive_result_reference_keeps_snapshot(self):
        self.result('one','s1');second=self.result('two','s2');self.snapshot('s2')
        self.write('thailand_jobs/pending/e.json',{'result_id':'one'})
        self.write('thailand_jobs/results/one.json',{'result_id':'one','scan_id':'one','generated_at':(AT-timedelta(days=40)).isoformat(),'nested':{'result_id':'two'}})
        self.assertEqual(plan(self.root,now=AT),[])
if __name__=='__main__':unittest.main()
