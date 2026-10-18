"""One scheduled full backup + graph retention; host-held shared kernel leases.

No remote Docker, network client, credentials, service restart or arbitrary command.
Run from the installed project, via its existing backup systemd service.
"""
from __future__ import annotations
import fcntl,json,os,stat,subprocess,sys
from contextlib import ExitStack
from pathlib import Path
ROOT=Path('/opt/laptop-monitor')
LEASES=('laptop_monitor.lock.lease','thailand_jobs/scan.lease',
        'thailand_jobs/delivery.lease','thailand_jobs/queue.lock')

def hold(root, stack):
    """Acquire in worker order, without waiting or stealing any active lease."""
    from ops.state_copy import require,safe_path
    safe_path(root,directory=True,uid=10001)
    for relative in LEASES:
        path=root/relative
        safe_path(path.parent,directory=True,uid=10001)
        try:
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
            os.fchown(fd,10001,10001);os.write(fd,b'0')
        except FileExistsError:
            safe_path(path,uid=10001)
            fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
        stack.callback(os.close,fd)
        st=os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_uid==10001,'unsafe shared lease')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    metadata=root/'laptop_monitor.lock'
    if metadata.exists():
        safe_path(metadata,uid=10001)
        require(json.loads(metadata.read_text()).get('protocol')=='kernel-v1','legacy pipeline owner unresolved')

def main():
    if os.geteuid()!=0 or Path.cwd()!=ROOT:
        raise RuntimeError('fixed installed project/root execution required')
    sys.path.insert(0,str(ROOT))
    from ops.state_copy import safe_path
    safe_path(ROOT,directory=True)
    backup=ROOT/'data/backups'
    if not backup.exists():
        backup.mkdir(mode=0o700);os.chown(backup,10001,10001)
    safe_path(backup,directory=True,uid=10001,private=True)
    env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
         'DOCKER_HOST':'unix:///var/run/docker.sock','PYTHONDONTWRITEBYTECODE':'1'}
    with ExitStack() as stack:
        hold(ROOT/'data',stack)
        subprocess.run([str(ROOT/'deploy/compose.sh'),'run','--rm','--no-deps','backup'],
                       cwd=ROOT,env=env,check=True,timeout=600)
        # No deletion is possible if full backup failed. The unprivileged
        # process sees the same locked state and never receives secrets.
        subprocess.run(['/usr/bin/python3','-B','-m','ops.state_retention','--root',str(ROOT/'data')],
                       cwd=ROOT,env=env,check=True,timeout=180,user=10001,group=10001)
    print('PASS: full state backup and graph retention; leases released')

if __name__=='__main__':main()
