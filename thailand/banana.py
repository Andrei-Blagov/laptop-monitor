from __future__ import annotations

"""BaNANA / BNN Thailand adapter — source bnn.in.th (not banana.co.th)."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

import config
from deal_ranking import canonical_gpu
from stores.common import looks_like_challenge_page
from thailand.models import StoreScanResult, ThailandOffer
from thailand.specs_parse import (
    exclude_non_target_gpu,
    gpu_from_explicit_text,
    is_target_laptop_gpu,
    normalize_availability,
    parse_thb_price,
    specs_from_text,
)
from thailand.verification import (
    AVAIL_SOURCE_STRUCTURED_API,
    AVAIL_SOURCE_UNKNOWN,
    GPU_SOURCE_EXPLICIT_TITLE,
    GPU_SOURCE_SEARCH_QUERY,
    GPU_SOURCE_STRUCTURED_API,
    GPU_SOURCE_UNKNOWN,
    PRICE_SOURCE_STRUCTURED_API,
    compute_verification,
)

logger = logging.getLogger(__name__)

STORE = "banana"
BASE = "https://www.bnn.in.th"
SEARCH_URLS = (
    f"{BASE}/th/search?q=rtx+5070+ti+notebook",
    f"{BASE}/th/search?q=rtx+5080+notebook",
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (compatible; LaptopMonitor/0.4; +https://localhost)"
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }


def parse_banana_product_dict(
    raw: dict[str, Any],
    *,
    catalog_gpu: str | None = None,
    collected_at: datetime | None = None,
) -> ThailandOffer | None:
    collected_at = collected_at or datetime.now(timezone.utc)
    name = str(raw.get("name") or raw.get("title") or "").strip()
    eid = str(
        raw.get("external_id") or raw.get("id") or raw.get("sku") or ""
    ).strip()
    if not name or not eid:
        return None
    if exclude_non_target_gpu(name):
        return None
    # Guard: GDDR VRAM must not become RAM (extract_specs_from_name already guards).
    specs = specs_from_text(
        name,
        catalog_gpu=str(catalog_gpu) if catalog_gpu else None,
        apply_catalog_gpu=False,
    )
    gpu_source = GPU_SOURCE_UNKNOWN
    gpu = None
    if raw.get("gpu"):
        gpu = str(raw["gpu"])
        gpu_source = GPU_SOURCE_STRUCTURED_API
    else:
        title_gpu = gpu_from_explicit_text(name)
        if title_gpu:
            gpu = title_gpu
            gpu_source = GPU_SOURCE_EXPLICIT_TITLE
    for key in ("cpu", "brand", "warranty", "screen_resolution"):
        if raw.get(key):
            specs[key] = raw[key]
    for src, dst, cast in (
        ("ram_gb", "ram_gb", int),
        ("ssd_gb", "ssd_gb", int),
        ("screen_size_inch", "screen_inch", float),
    ):
        if raw.get(src) is not None:
            try:
                specs[dst] = cast(raw[src])
            except (TypeError, ValueError):
                pass
    if not is_target_laptop_gpu(gpu):
        return None
    price = parse_thb_price(
        raw.get("price_thb") or raw.get("price") or raw.get("sale_price")
    )
    regular = parse_thb_price(
        raw.get("regular_price_thb") or raw.get("regular_price")
    )
    if price is None:
        return None
    avail_text = str(
        raw.get("availability_status") or raw.get("availability") or ""
    )
    available, status = normalize_availability(
        available_flag=raw.get("available")
        if (avail_text or raw.get("available") is not None)
        else None,
        status_text=avail_text or None,
    )
    avail_confirmed = bool(avail_text) or raw.get("available") is not None
    url = str(raw.get("url") or f"{BASE}/th/p/{eid}")
    mpn = raw.get("manufacturer_part_number") or specs.get("manufacturer_part_number")
    sku = raw.get("sku") or mpn
    candidate = canonical_gpu(str(catalog_gpu)) if catalog_gpu else None
    offer = ThailandOffer(
        store=STORE,
        external_id=eid,
        name=name,
        url=url,
        price_thb=price,
        regular_price_thb=regular if regular and regular != price else None,
        available=available,
        availability_status=status,
        availability_confirmed=avail_confirmed,
        availability_source=AVAIL_SOURCE_STRUCTURED_API
        if avail_confirmed
        else AVAIL_SOURCE_UNKNOWN,
        price_source=PRICE_SOURCE_STRUCTURED_API,
        collected_at=collected_at,
        sku=str(sku) if sku else None,
        manufacturer_part_number=str(mpn) if mpn else None,
        brand=str(specs["brand"]) if specs.get("brand") else None,
        cpu=str(specs["cpu"]) if specs.get("cpu") else None,
        gpu=str(gpu) if gpu else None,
        gpu_source=gpu_source,
        candidate_gpu=candidate,
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
        warranty=str(raw["warranty"]) if raw.get("warranty") else None,
        metadata={
            "source": raw.get("source") or "bnn_json",
            "site": "bnn.in.th",
            "candidate_gpu_source": GPU_SOURCE_SEARCH_QUERY if candidate else None,
        },
    )
    return compute_verification(offer)


def parse_banana_json_payload(
    payload: Any,
    *,
    catalog_gpu: str | None = None,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("products", "items", "data", "results"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        if items is None:
            items = [payload]
    else:
        return []
    out: list[ThailandOffer] = []
    for raw in items:
        if isinstance(raw, dict):
            offer = parse_banana_product_dict(
                raw, catalog_gpu=catalog_gpu, collected_at=collected_at
            )
            if offer:
                out.append(offer)
    return out


def parse_banana_html(
    html: str,
    *,
    catalog_gpu: str | None = None,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    if looks_like_challenge_page(html):
        return []
    collected_at = collected_at or datetime.now(timezone.utc)
    offers: list[ThailandOffer] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") == "Product":
                offers_node = node.get("offers") or {}
                avail = ""
                if isinstance(offers_node, dict):
                    avail = str(offers_node.get("availability") or "")
                    price = offers_node.get("price")
                else:
                    price = None
                offers.extend(
                    parse_banana_json_payload(
                        [
                            {
                                "name": node.get("name"),
                                "id": node.get("sku")
                                or node.get("mpn")
                                or node.get("@id"),
                                "sku": node.get("sku"),
                                "manufacturer_part_number": node.get("mpn"),
                                "price": price,
                                "url": node.get("url"),
                                "availability_status": avail,
                                "available": "OutOfStock" not in avail,
                            }
                        ],
                        catalog_gpu=catalog_gpu,
                        collected_at=collected_at,
                    )
                )
    return offers


def collect(
    *,
    client: httpx.Client | None = None,
    timeout: float | None = None,
) -> StoreScanResult:
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
    try:
        now = datetime.now(timezone.utc)
        offers: list[ThailandOffer] = []
        errors: list[str] = []
        for url, gpu in (
            (SEARCH_URLS[0], "RTX 5070 Ti"),
            (SEARCH_URLS[1], "RTX 5080"),
        ):
            try:
                resp = client.get(url, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
                continue
            if resp.status_code >= 400:
                errors.append(f"HTTP_{resp.status_code}")
                continue
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" in ct:
                try:
                    offers.extend(
                        parse_banana_json_payload(
                            resp.json(), catalog_gpu=gpu, collected_at=now
                        )
                    )
                except ValueError:
                    errors.append("invalid_json")
            else:
                if looks_like_challenge_page(resp.text):
                    errors.append("blocked")
                    continue
                offers.extend(
                    parse_banana_html(resp.text, catalog_gpu=gpu, collected_at=now)
                )
        by_id: dict[str, ThailandOffer] = {}
        for o in offers:
            by_id.setdefault(o.external_id, o)
        final = list(by_id.values())
        if not final:
            err = ",".join(errors) if errors else "no_target_offers"
            return StoreScanResult(
                store=STORE,
                ok=False,
                offers=[],
                error=err,
                duration_seconds=time.perf_counter() - started,
            )
        return StoreScanResult(
            store=STORE,
            ok=True,
            offers=final,
            error=",".join(errors) if errors else None,
            duration_seconds=time.perf_counter() - started,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("BaNANA collect failed: %s", type(exc).__name__)
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
