from __future__ import annotations

"""JIB Thailand adapter — HTTP search_suggestion API (public prices)."""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

import httpx

import config
from thailand.models import StoreScanResult, ThailandOffer
from thailand.specs_parse import (
    exclude_non_target_gpu,
    is_target_laptop_gpu,
    normalize_availability,
    parse_thb_price,
    specs_from_text,
)

logger = logging.getLogger(__name__)

STORE = "jib"
BASE = "https://www.jib.co.th"
SUGGEST_URL = f"{BASE}/web/product/search_suggestion"

# Catalog-scoped queries: GPU comes from query context (titles often omit GPU).
TARGET_QUERIES: tuple[tuple[str, str], ...] = (
    ("notebook rtx 5070 ti", "RTX 5070 Ti"),
    ("notebook rtx 5080", "RTX 5080"),
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (compatible; LaptopMonitor/0.4; +https://localhost)"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/",
    }


def _product_url(external_id: str, title: str) -> str:
    # Public search landing — detail SPA routes are not stably addressable via HTTP.
    q = quote(title[:80])
    return f"{BASE}/web/product/product_search/0/{q}?pid={external_id}"


def parse_suggestion_rec(
    items: Iterable[dict[str, Any]],
    *,
    catalog_gpu: str,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    """Parse JIB search_suggestion `rec` rows into ThailandOffer list."""
    collected_at = collected_at or datetime.now(timezone.utc)
    out: list[ThailandOffer] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        eid = str(raw.get("id") or "").strip()
        if not title or not eid:
            continue
        if eid in seen:
            continue
        if exclude_non_target_gpu(title):
            continue
        specs = specs_from_text(title, catalog_gpu=catalog_gpu)
        gpu = specs.get("gpu")
        if not is_target_laptop_gpu(gpu if isinstance(gpu, str) else None, title, catalog_gpu):
            continue
        regular = parse_thb_price(raw.get("price"))
        sale = parse_thb_price(raw.get("salePrice"))
        # Public promo price preferred when present and lower.
        if sale and regular and sale < regular:
            price = sale
        else:
            price = sale or regular
        if price is None:
            continue
        available, status = normalize_availability(available_flag=True)
        mpn = specs.get("manufacturer_part_number")
        offer = ThailandOffer(
            store=STORE,
            external_id=eid,
            name=title,
            url=_product_url(eid, title),
            price_thb=price,
            regular_price_thb=regular if regular and regular != price else None,
            available=available,
            availability_status=status,
            collected_at=collected_at,
            sku=str(mpn) if mpn else None,
            manufacturer_part_number=str(mpn) if mpn else None,
            brand=str(specs["brand"]) if specs.get("brand") else None,
            cpu=str(specs["cpu"]) if specs.get("cpu") else None,
            gpu=str(gpu) if gpu else catalog_gpu,
            ram_gb=int(specs["ram_gb"]) if specs.get("ram_gb") is not None else None,
            ssd_gb=int(specs["ssd_gb"]) if specs.get("ssd_gb") is not None else None,
            screen_size_inch=(
                float(specs["screen_inch"])
                if specs.get("screen_inch") is not None
                else None
            ),
            screen_resolution=(
                str(specs["screen_resolution"])
                if specs.get("screen_resolution")
                else None
            ),
            metadata={
                "source": "search_suggestion",
                "catalog_gpu": catalog_gpu,
                "image": raw.get("link"),
            },
        )
        seen.add(eid)
        out.append(offer)
    return out


def collect(
    *,
    client: httpx.Client | None = None,
    timeout: float | None = None,
) -> StoreScanResult:
    """Collect target GPU notebooks from JIB. Fail-safe on errors."""
    timeout = float(
        timeout
        if timeout is not None
        else config.THAILAND_PER_STORE_TIMEOUT_SECONDS
    )
    started = time.perf_counter()
    owns = client is None
    client = client or httpx.Client(
        headers=_headers(), follow_redirects=True, timeout=timeout
    )
    offers: list[ThailandOffer] = []
    try:
        now = datetime.now(timezone.utc)
        for query, gpu in TARGET_QUERIES:
            try:
                resp = client.get(
                    SUGGEST_URL,
                    params={"term": query, "cate_id": "0"},
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("JIB query failed %s: %s", query, type(exc).__name__)
                continue
            if resp.status_code >= 400:
                logger.warning("JIB HTTP %s for %s", resp.status_code, query)
                continue
            try:
                payload = resp.json()
            except ValueError:
                logger.warning("JIB invalid JSON for %s", query)
                continue
            rec = payload.get("rec") if isinstance(payload, dict) else None
            if not isinstance(rec, list):
                continue
            offers.extend(
                parse_suggestion_rec(rec, catalog_gpu=gpu, collected_at=now)
            )
        # Dedupe across queries by external_id (prefer first / already filtered).
        by_id: dict[str, ThailandOffer] = {}
        for o in offers:
            by_id.setdefault(o.external_id, o)
        final = list(by_id.values())
        if not final:
            return StoreScanResult(
                store=STORE,
                ok=False,
                offers=[],
                error="no_target_offers",
                duration_seconds=time.perf_counter() - started,
            )
        return StoreScanResult(
            store=STORE,
            ok=True,
            offers=final,
            duration_seconds=time.perf_counter() - started,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("JIB collect failed: %s", type(exc).__name__)
        return StoreScanResult(
            store=STORE,
            ok=False,
            offers=[],
            error=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
        )
    finally:
        if owns:
            client.close()
