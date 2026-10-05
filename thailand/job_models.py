from __future__ import annotations

"""Thailand async job schema helpers (no secrets)."""

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from buy_opportunity import BuySignal
from deal_ranking import RankedDeal

JOB_SCHEMA_VERSION = 1

TRIGGER_BUY = "buy_signal"
TRIGGER_MANUAL_CHECK = "manual_check"
TRIGGER_MANUAL_COMPARE = "manual_compare"
TRIGGER_MANUAL_MODEL = "manual_model_compare"

SECRET_KEY_FRAGMENTS = (
    "token",
    "password",
    "secret",
    "api_key",
    "authorization",
    "bot_token",
    "webhook",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return uuid.uuid4().hex


def deal_to_russian_context(deal: RankedDeal) -> dict[str, Any]:
    offer = deal.offer or {}
    return {
        "cluster_name": deal.cluster_name,
        "model_key": f"{deal.store}:{offer.get('external_id') or ''}:{deal.cluster_name}",
        "store": deal.store,
        "price": deal.price,
        "url": deal.url,
        "gpu": deal.gpu,
        "cpu": deal.cpu,
        "ram_gb": deal.ram_gb,
        "ssd_gb": deal.ssd_gb,
        "screen_inch": deal.screen_inch,
        "screen_resolution": deal.screen_resolution,
        "score": deal.score,
        "confidence": deal.confidence,
        "historical_min": deal.historical_min,
        "target_price": None,  # filled by caller if known
        "external_id": offer.get("external_id"),
        "sku": offer.get("sku") or offer.get("article"),
        "mpn": offer.get("mpn") or offer.get("part_number"),
    }


def signal_to_dict(signal: BuySignal) -> dict[str, Any]:
    return signal.to_dict()


def russian_context_to_deal(ctx: Mapping[str, Any]) -> RankedDeal:
    return RankedDeal(
        score=float(ctx.get("score") or 0.0),
        reasons=[],
        offer={
            "store": ctx.get("store"),
            "external_id": ctx.get("external_id"),
            "name": ctx.get("cluster_name"),
            "sku": ctx.get("sku"),
            "mpn": ctx.get("mpn"),
            "price": ctx.get("price"),
        },
        cluster_name=str(ctx.get("cluster_name") or ""),
        gpu=ctx.get("gpu"),
        store=str(ctx.get("store") or ""),
        price=int(ctx["price"]) if ctx.get("price") is not None else None,
        url=ctx.get("url"),
        ram_gb=ctx.get("ram_gb"),
        ssd_gb=ctx.get("ssd_gb"),
        screen_inch=ctx.get("screen_inch"),
        cpu=ctx.get("cpu"),
        confidence=int(ctx.get("confidence") or 0),
        historical_min=ctx.get("historical_min"),
        screen_resolution=ctx.get("screen_resolution"),
    )


def signal_from_dict(data: Mapping[str, Any]) -> BuySignal:
    return BuySignal(
        level=str(data.get("level") or "BUY"),
        model_key=str(data.get("model_key") or ""),
        cluster_name=str(data.get("cluster_name") or ""),
        price=int(data.get("price") or 0),
        store=str(data.get("store") or ""),
        gpu=data.get("gpu"),
        cpu=data.get("cpu"),
        ram_gb=data.get("ram_gb"),
        ssd_gb=data.get("ssd_gb"),
        screen_inch=data.get("screen_inch"),
        score=float(data.get("score") or 0.0),
        confidence=int(data.get("confidence") or 0),
        historical_min=data.get("historical_min"),
        target_price=data.get("target_price"),
        url=data.get("url"),
        reasons=list(data.get("reasons") or []),
        rule_ids=list(data.get("rule_ids") or []),
        over_hist_pct=data.get("over_hist_pct"),
        fingerprint=str(data.get("fingerprint") or ""),
    )


def build_job(
    *,
    trigger_type: str,
    signals: Sequence[BuySignal] | Sequence[Mapping[str, Any]],
    russian_deals: Sequence[RankedDeal] | Sequence[Mapping[str, Any]],
    dedupe_key: str,
    source_pipeline_run_id: int | None = None,
    app_version: str | None = None,
    instance_id: str | None = None,
    requested_chat_id: str | int | None = None,
    job_id: str | None = None,
    target_model: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from deal_ranking import target_price_for_gpu

    sig_dicts: list[dict[str, Any]] = []
    for s in signals:
        if isinstance(s, BuySignal):
            sig_dicts.append(signal_to_dict(s))
        else:
            sig_dicts.append(dict(s))

    ctx_dicts: list[dict[str, Any]] = []
    for d in russian_deals:
        if isinstance(d, RankedDeal):
            row = deal_to_russian_context(d)
            if row.get("target_price") is None:
                row["target_price"] = target_price_for_gpu(d.gpu)
            ctx_dicts.append(row)
        else:
            ctx_dicts.append(dict(d))

    # Cap context size — relevant deals only (signals + top few).
    ctx_dicts = ctx_dicts[:12]

    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id or new_job_id(),
        "dedupe_key": dedupe_key,
        "created_at": utc_now_iso(),
        "trigger_type": trigger_type,
        "source_pipeline_run_id": source_pipeline_run_id,
        "app_version": app_version,
        "instance_id": instance_id,
        "signals": sig_dicts,
        "russian_context": ctx_dicts,
        "target_model": dict(target_model) if target_model else None,
        "requested_chat_id": (
            str(requested_chat_id) if requested_chat_id is not None else None
        ),
        "status": "pending",
        "attempt": 0,
        "claimed_at": None,
    }


def strip_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(frag in lk for frag in SECRET_KEY_FRAGMENTS):
                continue
            out[k] = strip_secrets(v)
        return out
    if isinstance(obj, list):
        return [strip_secrets(x) for x in obj]
    return obj


def validate_job(job: Mapping[str, Any]) -> tuple[bool, str]:
    if not isinstance(job, Mapping):
        return False, "not_object"
    if int(job.get("schema_version") or 0) != JOB_SCHEMA_VERSION:
        return False, "bad_schema_version"
    if not job.get("job_id"):
        return False, "missing_job_id"
    if not job.get("trigger_type"):
        return False, "missing_trigger_type"
    if not job.get("dedupe_key"):
        return False, "missing_dedupe_key"
    return True, "ok"
