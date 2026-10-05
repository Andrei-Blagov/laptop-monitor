from __future__ import annotations

"""Explainable Russia BUY / STRONG_BUY opportunity signals + cooldown state."""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import config
from deal_ranking import RankedDeal, canonical_gpu, target_price_for_gpu

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("data") / "buy_opportunity_state.json"
STATE_LOCK_SUFFIX = ".lock"

SignalLevel = str  # BUY | STRONG_BUY


@dataclass
class BuySignal:
    level: SignalLevel
    model_key: str
    cluster_name: str
    price: int
    store: str
    gpu: str | None
    cpu: str | None
    ram_gb: int | None
    ssd_gb: int | None
    screen_inch: float | None
    score: float
    confidence: int
    historical_min: int | None
    target_price: int | None
    url: str | None
    reasons: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    over_hist_pct: float | None = None
    fingerprint: str = ""
    cluster_key: str | None = None
    history_started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def model_key_for_deal(deal: RankedDeal) -> str:
    offer = deal.offer or {}
    ext = offer.get("external_id") or ""
    store = deal.store or offer.get("store") or ""
    name = deal.cluster_name or offer.get("name") or ""
    return f"{store}:{ext}:{name}".strip(":")


def fingerprint_for_deal(deal: RankedDeal, level: str) -> str:
    return "|".join(
        [
            model_key_for_deal(deal),
            str(deal.price),
            level,
            f"{deal.score:g}",
            str(deal.confidence),
        ]
    )


def evaluate_buy_rules(deal: RankedDeal) -> BuySignal | None:
    """
    Config-driven explainable BUY rules.

    No automatic signal without historical_min.
    """
    price = deal.price
    hist = deal.historical_min
    conf = int(deal.confidence or 0)
    score = float(deal.score or 0.0)
    if price is None or price <= 0:
        return None
    if hist is None or hist <= 0:
        return None
    if conf < int(config.BUY_MIN_CONFIDENCE):
        return None

    over = (price - hist) / hist
    gpu = canonical_gpu(deal.gpu)
    target = target_price_for_gpu(deal.gpu)
    rule_ids: list[str] = []
    reasons: list[str] = []

    # RULE A — TARGET + HISTORY
    if (
        target is not None
        and price <= target
        and over <= float(config.BUY_RULE_A_MAX_OVER_HIST_PCT)
        and conf >= int(config.BUY_MIN_CONFIDENCE)
    ):
        rule_ids.append("A")
        reasons.append(f"цена <= target {target}")
        reasons.append(f"+{over * 100:.1f}% от исторического минимума")

    # RULE B — VERY STRONG VALUE
    if (
        score >= float(config.BUY_RULE_B_MIN_SCORE)
        and over <= float(config.BUY_RULE_B_MAX_OVER_HIST_PCT)
        and conf >= int(config.BUY_MIN_CONFIDENCE)
    ):
        rule_ids.append("B")
        reasons.append(f"сильный score {score:g}")
        reasons.append(f"<= {float(config.BUY_RULE_B_MAX_OVER_HIST_PCT) * 100:g}% от минимума")

    # RULE C — HISTORICAL LOW
    if (
        over <= float(config.BUY_RULE_C_MAX_OVER_HIST_PCT)
        and score >= float(config.BUY_RULE_C_MIN_SCORE)
        and conf >= int(config.BUY_MIN_CONFIDENCE)
    ):
        rule_ids.append("C")
        reasons.append("исторический минимум / почти минимум")
        reasons.append(f"score {score:g}")

    if not rule_ids:
        return None

    # STRONG_BUY: at/under target, within strong hist band, high score+confidence
    is_strong = (
        target is not None
        and price <= target
        and over <= float(config.BUY_STRONG_MAX_OVER_HIST_PCT)
        and score >= float(config.BUY_STRONG_MIN_SCORE)
        and conf >= int(config.BUY_MIN_CONFIDENCE)
    )
    level: SignalLevel = "STRONG_BUY" if is_strong else "BUY"

    if gpu:
        reasons.insert(0, gpu)
    reasons.append(f"score {score:g}")
    reasons.append(f"confidence {conf}")
    # dedupe reasons preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)

    key = model_key_for_deal(deal)
    sig = BuySignal(
        level=level,
        model_key=key,
        cluster_name=deal.cluster_name,
        price=int(price),
        store=deal.store,
        gpu=deal.gpu,
        cpu=deal.cpu,
        ram_gb=deal.ram_gb,
        ssd_gb=deal.ssd_gb,
        screen_inch=deal.screen_inch,
        score=score,
        confidence=conf,
        historical_min=int(hist),
        target_price=target,
        url=deal.url,
        reasons=uniq,
        rule_ids=rule_ids,
        over_hist_pct=over,
        fingerprint=fingerprint_for_deal(deal, level),
        cluster_key=deal.cluster_key,
        history_started_at=deal.history_started_at,
    )
    return sig


