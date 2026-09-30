"""Independent n8n health watchdog (host-side, no n8n notifications)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from zoneinfo import ZoneInfo

    try:
        MSK = ZoneInfo("Europe/Moscow")
    except Exception:  # noqa: BLE001 — Windows hosts may lack tzdata
        MSK = timezone(timedelta(hours=3), name="MSK")
except ImportError:  # pragma: no cover
    MSK = timezone(timedelta(hours=3), name="MSK")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = ROOT / "data" / "n8n_watchdog_state.json"
DEFAULT_ENV_PATH = ROOT / ".env"
DEFAULT_CONTAINER = "n8n"
DEFAULT_HEALTH_URL = "http://127.0.0.1:5678/healthz/readiness"
FAILURE_THRESHOLD = 3
HTTP_TIMEOUT_SECONDS = 8.0
DOCKER_TIMEOUT_SECONDS = 10.0
TELEGRAM_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("n8n_watchdog")

Status = str  # OK | DEGRADED | DOWN


@dataclass(frozen=True)
class CheckResult:
    status: Status
    container: str
    http: str
    detail: str = ""


@dataclass
class WatchState:
    status: Status = "OK"
    consecutive_failures: int = 0
    down_since: str | None = None
    last_check: str | None = None
    alert_sent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "consecutive_failures": int(self.consecutive_failures),
            "down_since": self.down_since,
            "last_check": self.last_check,
            "alert_sent": bool(self.alert_sent),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> WatchState:
        if not data:
            return cls()
        try:
            failures = int(data.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        status = str(data.get("status") or "OK")
        if status not in {"OK", "DEGRADED", "DOWN"}:
            status = "OK"
        return cls(
            status=status,
            consecutive_failures=max(0, failures),
            down_since=_as_optional_str(data.get("down_since")),
            last_check=_as_optional_str(data.get("last_check")),
            alert_sent=bool(data.get("alert_sent")),
        )


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_msk(dt: datetime | None = None) -> str:
    when = dt or now_utc()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S %Z")


def iso_utc(dt: datetime | None = None) -> str:
    when = dt or now_utc()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env loader (no dependency on python-dotenv)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read env file: %s", type(exc).__name__)
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key:
            continue
        out[key] = value
        # Prefer already-exported process env; fill gaps from file.
        os.environ.setdefault(key, value)
    return out


def get_telegram_config(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    src = env if env is not None else os.environ
    token = (src.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    admin = (src.get("TELEGRAM_ADMIN_CHAT_ID") or os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()
    if admin:
        chat_id = admin.split(",")[0].strip()
    else:
        chat_id = (src.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID/TELEGRAM_CHAT_ID required")
    return token, chat_id


def sanitize_error(text: str, token: str | None = None) -> str:
    cleaned = text or ""
    if token:
        cleaned = cleaned.replace(token, "***")
    cleaned = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***:***", cleaned)
    cleaned = re.sub(r"api\.telegram\.org/bot[^\s\"']+", "api.telegram.org/bot***/", cleaned)
    return cleaned[:500]


def load_state(path: Path) -> WatchState:
    if not path.exists():
        return WatchState()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("Corrupted watchdog state (not object); resetting")
            return WatchState()
        return WatchState.from_dict(data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Corrupted/unreadable watchdog state (%s); resetting", type(exc).__name__)
        return WatchState()


def save_state(path: Path, state: WatchState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def inspect_container(
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Return docker inspect JSON object for container, or empty dict if missing."""
    runner = run or subprocess.run
    try:
        proc = runner(
            ["docker", "inspect", name],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"_error": type(exc).__name__}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "No such object" in err or "No such container" in err:
            return {"_missing": True}
        return {"_error": "inspect_failed"}
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return {"_error": "bad_json"}
    if not isinstance(data, list) or not data:
        return {"_missing": True}
    obj = data[0]
    return obj if isinstance(obj, dict) else {"_error": "bad_shape"}


