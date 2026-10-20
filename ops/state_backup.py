"""Full private state copies and bounded retention of verified owned backups."""
from __future__ import annotations
import json, os, shutil, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from ops.state_copy import (Refused, require, safe_path, child_path, digest, disjoint,
                            write_json, require_readonly_view, backup_connection, verify_autonomous_copy)
FORMAT = 'laptop-monitor-full-state-v1'
DB = 'laptop_monitor.db'
SIDECARS = {DB+'-wal', DB+'-shm', DB+'-journal'}


def inventory(root):
    safe_path(root, directory=True)
    found = {}
    # Do not descend into retained backups. Never treat a directory name alone
    # as ownership evidence when deleting; deletion uses the complete manifest.
    def walk(directory):
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if rel == 'backups':
                safe_path(path, directory=True)
                continue
            safe_path(path, directory=path.is_dir())
            require(not os.path.ismount(path), 'nested data mount refused')
            st = path.stat()
            found[rel] = dict(directory=path.is_dir(), inode=st.st_ino, uid=st.st_uid,
                              size=st.st_size, mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns,
                              sha256=None if path.is_dir() else digest(path))
            if path.is_dir(): walk(path)
    walk(root)
    return found


def verify_copy(root):
    safe_path(root, directory=True, uid=os.geteuid(), private=True)
    report_path = safe_path(root/'manifest.json', uid=os.geteuid(), private=True)
    record = json.loads(report_path.read_text())
    require(record.get('format') == FORMAT and record.get('status') == 'PASS', 'unowned/incomplete backup')
    safe_path(root/'data', directory=True, uid=os.geteuid(), private=True)
    files, dirs = {}, []
    for p in sorted((root/'data').rglob('*')):
        safe_path(p, directory=p.is_dir(), uid=os.geteuid(), private=True)
        require(not os.path.ismount(p), 'backup contains mount')
        rel = p.relative_to(root/'data').as_posix()
        if p.is_dir(): dirs.append(rel)
        else: files[rel] = digest(p)
    require(files == record['files'] and dirs == record['directories'], 'backup composition/hash changed')
    require({p.name for p in root.iterdir()} == {'data','manifest.json'}, 'extra backup member')
    verify_autonomous_copy(root/'data'/DB)
    json.loads((root/'data'/'product_specs.json').read_text())
    return record


def create_backup(source, destination, *, now=None):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    require_readonly_view(source)
    safe_path(destination, directory=True, uid=os.geteuid(), private=True)
    require(disjoint(source,destination), 'backup destination overlaps source')
    # An output bind may map source/backups elsewhere in the container. That
    # subtree is explicitly excluded above and must not affect source records.
    before = inventory(source)
    require(DB in before and 'product_specs.json' in before, 'required state missing')
    with (source/DB).open('rb') as stream: wal = stream.read(20)[18:20] == b'\x02\x02'
    if wal:
        require(all((source/(DB+s)).is_file() for s in ('-wal','-shm')), 'live WAL sidecars unavailable')
    created = now or datetime.now(timezone.utc)
    require(created.tzinfo is not None, 'explicit zoned backup time required')
    root = destination/('daily-'+created.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex)
    root.mkdir(mode=0o700);inode=root.stat().st_ino
    try:
        data=root/'data';data.mkdir(mode=0o700)
        with sqlite3.connect((source/DB).as_uri()+'?mode=ro',uri=True,timeout=2) as src:
            require(backup_connection(src,data/DB), 'database changed during backup')
        (data/DB).chmod(0o600)
        for rel, meta in before.items():
            if rel == DB or rel in SIDECARS: continue
            src, target = child_path(source,rel), child_path(data,rel)
            if meta['directory']:
                target.mkdir(mode=0o700)
            else:
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as out, src.open('rb') as inp: shutil.copyfileobj(inp,out)
                require(digest(target)==meta['sha256'], 'state member changed during copy')
        require(before==inventory(source), 'state changed while copying; no backup accepted')
        record={'format':FORMAT,'status':'PASS','created_at':created.isoformat(),
                'source_unchanged':True,'source_guard':'kernel_readonly_filesystem',
                'files':{p.relative_to(data).as_posix():digest(p) for p in sorted(data.rglob('*')) if p.is_file()},
                'directories':[p.relative_to(data).as_posix() for p in sorted(data.rglob('*')) if p.is_dir()]}
        write_json(root/'manifest.json',record)
        verify_copy(root)
        return root
    except BaseException:
        # Only the just-created, proved private inode, never a name-pattern deletion.
        safe_path(root,directory=True,uid=os.geteuid(),private=True)
        require(root.stat().st_ino==inode,'backup work identity changed')
        for p in root.rglob('*'):
            safe_path(p,directory=p.is_dir(),uid=os.geteuid(),private=True)
            require(not os.path.ismount(p),'backup work mount')
        require(shutil.rmtree.avoids_symlink_attacks,'safe tree removal unavailable')
        shutil.rmtree(root)
        raise


def retain_daily(destination, *, keep=7):
    require(isinstance(keep,int) and keep>=2,'at least two verified daily backups required')
    root=Path(destination).absolute();safe_path(root,directory=True,uid=os.geteuid(),private=True)
    owned=[];skipped=[]
    for path in root.iterdir():
        if not path.name.startswith('daily-'):
            continue  # Legacy/manual/rollback backups are not ours to prune.
        try:
            record=verify_copy(path)
            created=datetime.fromisoformat(record['created_at'])
            require(created.tzinfo is not None,'unverifiable backup time')
            owned.append((created,path,path.stat().st_ino,digest(path/'manifest.json')))
        except (OSError,ValueError,KeyError,Refused):
            skipped.append(path.name)
    owned.sort(key=lambda row:(row[0],row[1].name),reverse=True)
    removed=[]
    # Verify the retained recovery set before every deletion batch.
    for _, path, _, _ in owned[:keep]: verify_copy(path)
    for _, path, inode, sha in owned[keep:]:
        verify_copy(path)
        require(path.parent==root and path.stat().st_ino==inode and digest(path/'manifest.json')==sha,'backup identity changed')
        require(not os.path.ismount(path) and shutil.rmtree.avoids_symlink_attacks,'unsafe backup deletion')
        shutil.rmtree(path);require(not path.exists(),'old backup remains');removed.append(path.name)
    return {'kept':len(owned[:keep]),'removed':removed,'skipped':skipped}
