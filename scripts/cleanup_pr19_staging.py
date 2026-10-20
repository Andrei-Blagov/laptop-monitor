"""One fixed failed PR19 run. Inventory by default; --apply removes only proved waste.

Run through the existing infra-ops reviewed SSH boundary, as root, after retaining
and SHA-verifying the reviewed-package artifact on the operator's machine.
No containers are started/stopped, no production paths or volumes are modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile

RUN = '39376742146'
ROOT = Path('/var/tmp/lm-pr19-' + RUN)
TAG = 'lm-pr19:' + RUN
IMAGE = 'sha256:fcfddd74df2a52a89f4456f3edb42ac5ae9a1bc46c75749d911e5ebdc1eff218'
CONFIG = 'sha256:80b09e5b499b30ca154444bc454064b9b0979087fd9cced2529dbe92b4905ed0'
ARCHIVE = 'f0ff205ede0c9fb20b5a88e042f57235618e533889128375ae6ef74d715f453f'
APP = 'a91d60e6ea8df95f69b41c9b6d5951c92d75a18f'
PRODUCTION = '7c93423b5b46d508deb8f4e063e6d2ee690184cba668c283f0d94459dfddc784'
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
       'LC_ALL': 'C', 'DOCKER_HOST': 'unix:///var/run/docker.sock'}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, env=ENV)
    require(result.returncode == 0, 'command failed: ' + args[0] + ' ' + args[1])
    return result.stdout


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def tree(root=ROOT, *, expected_inode=788, owner=1001):
    require(not root.parent.is_symlink() and not root.is_symlink(), 'symlink stage')
    st = root.lstat()
    require(stat.S_ISDIR(st.st_mode) and st.st_ino == expected_inode and
            st.st_uid == owner and st.st_dev == 2049 and stat.S_IMODE(st.st_mode) == 0o700, 'stage identity changed')
    require(not os.path.ismount(root), 'stage is a mount')
    require({p.name for p in root.iterdir()} == {'candidate.tar', 'data', 'evidence'}, 'unknown stage entries')
    archive = root / 'candidate.tar'
    a = archive.lstat()
    require(stat.S_ISREG(a.st_mode) and a.st_nlink == 1 and a.st_uid == owner and
            stat.S_IMODE(a.st_mode) == 0o600 and a.st_dev == st.st_dev, 'unsafe archive')
    require(0 < a.st_size <= 2 * 1024**3, 'archive size outside limit')
    for name in ('data', 'evidence'):
        p = root / name
        d = p.lstat()
        require(stat.S_ISDIR(d.st_mode) and d.st_uid == 10001 and d.st_dev == st.st_dev and
                stat.S_IMODE(d.st_mode) == 0o700 and not os.path.ismount(p), 'unsafe fixture directory')
        require(not list(p.iterdir()), 'fixture data/evidence is not empty: preserve for review')
    return {'inode': st.st_ino, 'device': st.st_dev, 'archive_bytes': a.st_size,
            'allocated_bytes': sum(p.lstat().st_blocks * 512 for p in (root, archive, root/'data', root/'evidence')),
            'archive_inode': a.st_ino, 'archive_mtime_ns': a.st_mtime_ns}


def archive_config(root=ROOT):
    path = root / 'candidate.tar'
    require(digest(path) == ARCHIVE, 'archive checksum mismatch')
    with tarfile.open(path, 'r:') as archive:
        members = archive.getmembers()
        require(len(members) < 20000 and len({m.name for m in members}) == len(members), 'archive member ambiguity')
        for m in members:
            require((m.isfile() or m.isdir()) and not m.issparse() and
                    not m.name.startswith('/') and '..' not in Path(m.name).parts, 'unsafe archive entry')
        manifest = archive.getmember('manifest.json')
        require(manifest.isfile() and manifest.size < 65536, 'unsafe manifest')
        rows = json.load(archive.extractfile(manifest))
        require(len(rows) == 1 and rows[0]['RepoTags'] == [TAG], 'archive image/tag mismatch')
        config = archive.getmember(rows[0]['Config'])
        require(config.isfile() and config.size < 2 * 1024**2, 'unsafe config')
        raw = archive.extractfile(config).read()
        require('sha256:' + hashlib.sha256(raw).hexdigest() == CONFIG, 'archive config mismatch')
        return json.loads(raw)


def image_owned(image, config):
    require(image['Id'] == IMAGE and image['RepoTags'] == [TAG], 'Docker descriptor/tag changed')
    labels = image['Config'].get('Labels') or {}
    require(labels.get('infra-ops.scope') == 'laptop-pr19-issue24' and
            labels.get('infra-ops.run-id') == RUN and
            labels.get('org.opencontainers.image.revision') == APP, 'image provenance mismatch')
    require(image['RootFS']['Type'] == config['rootfs']['type'] and
            image['RootFS']['Layers'] == config['rootfs']['diff_ids'], 'image layers differ from recovery archive')
    require(image['Config'] == config['config'], 'image config differs from recovery archive')


def references(root=ROOT):
    ids = command(['docker', 'ps', '-aq', '--no-trunc']).split()
    for cid in ids:
        obj = json.loads(command(['docker', 'inspect', cid]))[0]
        require(obj['Image'] not in (IMAGE, CONFIG) and obj['Config']['Image'] not in (TAG, IMAGE, CONFIG), 'image has container reference')
        for mount in obj.get('Mounts') or []:
            source = Path(mount.get('Source') or '/__absent__')
            require(not (source == root or source in root.parents or root in source.parents), 'stage has container mount reference')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for link in [proc/'cwd', proc/'root', *list((proc/'fd').iterdir())]:
                try:
                    value = Path(os.readlink(link).removesuffix(' (deleted)'))
                except FileNotFoundError:
                    continue
                require(value != root and root not in value.parents, 'stage has process reference')
        except FileNotFoundError:
            continue


def production():
    obj = json.loads(command(['docker', 'inspect', 'laptop-monitor-control']))[0]
    require(obj['Id'] == PRODUCTION and obj['State']['Running'] and
            obj['Config']['Image'] == 'laptop-monitor:0.12.1', 'production changed since inventory')
    return {'id': obj['Id'], 'image': obj['Image'], 'started_at': obj['State']['StartedAt'],
            'restarts': obj['RestartCount']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, 'root required for complete reference checks')
    before = production()
    inventory = tree()
    config = archive_config()
    image = json.loads(command(['docker', 'image', 'inspect', TAG]))[0]
    image_owned(image, config)
    references()
    require(tree() == inventory, 'stage changed during checks')
    free_before = shutil.disk_usage(ROOT).free
    report = {'mode': 'apply' if args.apply else 'inventory', 'run': RUN,
              'stage': str(ROOT), **inventory, 'image': IMAGE, 'tag': TAG,
              'archive_sha256': ARCHIVE, 'archive_config_id': CONFIG,
              'free_before_bytes': free_before, 'removed': []}
    report['status'] = 'PASS'
    try:
        if args.apply:
            require(production() == before, 'production changed during checks')
            image_owned(json.loads(command(['docker', 'image', 'inspect', TAG]))[0], config)
            references()
            command(['docker', 'image', 'rm', IMAGE])  # no force; Docker also rejects live references
            report['removed'].append(IMAGE)
            require(IMAGE not in command(['docker', 'image', 'ls', '--no-trunc', '--quiet']).split(), 'image still present')
            require(not command(['docker', 'image', 'ls', '--quiet', TAG]).strip(), 'tag still present')
            require(tree() == inventory and digest(ROOT/'candidate.tar') == ARCHIVE, 'stage changed before deletion')
            references()
            # Non-recursive removal: a new child makes rmdir fail, never disappear.
            (ROOT/'data').rmdir()
            (ROOT/'evidence').rmdir()
            (ROOT/'candidate.tar').unlink()
            ROOT.rmdir()
            report['removed'].append(str(ROOT))
    except Exception as exc:
        report['status'] = 'FAILED'
        report['error'] = str(exc)
        raise
    finally:
        report['free_after_bytes'] = shutil.disk_usage(ROOT.parent).free
        try:
            report['production_unchanged'] = production() == before
        except Exception as exc:
            report['production_unchanged'] = False
            report['production_check_error'] = type(exc).__name__
        if not report['production_unchanged']:
            report['status'] = 'FAILED'
        print(json.dumps(report, indent=2))
    require(report['production_unchanged'], 'production changed; investigate without retry')



if __name__ == '__main__':
    main()