def check_http_via_docker_exec(
    container: str,
    url: str = DEFAULT_HEALTH_URL,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[bool, str]:
    """Hit n8n readiness inside the container (no public round-trip)."""
    runner = run or subprocess.run
    # Prefer wget (present in n8n image); fall back to python urllib in container.
    script = (
        "wget -qO- --timeout=5 "
        + url
        + " 2>/dev/null || python -c \"import urllib.request; print(urllib.request.urlopen('"
        + url
        + "', timeout=5).read().decode())\""
    )
    try:
        proc = runner(
            ["docker", "exec", container, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT_SECONDS + 2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"exec_error:{type(exc).__name__}"
    body = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"http_fail:rc={proc.returncode}"
    if '"status"' in body and "ok" in body.lower():
        return True, "ok"
    if body.lower() == "ok":
        return True, "ok"
    return False, f"unexpected_body:{body[:80]}"


def evaluate_check(
    inspect_data: Mapping[str, Any],
    http_ok: bool,
    http_detail: str,
) -> CheckResult:
    if inspect_data.get("_missing"):
        return CheckResult("DOWN", "missing", http_detail or "skipped", "container missing")
    if inspect_data.get("_error"):
        return CheckResult(
            "DOWN",
            f"inspect_error:{inspect_data['_error']}",
            http_detail or "skipped",
            "docker inspect failed",
        )

    state = inspect_data.get("State") or {}
    if not isinstance(state, dict):
        state = {}
    status = str(state.get("Status") or "unknown")
    restarting = bool(state.get("Restarting"))
    health_obj = state.get("Health")
    health = None
    if isinstance(health_obj, dict):
        health = str(health_obj.get("Status") or "") or None

    container_desc = status
    if restarting:
        container_desc = f"{status}/restarting"
    if health:
        container_desc = f"{container_desc} health={health}"

    if restarting or status in {"exited", "dead", "created", "paused", "removing"}:
        return CheckResult("DOWN", container_desc, http_detail or "skipped", "container not running")
    if status != "running":
        return CheckResult("DOWN", container_desc, http_detail or "skipped", f"status={status}")

    if health == "unhealthy":
        return CheckResult("DEGRADED", container_desc, http_detail, "docker health unhealthy")

    if not http_ok:
        return CheckResult("DEGRADED", container_desc, http_detail or "fail", "http health failed")

    return CheckResult("OK", container_desc, http_detail or "ok", "healthy")


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = TELEGRAM_TIMEOUT_SECONDS,
    urlopen: Callable[..., Any] | None = None,
) -> tuple[bool, str | None]:
    """One-shot Telegram sendMessage via stdlib. Never logs token/URL."""
    opener = urlopen or urllib.request.urlopen
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        api,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, sanitize_error(f"HTTP {exc.code}: {body}", token)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        return False, sanitize_error(f"{type(exc).__name__}", token)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, sanitize_error(f"invalid JSON (HTTP {status})", token)
    if not isinstance(data, dict) or not data.get("ok"):
        desc = ""
        if isinstance(data, dict):
            desc = str(data.get("description") or "")
        return False, sanitize_error(desc or f"telegram rejected (HTTP {status})", token)
    return True, None


def build_down_message(
    *,
    hostname: str,
    check: CheckResult,
    failures: int,
    when: datetime | None = None,
) -> str:
    return (
        "⚠️ n8n DOWN\n"
        f"Host: {hostname}\n"
        f"Time: {format_msk(when)}\n"
        f"Container: {check.container}\n"
        f"HTTP: {check.http}\n"
        f"Failed checks: {failures}"
    )


def build_recovered_message(
    *,
    hostname: str,
    down_since: str | None,
    when: datetime | None = None,
) -> str:
    recovered_at = when or now_utc()
    started = parse_iso(down_since)
    if started is not None:
        delta = recovered_at - started
        seconds = max(0, int(delta.total_seconds()))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        downtime = f"{hours:d}h {minutes:02d}m {secs:02d}s"
        since_txt = format_msk(started)
    else:
        downtime = "unknown"
        since_txt = down_since or "unknown"
    return (
        "✅ n8n RECOVERED\n"
        f"Host: {hostname}\n"
        f"Recovered: {format_msk(recovered_at)}\n"
        f"Down since: {since_txt}\n"
        f"Downtime: {downtime}"
    )


def build_test_message(hostname: str, when: datetime | None = None) -> str:
    return (
        "🧪 n8n watchdog test\n"
        f"Host: {hostname}\n"
        f"Time: {format_msk(when)}\n"
        "Status: monitoring active"
    )


def run_watch_cycle(
    *,
    state: WatchState,
    check: CheckResult,
    hostname: str,
    send: Callable[[str], tuple[bool, str | None]],
    now: datetime | None = None,
    failure_threshold: int = FAILURE_THRESHOLD,
) -> tuple[WatchState, str | None]:
    """
    Apply one check to state; optionally send Telegram.

    Returns (new_state, action) where action is down|recovered|None.
    """
    when = now or now_utc()
    new = WatchState(
        status=check.status,
        consecutive_failures=state.consecutive_failures,
        down_since=state.down_since,
        last_check=iso_utc(when),
        alert_sent=state.alert_sent,
    )
    action: str | None = None

    if check.status == "OK":
        if new.alert_sent:
            ok, err = send(build_recovered_message(hostname=hostname, down_since=new.down_since, when=when))
            if not ok:
                logger.error("Telegram RECOVERED send failed: %s", err or "unknown")
                # Keep alert_sent True so we retry recovery next OK cycle.
                new.consecutive_failures = 0
                new.status = "OK"
                return new, None
            action = "recovered"
        new.consecutive_failures = 0
        new.down_since = None
        new.alert_sent = False
        new.status = "OK"
        return new, action

    # Failure path (DEGRADED or DOWN)
    new.consecutive_failures = int(new.consecutive_failures) + 1
    if new.down_since is None:
        new.down_since = iso_utc(when)
    new.status = check.status

    if new.consecutive_failures >= failure_threshold and not new.alert_sent:
        ok, err = send(
            build_down_message(
                hostname=hostname,
                check=check,
                failures=new.consecutive_failures,
                when=when,
            )
        )
        if ok:
            new.alert_sent = True
            action = "down"
        else:
            logger.error("Telegram DOWN send failed: %s", err or "unknown")
            # alert_sent stays False
    return new, action


def perform_live_check(
    container: str = DEFAULT_CONTAINER,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> CheckResult:
    inspect_data = inspect_container(container, run=run)
    if inspect_data.get("_missing") or inspect_data.get("_error"):
        return evaluate_check(inspect_data, False, "skipped")

    state = inspect_data.get("State") or {}
    status = str((state or {}).get("Status") or "")
    restarting = bool((state or {}).get("Restarting"))
    if status != "running" or restarting:
        return evaluate_check(inspect_data, False, "skipped")

    http_ok, http_detail = check_http_via_docker_exec(container, run=run)
    return evaluate_check(inspect_data, http_ok, http_detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent n8n health watchdog")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--test-alert", action="store_true", help="Send one test Telegram message; do not change state")
    parser.add_argument("--check-only", action="store_true", help="Print check result and exit (no Telegram/state)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    load_env_file(args.env_file)
    hostname = socket.gethostname()

    if args.test_alert:
        try:
            token, chat_id = get_telegram_config()
        except ValueError as exc:
            logger.error("Telegram config missing: %s", type(exc).__name__)
            return 2
        ok, err = send_telegram_message(token, chat_id, build_test_message(hostname))
        if not ok:
            logger.error("Telegram test alert failed: %s", err or "unknown")
            return 1
        logger.info("Telegram test alert sent")
        return 0

    check = perform_live_check(args.container)
    logger.info(
        "check status=%s container=%s http=%s detail=%s",
        check.status,
        check.container,
        check.http,
        check.detail,
    )

    if args.check_only:
        print(json.dumps(check.__dict__, ensure_ascii=False))
        return 0 if check.status == "OK" else 1

    state = load_state(args.state_file)

    try:
        token, chat_id = get_telegram_config()
    except ValueError as exc:
        logger.error("Telegram config missing: %s", type(exc).__name__)
        # Still persist check counters so failures accumulate if config is fixed later.
        when = now_utc()
        if check.status == "OK":
            state = WatchState(status="OK", consecutive_failures=0, last_check=iso_utc(when), alert_sent=False)
        else:
            state.consecutive_failures += 1
            state.status = check.status
            state.last_check = iso_utc(when)
            if state.down_since is None:
                state.down_since = iso_utc(when)
        save_state(args.state_file, state)
        return 2

    def _send(text: str) -> tuple[bool, str | None]:
        return send_telegram_message(token, chat_id, text)

    new_state, action = run_watch_cycle(
        state=state,
        check=check,
        hostname=hostname,
        send=_send,
    )
    save_state(args.state_file, new_state)
    if action:
        logger.info("telegram_action=%s", action)
    logger.info(
        "state status=%s failures=%s alert_sent=%s",
        new_state.status,
        new_state.consecutive_failures,
        new_state.alert_sent,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
