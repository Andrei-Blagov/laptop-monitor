"""Read-only SQLite copy primitives, retained infra-ops acceptance implementation.

Adapted from infra-ops laptop_isolated.py (copy-helper SHA e320082c340e9956bffbdeccc0e36476eb7b1508028b09fef71d37908dcf4599).
No production access, implicit fallback or immutable live-WAL mode.
"""
import hashlib,json,os,sqlite3,stat,time,uuid
from pathlib import Path,PurePosixPath

class Refused(RuntimeError):
    pass

def require(condition, message):
    if not condition:
        raise Refused(message)

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def child_path(parent, relative):
    rel = PurePosixPath(relative)
    require(bool(relative) and not rel.is_absolute() and str(rel) == relative
            and all(part not in ('', '.', '..') for part in rel.parts)
            and '\\' not in relative and '\x00' not in relative, 'unsafe relative path')
    return Path(parent).joinpath(*rel.parts)

def safe_path(path, *, directory=False, uid=None, private=False):
    path = Path(path)
    require(path.is_absolute() and path == Path(os.path.abspath(path)), 'path must be absolute and normalized')
    for part in [*reversed(path.parents), path]:
        st = part.lstat()
        require(not stat.S_ISLNK(st.st_mode), 'symlink refused')
        # /tmp and /var/tmp are permitted only as sticky ancestors, never targets.
        require(not st.st_mode & stat.S_IWOTH or (part != path and st.st_mode & stat.S_ISVTX), 'world-writable path')
    st = path.lstat()
    require(stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode), 'wrong file type')
    if not directory:
        require(st.st_nlink == 1, 'hard-linked input refused')
    if uid is not None:
        require(st.st_uid == uid, 'unexpected owner')
    if private:
        require(not st.st_mode & 0o077, 'private permissions required')
    return path

def disjoint(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return not (a.is_relative_to(b) or b.is_relative_to(a))

def write_json(path, data):
    path = Path(path)
    safe_path(path.parent, directory=True, uid=os.geteuid(), private=True)
    if path.exists() or path.is_symlink():
        safe_path(path, uid=os.geteuid(), private=True)
    temp = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temp.unlink(missing_ok=True)

def require_readonly_view(source):
    """No SQLite connection before this kernel-enforced precondition."""
    source = safe_path(source, directory=True)
    require(os.statvfs(source).f_flag & os.ST_RDONLY, 'source filesystem view is not read-only; copy refused')
    return source

def verify_autonomous_copy(path):
    safe_path(path)
    with path.open('rb') as stream:
        require(stream.read(20)[18:20] == b'\x01\x01', 'copy not standalone DELETE journal')
    require(not any(Path(str(path) + suffix).exists() for suffix in ('-wal','-shm','-journal')), 'copy still depends on sidecars')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as con:
        con.execute('PRAGMA query_only=ON')
        require(con.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'copy integrity failed')
    require(not any(Path(str(path) + s).exists() for s in ('-wal','-shm','-journal')), 'read created copy sidecars')

def backup_connection(src, dest):
    """SQLite mechanics; caller must already enforce source read-only at OS level.

    Exposed separately for genuine SQLite unit tests without faking a ro mount.
    """
    started = time.monotonic()
    dst = sqlite3.connect(dest)
    try:
        src.execute('PRAGMA query_only=ON')
        generation = src.execute('PRAGMA data_version').fetchone()[0]
        def progress(*_):
            if time.monotonic()-started > 15:
                raise TimeoutError('SQLite backup deadline')
        src.backup(dst,pages=64,progress=progress,sleep=0.05)
        stable = src.execute('PRAGMA data_version').fetchone()[0] == generation
        dst.commit()
        require(dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete','copy journal conversion failed')
    finally:
        dst.close()
    verify_autonomous_copy(dest)
    return stable