def load_state(path: Path | str = DEFAULT_STATE_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"version": 1, "models": {}, "last_top1_key": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state not object")
        data.setdefault("version", 1)
        data.setdefault("models", {})
        data.setdefault("last_top1_key", None)
        if not isinstance(data["models"], dict):
            data["models"] = {}
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("buy_opportunity state corrupt, reset: %s", type(exc).__name__)
        return {"version": 1, "models": {}, "last_top1_key": None}


def atomic_write_json(path: Path | str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_state(state: dict[str, Any], path: Path | str = DEFAULT_STATE_PATH) -> None:
    atomic_write_json(path, state)


@contextmanager
def state_process_lock(
    path: Path | str = DEFAULT_STATE_PATH, *, timeout: float = 5.0
) -> Iterator[None]:
    """
    Cross-process advisory lock for buy_opportunity_state.json RMW.

    Hold only around read-modify-write — never during Thailand HTTP.
    """
    p = Path(path)
    lock_path = Path(str(p) + STATE_LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + float(timeout)
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                # Stale lock recovery: if older than timeout*3, steal.
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > float(timeout) * 3:
                        lock_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                        continue
                except OSError:
                    pass
                raise TimeoutError("buy_opportunity state lock timeout")
            time.sleep(0.02)
    try:
        # Optional POSIX advisory lock for extra safety when available.
        try:
            import fcntl  # type: ignore

            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        try:
            if fd is not None:
                try:
                    import fcntl  # type: ignore

                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                os.close(fd)
        finally:
            try:
                lock_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except TypeError:
                # Python <3.8 compatibility path (not expected on 3.12)
                try:
                    if lock_path.exists():
                        lock_path.unlink()
                except OSError:
                    pass
            except OSError:
                pass


def with_state(
    mutator,
    path: Path | str = DEFAULT_STATE_PATH,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Load → mutate → save under process lock. mutator(state) -> state."""
    with state_process_lock(path, timeout=timeout):
        state = load_state(path)
        state = mutator(state) or state
        save_state(state, path)
        return state


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def history_days(signal: BuySignal, now: datetime | None = None) -> float | None:
    started = _parse_ts(signal.history_started_at)
    if started is None:
        return None
    return ((now or _now()) - started).total_seconds() / 86400.0


def history_is_mature(signal: BuySignal, now: datetime | None = None) -> bool:
    days = history_days(signal, now)
    return days is not None and days >= float(config.BUY_MIN_HISTORY_DAYS)


def _cluster_name_of_key(key: str | None) -> str | None:
    # model_key format: "<store>:<external_id>:<cluster name>"
    parts = str(key or "").split(":", 2)
    return parts[2] if len(parts) == 3 else None


def _notified_at(entry: dict[str, Any]) -> datetime | None:
    return _parse_ts(
        entry.get("last_signal_at")
        or entry.get("last_thailand_job_enqueued_at")
        or entry.get("last_thailand_scan_at")
    )


def _was_notified(entry: Any) -> bool:
    return isinstance(entry, dict) and (
        _notified_at(entry) is not None or entry.get("last_ru_price") is not None
    )


def find_previous_notification(
    state: dict[str, Any], signal: BuySignal
) -> dict[str, Any] | None:
    """
    Latest notified entry for this model cluster.

    model_key embeds the cheapest store, so the same laptop gets a new key when
    another store becomes cheapest; fall back to cluster_key / cluster_name.
    """
    models = state.get("models") or {}
    exact = models.get(signal.model_key)
    if _was_notified(exact):
        return exact
    candidates = [
        entry
        for entry in models.values()
        if _was_notified(entry)
        and (
            (signal.cluster_key and entry.get("cluster_key") == signal.cluster_key)
            or (signal.cluster_name and entry.get("cluster_name") == signal.cluster_name)
        )
    ]
    if not candidates:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return max(candidates, key=lambda e: _notified_at(e) or epoch)


def _is_new_historical_low(prev_low: Any, signal: BuySignal) -> bool:
    try:
        prev = int(prev_low)
    except (TypeError, ValueError):
        return False
    low = signal.historical_min
    if prev <= 0 or low is None or signal.price > int(low):
        return False
    drop = prev - int(low)
    return drop > 0 and (
        drop >= int(config.HISTORICAL_LOW_MIN_ABSOLUTE)
        or drop / prev * 100.0 >= float(config.HISTORICAL_LOW_MIN_PERCENT)
    )


def cooldown_allows(
    *,
    state: dict[str, Any],
    signal: BuySignal,
    top1_key: str | None,
    manual: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    Return (should_notify_and_trigger_thailand_scan, reason).

    Automatic repeat only on a meaningful new event vs the last notification
    for this model cluster. Elapsed time alone is never a reason. Manual
    always passes. History maturity is checked separately (select_buy_signals).
    """
    if manual:
        return True, "manual"
    now = now or _now()
    prev = find_previous_notification(state, signal)
    if not prev:
        return True, "first_signal"

    last_price = prev.get("last_notified_price", prev.get("last_ru_price"))
    try:
        last_price_i = int(last_price) if last_price is not None else 0
    except (TypeError, ValueError):
        last_price_i = 0
    if last_price_i > 0:
        drop = last_price_i - int(signal.price)
        if drop / last_price_i >= float(config.BUY_BYPASS_PRICE_IMPROVE_PCT):
            return True, "price_improved_pct"
        if drop >= int(config.BUY_BYPASS_PRICE_DROP_RUB):
            return True, "price_drop_abs"

    last_level = prev.get("last_notified_level", prev.get("last_signal_level"))
    if last_level == "BUY" and signal.level == "STRONG_BUY":
        return True, "upgraded_to_strong"

    if _is_new_historical_low(prev.get("last_notified_historical_min"), signal):
        return True, "new_historical_low"

    if top1_key and signal.model_key == top1_key:
        last_top1_name = state.get("last_top1_cluster") or _cluster_name_of_key(
            state.get("last_top1_key")
        )
        last_top1_at = _parse_ts(prev.get("last_top1_notified_at"))
        recently_top1 = last_top1_at is not None and (
            (now - last_top1_at).total_seconds() / 3600.0 < float(config.BUY_COOLDOWN_HOURS)
        )
        if last_top1_name and last_top1_name != signal.cluster_name and not recently_top1:
            return True, "new_top1_model"

    job_status = str(prev.get("last_thailand_job_status") or "")
    if job_status in {"queued", "processing"}:
        return False, "job_pending"

    return False, "deduped"


def _record_notification(
    state: dict[str, Any],
    signal: BuySignal,
    *,
    top1_key: str | None,
    now: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    models = state.setdefault("models", {})
    entry = dict(models.get(signal.model_key) or {})
    entry.update(
        {
            "model_key": signal.model_key,
            "cluster_name": signal.cluster_name,
            "cluster_key": signal.cluster_key,
            "last_signal_at": now.isoformat(),
            "last_ru_price": signal.price,
            "last_signal_level": signal.level,
            "last_score": signal.score,
            "fingerprint": signal.fingerprint,
            "last_notified_price": signal.price,
            "last_notified_level": signal.level,
            "last_notified_historical_min": signal.historical_min,
        }
    )
    if top1_key and signal.model_key == top1_key:
        entry["last_top1_notified_at"] = now.isoformat()
    entry.update(extra or {})
    models[signal.model_key] = entry
    if top1_key:
        state["last_top1_key"] = top1_key
        if signal.model_key == top1_key:
            state["last_top1_cluster"] = signal.cluster_name
        else:
            state["last_top1_cluster"] = _cluster_name_of_key(top1_key)
    return state


def record_signal(
    state: dict[str, Any],
    signal: BuySignal,
    *,
    thailand_scanned: bool,
    top1_key: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a notified signal; thailand_scanned sets last_thailand_scan_at."""
    now = now or _now()
    extra = {"last_thailand_scan_at": now.isoformat()} if thailand_scanned else None
    return _record_notification(state, signal, top1_key=top1_key, now=now, extra=extra)


def record_signal_enqueued(
    state: dict[str, Any],
    signal: BuySignal,
    *,
    job_id: str,
    top1_key: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record BUY signal + job enqueue. Does NOT set last_thailand_scan_at."""
    now = now or _now()
    return _record_notification(
        state,
        signal,
        top1_key=top1_key,
        now=now,
        extra={
            "last_thailand_job_enqueued_at": now.isoformat(),
            "last_thailand_job_id": job_id,
            "last_thailand_job_status": "queued",
        },
    )


def record_thailand_job_completed(
    state: dict[str, Any],
    *,
    model_key: str | None,
    job_id: str,
    job_status: str,
    scan_status: str,
    snapshot_path: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Worker completion: update scan timestamps only after real scan."""
    now = now or _now()
    models = state.setdefault("models", {})
    key = model_key
    if not key:
        # Fall back to entry matching job_id
        for k, v in models.items():
            if str((v or {}).get("last_thailand_job_id") or "") == job_id:
                key = k
                break
    if not key:
        return state
    entry = dict(models.get(key) or {})
    entry["last_thailand_job_id"] = job_id
    entry["last_thailand_job_status"] = job_status
    entry["last_thailand_scan_at"] = now.isoformat()
    entry["last_thailand_scan_status"] = scan_status
    if snapshot_path:
        entry["last_thailand_snapshot"] = snapshot_path
    models[key] = entry
    return state


def explain_buy_decisions(
    deals: Sequence[RankedDeal],
    *,
    state: dict[str, Any] | None = None,
    manual: bool = False,
    now: datetime | None = None,
) -> tuple[list[tuple[BuySignal, bool, str]], str | None]:
    """Per valid signal: (signal, would_notify, reason). Pure, no state mutation."""
    state = state if state is not None else {}
    now = now or _now()
    top1_key = model_key_for_deal(deals[0]) if deals else None
    out: list[tuple[BuySignal, bool, str]] = []
    for deal in deals:
        sig = evaluate_buy_rules(deal)
        if sig is None:
            continue
        if not manual and not history_is_mature(sig, now):
            out.append((sig, False, "history_immature"))
            continue
        ok, reason = cooldown_allows(
            state=state, signal=sig, top1_key=top1_key, manual=manual, now=now
        )
        out.append((sig, ok, reason))
    return out, top1_key


def select_buy_signals(
    deals: Sequence[RankedDeal],
    *,
    state: dict[str, Any] | None = None,
    manual: bool = False,
    limit: int = 3,
    now: datetime | None = None,
) -> tuple[list[BuySignal], list[BuySignal], str | None]:
    """
    Evaluate deals → signals; split into (to_announce_with_thai_scan, all_valid).

    Returns (actionable, all_signals, top1_key).
    """
    decisions, top1_key = explain_buy_decisions(deals, state=state, manual=manual, now=now)
    all_signals = [sig for sig, _ok, _reason in decisions]
    actionable = [sig for sig, ok, _reason in decisions if ok][: int(limit)]
    return actionable, all_signals, top1_key


