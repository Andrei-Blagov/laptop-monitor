"""Real separate-container reproduction of the legacy RU PID lock. No network."""
import argparse,hashlib,json,os,subprocess,tempfile,time,uuid
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--expect',choices=['defect','fixed'],required=True);a=p.parse_args()
    source=a.source.resolve();module=source/'pipeline_lock.py'
    image='python:3.12-slim-bookworm';endpoint='unix:///var/run/docker.sock'
    env={'PATH':os.environ['PATH'],'DOCKER_HOST':endpoint,'LC_ALL':'C'}
    def docker(*args,check=True):
        r=subprocess.run(['docker','--host='+endpoint,*args],capture_output=True,text=True,env=env,timeout=180)
        if check and r.returncode:raise RuntimeError('Docker operation failed: '+args[0])
        return r
    docker('pull',image);image_id=json.loads(docker('image','inspect',image).stdout)[0]['Id']
    name='lm-ru-lock-proof-'+uuid.uuid4().hex;report={'status':'FAILED','expected':a.expect,'module_sha256':hashlib.sha256(module.read_bytes()).hexdigest(),'image_id':image_id}
    output=Path('dist/checks');output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lm-ru-namespace-') as tmp:
        work=Path(tmp);work.chmod(0o700);os.chown(work,10001,10001)
        common=['--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','10001:10001','--label','lm.proof='+name,
                '--mount',f'type=bind,src={source},dst=/source,readonly','--mount',f'type=bind,src={work},dst=/proof','--env','PYTHONPATH=/source','--env','PYTHONDONTWRITEBYTECODE=1']
        child="import os,time;from pathlib import Path;from pipeline_lock import acquire_pipeline_lock;acquire_pipeline_lock('/proof/lock');Path('/proof/ready').write_text(str(os.getpid()));time.sleep(120)"
        parent="import subprocess,sys;subprocess.run([sys.executable,'-c',"+repr(child)+"],check=True)"
        try:
            docker('create','--name',name,*common,image_id,'python','-c',parent);docker('start',name)
            deadline=time.monotonic()+20
            while not (work/'ready').exists():
                if time.monotonic()>deadline:raise RuntimeError('owner not ready')
                time.sleep(.1)
            holder=json.loads(docker('container','inspect',name).stdout)[0]
            if not holder['State']['Running'] or holder['HostConfig']['PidMode'] or holder['HostConfig']['NetworkMode']!='none':raise RuntimeError('isolation proof failed')
            contender="""import os,json
from pipeline_lock import acquire_pipeline_lock,release_pipeline_lock,PipelineLockError
try:
 acquire_pipeline_lock('/proof/lock');acquired=True;release_pipeline_lock('/proof/lock',expected_pid=os.getpid())
except PipelineLockError:acquired=False
print(json.dumps({'acquired':acquired,'pid':os.getpid()}))
"""
            result=json.loads(docker('run','--name',name+'-contender','--rm',*common,image_id,'python','-c',contender).stdout)
            report.update(owner_pid=int((work/'ready').read_text()),contender=result,owner_still_running=json.loads(docker('container','inspect',name).stdout)[0]['State']['Running'])
            if result['acquired'] != (a.expect=='defect') or not report['owner_still_running']:raise RuntimeError('unexpected lock outcome')
            docker('stop','--time','1',name)
            recovered=json.loads(docker('run','--name',name+'-recover','--rm',*common,image_id,'python','-c',contender).stdout)
            if not recovered['acquired']:raise RuntimeError('lock not recovered after owner death')
            report['recovery_after_owner_exit']=recovered
            report['status']='PASS' 
        finally:
            for resource in (name+'-recover',name+'-contender',name):
                found=docker('container','inspect',resource,check=False)
                if found.returncode==0:
                    obj=json.loads(found.stdout)[0]
                    if obj['Config']['Labels'].get('lm.proof')!=name or obj['Image']!=image_id:raise RuntimeError('foreign test resource')
                    if obj['State']['Running']:docker('stop','--time','1',resource)
                    docker('rm',resource)
                docker('info','--format','{{.ID}}')
                if docker('container','inspect',resource,check=False).returncode==0:raise RuntimeError('test container remains')
            report['cleanup_verified']=True
            (output/('ru-lock-'+a.expect+'.json')).write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
if __name__=='__main__':main()
