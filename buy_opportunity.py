from __future__ import annotations

"""Explainable Russia BUY / STRONG_BUY opportunity signals + cooldown state."""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import config
from deal_ranking import RankedDeal, canonical_gpu, target_price_for_gpu

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("data") / "buy_opportunity_state.json"

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


def cooldown_allows(
    *,
    state: dict[str, Any],
    signal: BuySignal,
    top1_key: str | None,
    manual: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    Return (should_trigger_thailand_scan, reason).

    Manual always bypasses cooldown.
    """
    if manual:
        return True, "manual"
    now = now or _now()
    models = state.setdefault("models", {})
    prev = models.get(signal.model_key) or {}
    last_scan = _parse_ts(prev.get("last_thailand_scan_at") or prev.get("last_signal_at"))
    if last_scan is None:
        return True, "first_signal"

    age_h = (now - last_scan).total_seconds() / 3600.0
    if age_h >= float(config.BUY_COOLDOWN_HOURS):
        return True, "cooldown_expired"

    last_price = prev.get("last_ru_price")
    last_level = prev.get("last_signal_level")
    last_top1 = state.get("last_top1_key")

    if last_price is not None:
        try:
            last_price_i = int(last_price)
            if last_price_i > 0:
                drop = last_price_i - int(signal.price)
                improve = drop / last_price_i
                if improve >= float(config.BUY_BYPASS_PRICE_IMPROVE_PCT):
                    return True, "price_improved_pct"
                if drop >= int(config.BUY_BYPASS_PRICE_DROP_RUB):
                    return True, "price_drop_abs"
        except (TypeError, ValueError):
            pass

    if last_level == "BUY" and signal.level == "STRONG_BUY":
        return True, "upgraded_to_strong"

    if top1_key and last_top1 and top1_key != last_top1:
        return True, "new_top1_model"

    return False, "deduped"


def record_signal(
    state: dict[str, Any],
    signal: BuySignal,
    *,
    thailand_scanned: bool,
    top1_key: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    models = state.setdefault("models", {})
    entry = dict(models.get(signal.model_key) or {})
    entry.update(
        {
            "model_key": signal.model_key,
            "cluster_name": signal.cluster_name,
            "last_signal_at": now.isoformat(),
            "last_ru_price": signal.price,
            "last_signal_level": signal.level,
            "last_score": signal.score,
            "fingerprint": signal.fingerprint,
        }
    )
    if thailand_scanned:
        entry["last_thailand_scan_at"] = now.isoformat()
    models[signal.model_key] = entry
    if top1_key:
        state["last_top1_key"] = top1_key
    return state


def select_buy_signals(
    deals: Sequence[RankedDeal],
    *,
    state: dict[str, Any] | None = None,
    manual: bool = False,
    limit: int = 3,
) -> tuple[list[BuySignal], list[BuySignal], str | None]:
    """
    Evaluate deals → signals; split into (to_announce_with_thai_scan, all_valid).

    Returns (actionable, all_signals, top1_key).
    """
    state = state if state is not None else {}
    all_signals: list[BuySignal] = []
    for deal in deals:
        sig = evaluate_buy_rules(deal)
        if sig:
            all_signals.append(sig)
    top1_key = model_key_for_deal(deals[0]) if deals else None
    actionable: list[BuySignal] = []
    for sig in all_signals:
        ok, _reason = cooldown_allows(
            state=state, signal=sig, top1_key=top1_key, manual=manual
        )
        if ok:
            actionable.append(sig)
        if len(actionable) >= limit:
            break
    return actionable, all_signals, top1_key
