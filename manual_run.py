"""Durable manual pipeline request used by the Telegram control process.

The worker is a separate process, so Telegram polling never runs collection.
The existing pipeline lease remains the final cross-container exclusion gate.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline_lock import DEFAULT_LOCK_PATH, PipelineLockError, acquire_pipeline_lock, release_pipeline_lock
from storage import DEFAULT_DB_PATH

DEFAULT_REQUEST_PATH = Path("data/manual_run.json")
ACTIVE = {"pending", "running"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _guard(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lease").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_request(path: Path = DEFAULT_REQUEST_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _alive(pid: Any, request_id: str) -> bool:
    try:
        process = Path(f"/proc/{int(pid)}")
        if process.joinpath("stat").read_text().split()[2] == "Z":
            return False
        command = process.joinpath("cmdline").read_bytes()
        if b"manual_run" not in command or request_id.encode() not in command:
            return False
        os.kill(int(pid), 0)
        return True
    except (ValueError, TypeError, ProcessLookupError, FileNotFoundError):
        return False
    except PermissionError:
        return True


def _pipeline_busy() -> bool:
    try:
        info = acquire_pipeline_lock(DEFAULT_LOCK_PATH)
    except PipelineLockError:
        return True
    release_pipeline_lock(DEFAULT_LOCK_PATH, expected_pid=info.pid)
    return False


def _spawn(path: Path, request_id: str) -> int:
    child = subprocess.Popen(
        [sys.executable, "-m", "manual_run", request_id, str(path)],
        cwd=Path(__file__).resolve().parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return child.pid


def request_run(chat_id: str | int, *, path: Path = DEFAULT_REQUEST_PATH,
                spawn: Callable[[Path, str], int] = _spawn) -> tuple[dict[str, Any], bool]:
    path = Path(path).absolute()
    with _guard(path):
        current = read_request(path)
        if current.get("status") in ACTIVE:
            return current, False
        if current.get("id") and current.get("message_id") and not current.get("notified"):
            return current, False
        if _pipeline_busy():
            return {"status": "busy"}, False
        state = {"id": uuid.uuid4().hex, "chat_id": str(chat_id), "status": "pending",
                 "created_at": _now(), "message_id": None, "notified": False}
        _save(path, state)
        try:
            state["pid"] = spawn(path, state["id"])
        except OSError as exc:
            state.update(status="failed", error=f"Не удалось запустить проверку: {type(exc).__name__}",
                         finished_at=_now())
        _save(path, state)
        return state, True


def set_message_id(request_id: str, message_id: int, *, path: Path = DEFAULT_REQUEST_PATH) -> None:
    path = Path(path).absolute()
    with _guard(path):
        state = read_request(path)
        if state.get("id") == request_id:
            state["message_id"] = int(message_id)
            _save(path, state)


def mark_notified(request_id: str, *, path: Path = DEFAULT_REQUEST_PATH) -> None:
    path = Path(path).absolute()
    with _guard(path):
        state = read_request(path)
        if state.get("id") == request_id:
            state["notified"] = True
            _save(path, state)


def reconcile(*, path: Path = DEFAULT_REQUEST_PATH) -> dict[str, Any]:
    """Expose a completed run or a visibly interrupted one after bot restart."""
    path = Path(path).absolute()
    with _guard(path):
        state = read_request(path)
        if state.get("status") not in ACTIVE:
            return state
        if _alive(state.get("pid"), str(state.get("id") or "")):
            return state
        if state.get("status") == "pending":
            try:
                state["pid"] = _spawn(path, state["id"])
                _save(path, state)
                return state
            except OSError:
                pass
        state.update(status="unconfirmed",
                     error="Результат ручной проверки не подтверждён после остановки процесса. Проверьте статус и запустите её снова.",
                     finished_at=_now())
        _save(path, state)
        return state


def run_request(request_id: str, *, path: Path = DEFAULT_REQUEST_PATH,
                runner: Callable[..., Any] | None = None) -> None:
    path = Path(path).absolute()
    with _guard(path):
        state = read_request(path)
        if state.get("id") != request_id or state.get("status") != "pending":
            return
        state.update(status="running", started_at=_now(), pid=os.getpid())
        _save(path, state)
    if runner is None:
        from run_pipeline import run_pipeline
        runner = run_pipeline
    try:
        result = runner(DEFAULT_DB_PATH, enrich_identities=True,
                        write_comparison_artifacts=True,
                        last_run_path=Path("data/last_run.json"), deliver=True)
        finished = {"status": result.status, "exit_code": result.exit_code,
                    "run_id": result.run_id, "error": result.error_message,
                    "duration_seconds": result.duration_seconds}
    except Exception as exc:
        finished = {"status": "failed", "error": f"{type(exc).__name__}: запуск завершился ошибкой"}
    with _guard(path):
        state = read_request(path)
        if state.get("id") == request_id:
            state.update(finished, finished_at=_now())
            _save(path, state)


if __name__ == "__main__":
    run_request(sys.argv[1], path=Path(sys.argv[2]))
