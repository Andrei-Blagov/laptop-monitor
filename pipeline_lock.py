from __future__ import annotations

"""Process lock для pipeline (Windows-compatible)."""

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DEFAULT_LOCK_PATH = Path("data") / "laptop_monitor.lock"


class PipelineLockError(Exception):
    """Не удалось захватить lock (уже занят живым процессом)."""


@dataclass
class LockInfo:
    pid: int
    started_at: str


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_is_running(pid: int) -> bool | None:
    """
    True = процесс жив, False = точно мёртв, None = неизвестно.

    На Windows OpenProcess; на POSIX os.kill(pid, 0).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                # ACCESS_DENIED (5) → процесс существует, нет прав
                err = ctypes.GetLastError()
                if err == 5:
                    return True
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def read_lock_info(lock_path: Path | str) -> LockInfo | None:
    path = Path(lock_path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(pid=int(raw["pid"]), started_at=str(raw["started_at"]))
    except Exception:
        return None


def _write_lock(path: Path, info: LockInfo) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": info.pid, "started_at": info.started_at, "protocol": "kernel-v1"}
    # Exclusive create — атомарно на Windows/POSIX.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(path), flags)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)


# The stable inode is never unlinked: PID namespaces do not affect its lock.
_HELD: dict[str, tuple[int, LockInfo]] = {}


def _after_fork() -> None:
    for fd, _ in _HELD.values():
        os.close(fd)  # Never LOCK_UN the parent's shared open-file description.
    _HELD.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def try_remove_stale_lock(lock_path: Path | str) -> bool:
    """Legacy PID files are never removed on a namespace-local PID guess."""
    return not Path(lock_path).exists()


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def acquire_pipeline_lock(lock_path: Path | str = DEFAULT_LOCK_PATH) -> LockInfo:
    path = Path(lock_path).absolute()
    key = str(path)
    if key in _HELD:
        raise PipelineLockError("Pipeline already running")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PipelineLockError("Unsafe pipeline lock")
    lease = path.with_name(path.name + ".lease")
    fd = os.open(lease, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise PipelineLockError("Unsafe pipeline lease")
        if st.st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PipelineLockError("Pipeline already running") from exc
        locked = True
        if path.exists():
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise PipelineLockError("Unsafe pipeline metadata")
            try:
                old = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise PipelineLockError("Unverified legacy pipeline lock") from exc
            if old.get("protocol") != "kernel-v1":
                raise PipelineLockError("Unverified legacy pipeline lock")
            # Only a dead kernel-v1 owner can leave metadata while this lease
            # is available. No cross-container PID lookup participates.
            path.unlink()
        info = LockInfo(pid=os.getpid(), started_at=_iso_now())
        _write_lock(path, info)
        _HELD[key] = (fd, info)
        return info
    except BaseException:
        if locked:
            _unlock(fd)
        else:
            os.close(fd)
        raise


def release_pipeline_lock(lock_path: Path | str = DEFAULT_LOCK_PATH, *, expected_pid: int | None = None) -> None:
    path = Path(lock_path).absolute()
    held = _HELD.get(str(path))
    if held is None:
        return
    fd, owner = held
    if owner.pid != os.getpid() or (expected_pid is not None and expected_pid != owner.pid):
        return
    _HELD.pop(str(path))
    try:
        if not path.is_symlink():
            info = read_lock_info(path)
            if info == owner:
                path.unlink(missing_ok=True)
    finally:
        _unlock(fd)


@contextmanager
def pipeline_lock(lock_path: Path | str = DEFAULT_LOCK_PATH) -> Iterator[LockInfo]:
    info = acquire_pipeline_lock(lock_path)
    try:
        yield info
    finally:
        release_pipeline_lock(lock_path, expected_pid=info.pid)
