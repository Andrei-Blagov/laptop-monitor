"""Full-state retention proof and actual SQLite mechanics without network."""
import json,os,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from ops.state_backup import FORMAT,create_backup,retain_daily,verify_copy
from ops.state_copy import Refused,backup_connection,digest,write_json

class FullBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.root.chmod(0o700)
    def tearDown(self):self.tmp.cleanup()
    def prepared(self,number):
        root=self.root/f'daily-fixture-{number:02}';root.mkdir(mode=0o700);data=root/'data';data.mkdir(mode=0o700)
        with sqlite3.connect(data/'laptop_monitor.db') as c:c.execute('CREATE TABLE history(price INTEGER)');c.execute('INSERT INTO history VALUES (230000)')
        (data/'product_specs.json').write_text('{}')
        for p in data.iterdir():p.chmod(0o600)
        write_json(root/'manifest.json',{'format':FORMAT,'status':'PASS','created_at':(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=number)).isoformat(),
            'files':{p.name:digest(p) for p in data.iterdir()},'directories':[]})
        return root
    def test_retention_seven_verified_copies_keeps_unknown_and_rollback(self):
        paths=[self.prepared(n) for n in range(10)]
        unknown=self.root/'daily-unknown';unknown.mkdir(mode=0o700)
        rollback=self.root/'rollback';rollback.mkdir(mode=0o700)
        report=retain_daily(self.root)
        self.assertEqual(report['kept'],7);self.assertEqual(len(report['removed']),3)
        self.assertFalse(paths[0].exists());self.assertTrue(paths[-1].exists())
        self.assertTrue(unknown.exists());self.assertTrue(rollback.exists())
    def test_changed_file_is_preserved_as_unknown(self):
        paths=[self.prepared(n) for n in range(9)]
        (paths[0]/'data/product_specs.json').write_text('{"changed":true}')
        report=retain_daily(self.root)
        self.assertIn(paths[0].name,report['skipped']);self.assertTrue(paths[0].exists())
    def test_foreign_symlink_never_followed_or_deleted(self):
        target=self.prepared(1);link=self.root/'daily-link';link.symlink_to(target,target_is_directory=True)
        report=retain_daily(self.root)
        self.assertTrue(link.is_symlink());self.assertTrue(target.exists());self.assertIn(link.name,report['skipped'])
    def test_writable_source_refused_before_backup_creation(self):
        source=self.root/'source';source.mkdir(mode=0o700);out=self.root/'out';out.mkdir(mode=0o700)
        with self.assertRaises(Refused):create_backup(source,out)
        self.assertEqual(list(out.iterdir()),[])
    def test_sqlite_delete_and_live_wal_backup_is_autonomous(self):
        for mode in ('DELETE','WAL'):
            source=self.root/(mode+'.db');dest=self.root/(mode+'-copy.db')
            with sqlite3.connect(source) as c:
                c.execute('PRAGMA journal_mode='+mode);c.execute('CREATE TABLE receipts(value TEXT)');c.execute("INSERT INTO receipts VALUES ('durable')");c.commit()
                self.assertTrue(backup_connection(c,dest))
            with sqlite3.connect(dest) as copy:self.assertEqual(copy.execute('SELECT value FROM receipts').fetchall(),[('durable',)])
            self.assertFalse(Path(str(dest)+'-wal').exists())

if __name__=='__main__':unittest.main()
