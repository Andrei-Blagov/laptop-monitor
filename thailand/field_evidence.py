"""Per-field spec evidence recorded by the store adapter that read the value."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from thailand.models import ThailandOffer

EVIDENCE_FIELDS = frozenset({
    "cpu",
    "ram_gb",
    "ssd_gb",
    "screen_inch",
    "screen_resolution",
})


def observation_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def record_field_evidence(
    offer: ThailandOffer,
    fields: Mapping[str, Any],
    *,
    method: str,
    captured_at: datetime,
) -> None:
    """Attach evidence for values this observation actually produced.

    GPU is intentionally absent: ``gpu_source`` is not reused here.
    """
    meta = dict(offer.metadata or {})
    evidence = dict(meta.get("field_evidence") or {})
    stamp = observation_iso(captured_at)
    for key, value in fields.items():
        name = "screen_inch" if key == "screen_size_inch" else key
        if name not in EVIDENCE_FIELDS or value is None or value == "":
            continue
        evidence[name] = {"method": method, "captured_at": stamp}
    meta["field_evidence"] = evidence
    offer.metadata = meta
