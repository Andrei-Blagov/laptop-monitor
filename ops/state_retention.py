"""Delete only aged, unreferenced result/snapshot files; preserve all ACKs/state."""
from __future__ import annotations
import json,os
from datetime import timedelta
from pathlib import Path
from ops.state_copy import require,safe_path,digest,write_json
from market_contracts import moment

RESULT_KEYS={'result_id','scan_id','subscription_scan_id'}
SNAPSHOT_KEYS={'snapshot_id','ru_snapshot_id','requested_ru_snapshot_id','result_snapshot_id'}

def links(value,results,snapshots):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in RESULT_KEYS and isinstance(item,str):results.add(item)
            if key in SNAPSHOT_KEYS and isinstance(item,str):snapshots.add(item)
            if key=='snapshot_ids' and isinstance(item,list):snapshots.update(str(x) for x in item)
            links(item,results,snapshots)
    elif isinstance(value,list):
        for item in value:links(item,results,snapshots)


def read(path):
    safe_path(path,uid=os.geteuid())
    value=json.loads(path.read_text())
    require(isinstance(value,(dict,list)),'unknown state document')
    return value


def plan(root,*,now,days=30):
    require(moment(now) is not None and days>=30,'unsafe retention horizon')
    root=Path(root).absolute();safe_path(root,directory=True,uid=os.geteuid())
    result_dir=root/'thailand_jobs/results'
    snapshot_dirs=[root/'thai_catalog/snapshots',root/'russian_market/snapshots']
    excluded=[root/'backups',result_dir,*snapshot_dirs]
    result_refs,snapshot_refs=set(),set()
    # All queue states, ACKs, BUY state, latest pointers, and unknown JSON
    # roots participate. Invalid documents stop deletion instead of disappearing.
    for path in root.rglob('*.json'):
        if any(path.is_relative_to(p) for p in excluded):continue
        links(read(path),result_refs,snapshot_refs)
    results={}
    if result_dir.exists():
        safe_path(result_dir,directory=True,uid=os.geteuid())
        for path in result_dir.iterdir():
            safe_path(path,uid=os.geteuid());value=read(path)
            require(isinstance(value,dict) and value.get('result_id')==path.stem and value.get('scan_id')==path.stem,'unrecognized result')
            results[path.stem]=(path,value)
    remove=[];cutoff=now-timedelta(days=days)
    for ident,(path,value) in results.items():
        captured=moment(value.get('generated_at'))
        if ident in result_refs or captured is None or captured>=cutoff:
            links(value,result_refs,snapshot_refs)
        else:remove.append(path)
    # Re-evaluate transitive result references to a fixed point.
    changed=True
    while changed:
        previous=(len(result_refs),len(snapshot_refs))
        for ident,(_,value) in results.items():
            if ident in result_refs:links(value,result_refs,snapshot_refs)
        changed=previous!=(len(result_refs),len(snapshot_refs))
    remove=[p for p in remove if p.stem not in result_refs]
    for directory in snapshot_dirs:
        if not directory.exists():continue
        safe_path(directory,directory=True,uid=os.geteuid())
        for path in directory.iterdir():
            value=read(path)
            require(isinstance(value,dict) and value.get('snapshot_id')==path.stem,'unrecognized snapshot')
            captured=moment(value.get('created_at') or value.get('finished_at'))
            if path.stem not in snapshot_refs and captured is not None and captured<cutoff:remove.append(path)
    return [{'path':p.relative_to(root).as_posix(),'inode':p.stat().st_ino,'sha256':digest(p),'bytes':p.stat().st_size} for p in remove]


def apply(root,*,now):
    """Caller must hold scan, delivery, queue and RU pipeline leases."""
    root=Path(root).absolute();first=plan(root,now=now)
    # Re-read the complete dependency graph immediately before deleting.
    require(first==plan(root,now=now),'dependency graph changed')
    removed=[]
    for entry in first:
        path=root/entry['path'];safe_path(path,uid=os.geteuid())
        require(path.stat().st_ino==entry['inode'] and digest(path)==entry['sha256'],'retention member changed')
        path.unlink();removed.append(entry)
    return {'removed_count':len(removed),'removed_bytes':sum(x['bytes'] for x in removed),
            'ack_policy':'preserved; no ACK/receipt expiration introduced','history_preserved':True}
