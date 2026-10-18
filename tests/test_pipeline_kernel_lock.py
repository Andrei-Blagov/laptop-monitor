import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from pipeline_lock import acquire_pipeline_lock,release_pipeline_lock,PipelineLockError

class KernelLockTests(unittest.TestCase):
    def test_live_owner_then_process_death_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'pipeline.lock'
            code="import sys;from pipeline_lock import acquire_pipeline_lock;acquire_pipeline_lock(sys.argv[1]);print('ready',flush=True);sys.stdin.read()"
            child=subprocess.Popen([sys.executable,'-c',code,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'ready')
                with self.assertRaises(PipelineLockError):acquire_pipeline_lock(path)
                child.kill();child.wait(timeout=5)
                acquire_pipeline_lock(path);release_pipeline_lock(path,expected_pid=os.getpid())
                self.assertFalse(path.exists());self.assertTrue(path.with_name(path.name+'.lease').exists())
            finally:
                if child.poll() is None:child.kill();child.wait(timeout=5)
                child.stdin.close();child.stdout.close()
    def test_same_process_cannot_reenter_or_release_other_owner(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'lock';acquire_pipeline_lock(p)
            try:
                with self.assertRaises(PipelineLockError):acquire_pipeline_lock(p)
                release_pipeline_lock(p,expected_pid=os.getpid()+1)
                with self.assertRaises(PipelineLockError):acquire_pipeline_lock(p)
            finally:release_pipeline_lock(p,expected_pid=os.getpid())
    def test_legacy_namespace_pid_is_never_used_as_ownership_proof(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'lock';p.write_text(json.dumps({'pid':99999999,'started_at':'old'}));before=p.read_bytes()
            with self.assertRaises(PipelineLockError):acquire_pipeline_lock(p)
            self.assertEqual(p.read_bytes(),before)

if __name__=='__main__':unittest.main()
