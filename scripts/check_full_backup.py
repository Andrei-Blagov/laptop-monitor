"""Actual UID 10001, kernel read-only DELETE/WAL full-state backup and restore.

Synthetic data only. No network, secrets, Docker model or SQLite replacement.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,tempfile,uuid,sys
from contextlib import ExitStack
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deploy.full_state_maintenance import hold

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('run as root on isolated CI runner for real UID ownership')
    endpoint='unix:///var/run/docker.sock';env={'PATH':os.environ['PATH'],'DOCKER_HOST':endpoint,'LC_ALL':'C'}
    def docker(*cmd,check=True):
        p=subprocess.run(['docker','--host='+endpoint,*cmd],env=env,capture_output=True,text=True,timeout=180)
        if check and p.returncode:raise RuntimeError('Docker '+cmd[0]+' failed: '+p.stderr[-1000:])
        return p
    image=json.loads(docker('image','inspect',args.image).stdout)[0]
    image_id=image['Id'];prefix='lm-full-state-'+uuid.uuid4().hex
    report={'status':'FAILED','image_id':image_id,'modes':{},'containers':[]}
    out=ROOT/'dist/checks';out.mkdir(parents=True,exist_ok=True)
    def inventory(path):
        return {p.relative_to(path).as_posix():[p.stat().st_ino,p.stat().st_mtime_ns,p.stat().st_ctime_ns,
                None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()] for p in path.rglob('*')}
    with tempfile.TemporaryDirectory(prefix=prefix) as temp:
        root=Path(temp);root.chmod(0o700);os.chown(root,10001,10001)
        def run(label,mounts,code):
            name=prefix+'-'+label;report['containers'].append(name)
            docker('create','--name',name,'--label','lm.backup-proof='+prefix,'--network','none','--read-only',
                   '--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','10001:10001',
                   *sum((['--mount',m] for m in mounts),[]),image_id,'python','-B','-c',code)
            info=json.loads(docker('container','inspect',name).stdout)[0]
            if (info['Image']!=image_id or info['Config']['User']!='10001:10001' or
                info['HostConfig']['NetworkMode']!='none' or not info['HostConfig']['ReadonlyRootfs'] or
                any('TELEGRAM' in item for item in info['Config']['Env'])):raise RuntimeError('isolation differs')
            for mount in info['Mounts']:
                if mount['Destination']=='/source' and mount['RW']:raise RuntimeError('source writable')
            docker('start','--attach',name)
            end=json.loads(docker('container','inspect',name).stdout)[0]
            if end['State']['ExitCode']!=0:raise RuntimeError('fixture/helper failed: '+docker('logs',name).stdout[-1000:])
            return docker('logs',name).stdout
        try:
            for mode in ('DELETE','WAL'):
                source=root/mode;source.mkdir(mode=0o700);dest=root/(mode+'-backup');dest.mkdir(mode=0o700)
                for p in (source,dest):os.chown(p,10001,10001)
                con=sqlite3.connect(source/'laptop_monitor.db')
                leases=ExitStack()
                try:
                    con.execute('PRAGMA journal_mode='+mode);con.execute('CREATE TABLE history(price INTEGER, checked TEXT)')
                    con.execute("INSERT INTO history VALUES (230000, '2026-10-18T05:00:00+00:00')");con.commit()
                    documents={'product_specs.json':{'own_capture':'2026-10-17T00:00:00Z'},
                        'buy_state.json':{'history':[1,2],'last_alert':'retained'},
                        'source_health.json':{'retry_after':'2099-01-01T00:00:00Z'},
                        'thailand_jobs/deferred/event.json':{'event_id':'event','result_id':'result','not_before':'2099-01-01T00:00:00Z'},
                        'thailand_jobs/receipts/ack.json':{'result_id':'result','acknowledged_indexes':[0]},
                        'thailand_jobs/results/result.json':{'result_id':'result','scan_id':'result','snapshot_id':'snapshot','generated_at':'2026-01-01T00:00:00Z'},
                        'thai_catalog/snapshots/snapshot.json':{'snapshot_id':'snapshot','created_at':'2026-01-01T00:00:00Z'}}
                    for rel,data in documents.items():
                        p=source/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data))
                    for p in source.rglob('*'):os.chown(p,10001,10001);p.chmod(0o700 if p.is_dir() else 0o600)
                    watchdog=source/'n8n_watchdog_state.json'
                    watchdog.write_text(json.dumps({'result_id':'result','owner':'root watchdog'}));os.chown(watchdog,0,0);watchdog.chmod(0o644)
                    hold(source,leases)
                    # The host backup controller and a separate PID namespace
                    # must contend on the very same stable inode.
                    run(mode.lower()+'-busy',[f'type=bind,src={source},dst=/source,readonly'],
                        "import fcntl;f=open('/source/thailand_jobs/scan.lease','rb');\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);raise RuntimeError('live lease stolen')\nexcept BlockingIOError: print('busy owner respected')")
                    before=inventory(source)
                    run(mode.lower()+'-root-state',[f'type=bind,src={source},dst=/source,readonly'],
                        "import os;from pathlib import Path;from datetime import datetime,timezone;from ops.state_retention import plan;p=Path('/source');assert (p/'n8n_watchdog_state.json').stat().st_uid==0;assert os.getuid()==10001;assert plan(p,now=datetime(2026,10,18,tzinfo=timezone.utc))==[];print('root-owned dependency read; protected result retained')")
                    if before!=inventory(source):raise RuntimeError('dependency read changed source')
                    log=run(mode.lower(),[f'type=bind,src={source},dst=/source,readonly',f'type=bind,src={dest},dst=/backup'],
                        "import os,json;from ops.state_backup import create_backup,verify_copy;assert os.getuid()==10001;p=create_backup('/source','/backup');r=verify_copy(p);print(json.dumps({'status':r['status'],'files':len(r['files']),'source_guard':r['source_guard']}))")
                    if before!=inventory(source):raise RuntimeError('source directory or file changed')
                    backup=list(dest.iterdir())[0];data=backup/'data';restore=root/(mode+'-restore');shutil.copytree(data,restore)
                    for p in [restore,*restore.rglob('*')]:os.chown(p,10001,10001)
                    # Restore has no source mount: autonomous DB and every state file.
                    restored=run(mode.lower()+'-restore',[f'type=bind,src={restore},dst=/restore'],
                        "import json,sqlite3;from pathlib import Path;from ops.state_copy import verify_autonomous_copy;from ops.state_retention import apply;from datetime import datetime,timezone;p=Path('/restore');verify_autonomous_copy(p/'laptop_monitor.db');c=sqlite3.connect(p/'laptop_monitor.db');assert c.execute('SELECT price FROM history').fetchall()==[(230000,)];assert not apply(p,now=datetime(2026,10,18,tzinfo=timezone.utc))['removed_count'];print('restore and dependency graph PASS')")
                    if {p.relative_to(data).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}!={p.relative_to(restore).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in restore.rglob('*') if p.is_file()}:raise RuntimeError('restored state changed')
                    report['modes'][mode]={'backup':json.loads(log),'restore':'PASS','source_unchanged':True,'queue_receipts_retry_after_history':'preserved','uid':10001}
                finally:leases.close();con.close()
            report['status']='PASS'
        finally:
            errors=[]
            for name in report['containers']:
                found=docker('container','inspect',name,check=False)
                if found.returncode==0:
                    obj=json.loads(found.stdout)[0]
                    if obj['Image']!=image_id or obj['Config']['Labels'].get('lm.backup-proof')!=prefix:errors.append('ownership mismatch');continue
                    if obj['State']['Running']:docker('stop','--time','2',name)
                    docker('rm',name)
                docker('info','--format','{{.ID}}')
                if docker('container','inspect',name,check=False).returncode==0:errors.append('container remains')
            if errors:report['status']='FAILED';report['cleanup_errors']=errors
            # Context manager removes this newly allocated synthetic root only.
            report['cleanup_verified']=not errors
            (out/'full-state-backup.json').write_text(json.dumps(report,indent=2))
    if root.exists():raise RuntimeError('synthetic work remains')
    report['copies_removed']=True
    (out/'full-state-backup.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    if report['status']!='PASS':raise RuntimeError('full backup proof failed')

if __name__=='__main__':main()
