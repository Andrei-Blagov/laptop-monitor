"""Isolated C2.1 bridge from collected store results to the Thai catalog.

Telegram delivery still uses the v0.10.2 scan payload. This module publishes
the full catalog and derived rankings only when a caller asks for them.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from thai_catalog import (
    ThaiCatalogError,
    comparison_from_catalog,
    export_thai_catalog,
    load_thai_catalog,
    rankings_from_catalog,
)
from thailand.models import FxRate, StoreScanResult


def fx_observation_document(fx: FxRate | None) -> dict[str, Any] | None:
    """D-core FX document. Does not contact the central bank."""
    if fx is None or fx.error or not fx.published_date:
        return None
    if str(fx.source or "").upper() != "CBR":
        return None
    fetched = fx.fetched_at
    if fetched.tzinfo is None:
        return None
    return {
        "fx_id": f"cbr-{fx.published_date}-{fx.nominal}",
        "source": "CBR",
        "currency": fx.currency or "THB",
        "nominal": fx.nominal,
        "rub_per_thb": str(fx.rub_per_thb),
        "fetched_at": fetched.isoformat(),
        "published_date": fx.published_date,
        "error": None,
    }


def publish_collected_catalog(
    store_results: Sequence[StoreScanResult],
    *,
    snapshot_root: Path | str,
    now: datetime,
    discovery_checked_at: datetime,
    instance_id: str,
    russian_snapshot: Mapping[str, Any] | None = None,
    fx: Mapping[str, Any] | None = None,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    """Publish one catalog from one collection. A failed write keeps the previous latest."""
    try:
        manifest = export_thai_catalog(
            store_results,
            snapshot_root=snapshot_root,
            now=now,
            discovery_checked_at=discovery_checked_at,
            instance_id=instance_id,
            started_at=started_at,
        )
        document = load_thai_catalog(snapshot_root, expected_instance_id=instance_id)
    except (ThaiCatalogError, OSError, ValueError) as exc:
        return {
            "status": "export_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "offer_count": None,
            "rankings": None,
            "comparison": None,
        }
    try:
        rankings = rankings_from_catalog(document, fx, now=now)
    except (ValueError, TypeError) as exc:
        return {
            "status": "published",
            "manifest": manifest,
            "offer_count": len(document.get("offers") or []),
            "rankings": None,
            "comparison": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    comparison = None
    if russian_snapshot is not None and fx is not None:
        try:
            comparison = comparison_from_catalog(document, russian_snapshot, fx, now=now)
        except (ValueError, TypeError, KeyError) as exc:
            comparison = {"status": "comparison_failed", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "published",
        "manifest": manifest,
        "snapshot_id": document.get("snapshot_id"),
        "offer_count": len(document.get("offers") or []),
        "eligible_count": len(rankings.get("eligible_models") or []),
        "top_count": len(rankings.get("thai_top_value") or []),
        "rankings": rankings,
        "comparison": comparison,
    }
