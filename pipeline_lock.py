from __future__ import annotations

"""Process lock для pipeline (Windows-compatible)."""

import json
import os
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
    payload = {"pid": info.pid, "started_at": info.started_at}
    # Exclusive create — атомарно на Windows/POSIX.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(path), flags)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)


def try_remove_stale_lock(lock_path: Path | str) -> bool:
    """
    Удаляет lock только если PID достоверно не работает.
    Returns True если lock удалён (или отсутствовал).
    """
    path = Path(lock_path)
    info = read_lock_info(path)
    if info is None:
        if path.exists():
            # Повреждённый/пустой lock — не удаляем агрессивно, если не читается
            # и файл есть: считаем «занято», кроме нулевого размера после crash mid-write.
            try:
                if path.stat().st_size == 0:
                    path.unlink(missing_ok=True)
                    return True
            except OSError:
                pass
            return False
        return True
    alive = _pid_is_running(info.pid)
    if alive is False:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
    return False


def acquire_pipeline_lock(lock_path: Path | str = DEFAULT_LOCK_PATH) -> LockInfo:
    path = Path(lock_path)
    info = LockInfo(pid=os.getpid(), started_at=_iso_now())
    try:
        _write_lock(path, info)
        return info
    except FileExistsError:
        if try_remove_stale_lock(path):
            try:
                _write_lock(path, info)
                return info
            except FileExistsError as exc:
                raise PipelineLockError("Pipeline already running") from exc
        raise PipelineLockError("Pipeline already running")


def release_pipeline_lock(
    lock_path: Path | str = DEFAULT_LOCK_PATH,
    *,
    expected_pid: int | None = None,
) -> None:
    path = Path(lock_path)
    info = read_lock_info(path)
    if info is None:
        path.unlink(missing_ok=True)
        return
    if expected_pid is not None and info.pid != expected_pid:
        # Чужой lock — не трогаем.
        return
    path.unlink(missing_ok=True)


@contextmanager
def pipeline_lock(lock_path: Path | str = DEFAULT_LOCK_PATH) -> Iterator[LockInfo]:
    info = acquire_pipeline_lock(lock_path)
    try:
        yield info
    finally:
        release_pipeline_lock(lock_path, expected_pid=info.pid)
