from __future__ import annotations

"""InvadeIT (invadeit.co.th) public JSON API adapter."""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from deal_ranking import canonical_gpu
from thailand.errors import NO_RESULTS, PARSE_ERROR, classify_store_failure, html_block_code
from thailand.models import StoreScanResult, ThailandOffer
from thailand.specs_parse import extract_brand, extract_mpn_candidates, gpu_from_explicit_text, specs_from_text
from thailand.verification import (
    AVAIL_SOURCE_PRODUCT_DETAIL,
    AVAIL_SOURCE_STRUCTURED_API,
    GPU_SOURCE_STRUCTURED_API,
    PRICE_SOURCE_STRUCTURED_API,
    compute_verification,
    is_verified_for_ranking,
)

logger = logging.getLogger(__name__)

STORE = "invadeit"
BASE = "https://www.invadeit.co.th"
CATEGORY = "notebooks-laptops"
_PAGE_SIZE = 100
_MAX_PAGES = 8
_MAX_DETAIL = 15


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (compatible; LaptopMonitor/0.5; +https://localhost)",
        "Accept": "application/json,text/html;q=0.8",
    }


def product_url(item: dict[str, Any]) -> str:
    pid = item.get("id")
    try:
        padded = f"{int(pid):06d}"
    except (TypeError, ValueError):
        padded = ""
    category = str(item.get("categoryUrlName") or "").strip()
    brand = str(item.get("manufacturerUrlName") or "").strip()
    slug = str(item.get("urlName") or "").strip()
    if category and brand and slug and padded:
        return f"{BASE}/product/{quote(category)}/{quote(brand)}/{quote(slug)}-p{padded}/"
    if pid:
        return f"{BASE}/product/{pid}"
    return BASE


def _positive_price(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _public_prices(item: dict[str, Any]) -> tuple[int | None, int | None]:
    """Public sale price when the API exposes one; never a zero placeholder."""
    sale = _positive_price(item.get("salePrice"))
    price = _positive_price(item.get("price"))
    before = _positive_price(item.get("beforePrice"))
    if sale is not None and (item.get("onSale") or price is None or sale < (price or sale)):
        regular = price if price and price > sale else before
        return sale, regular
    regular = before if before and price and before > price else None
    return price, regular


def _availability(item: dict[str, Any]) -> tuple[bool, str, bool]:
    flag = item.get("inStock")
    if flag is True:
        return True, "in_stock", True
    if flag is False:
        return False, "out_of_stock", True
    amount = item.get("amountInStock")
    try:
        if amount is not None and int(amount) > 0:
            return True, "in_stock", True
        if amount is not None and int(amount) <= 0:
            return False, "out_of_stock", True
    except (TypeError, ValueError):
        pass
    return False, "unknown", False


def _gpu_from_item(item: dict[str, Any]) -> str | None:
    evidence = "\n".join(
        str(item.get(key) or "")
        for key in ("keySpecs", "name", "description")
    )
    return canonical_gpu(gpu_from_explicit_text(evidence))


def parse_invadeit_item(
    item: dict[str, Any],
    *,
    collected_at: datetime | None = None,
) -> ThailandOffer | None:
    if not isinstance(item, dict):
        return None
    category = str(item.get("categoryName") or item.get("categoryUrlName") or "")
    if category and "notebook" not in category.lower() and "laptop" not in category.lower():
        return None
    gpu = _gpu_from_item(item)
    if gpu not in {"RTX 5070 Ti", "RTX 5080"}:
        return None
    name = str(item.get("name") or "").strip()
    specs_blob = "\n".join(
        p for p in (name, str(item.get("keySpecs") or ""), str(item.get("description") or "")) if p
    )
    specs = specs_from_text(specs_blob)
    price, regular = _public_prices(item)
    available, status, confirmed = _availability(item)
    sku = str(item.get("productNumber") or "").strip() or None
    mpns = extract_mpn_candidates(name)
    paren = re.search(r"\(([A-Z0-9][A-Z0-9.\-]{4,})\)", name)
    mpn = mpns[0] if mpns else (paren.group(1) if paren else None)
    collected_at = collected_at or datetime.now(timezone.utc)
    offer = ThailandOffer(
        store=STORE,
        external_id=str(item.get("id") or sku or name),
        name=name or str(item.get("id") or ""),
        url=product_url(item),
        price_thb=price,
        available=available,
        collected_at=collected_at,
        sku=sku,
        manufacturer_part_number=mpn,
        brand=specs.get("brand") or extract_brand(str(item.get("manufacturerName") or name)),
        cpu=specs.get("cpu"),
        gpu=gpu,
        ram_gb=specs.get("ram_gb"),
        ssd_gb=specs.get("ssd_gb"),
        screen_size_inch=specs.get("screen_inch"),
        screen_resolution=specs.get("screen_resolution"),
        regular_price_thb=regular,
        availability_status=status,
        gpu_source=GPU_SOURCE_STRUCTURED_API,
        availability_source=AVAIL_SOURCE_STRUCTURED_API,
        availability_confirmed=confirmed,
        price_source=PRICE_SOURCE_STRUCTURED_API,
        channel="direct",
        product_id=str(item.get("id") or "") or None,
        metadata={"category": category, "key_specs": item.get("keySpecs")},
    )
    return compute_verification(offer)


def specifications_to_text(payload: object) -> str:
    rows: list[str] = []
    if isinstance(payload, dict):
        payload = payload.get("specifications") or payload.get("items") or [payload]
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                label = row.get("name") or row.get("label") or row.get("key") or ""
                value = row.get("value") or row.get("text") or row.get("displayValue") or ""
                rows.append(f"{label}: {value}")
            elif row:
                rows.append(str(row))
    return "\n".join(rows)


def enrich_offer_from_specifications(offer: ThailandOffer, payload: object) -> ThailandOffer:
    """Fill missing specs from a product-detail payload. Does not invent a GPU."""
    text = specifications_to_text(payload)
    if not text.strip():
        return offer
    specs = specs_from_text(text)
    if offer.cpu is None and specs.get("cpu"):
        offer.cpu = specs["cpu"]
    if offer.ram_gb is None and specs.get("ram_gb"):
        offer.ram_gb = specs["ram_gb"]
    if offer.ssd_gb is None and specs.get("ssd_gb"):
        offer.ssd_gb = specs["ssd_gb"]
    if offer.screen_size_inch is None and specs.get("screen_inch"):
        offer.screen_size_inch = specs["screen_inch"]
    if offer.screen_resolution is None and specs.get("screen_resolution"):
        offer.screen_resolution = specs["screen_resolution"]
    if offer.gpu is None:
        gpu = canonical_gpu(gpu_from_explicit_text(text))
        if gpu in {"RTX 5070 Ti", "RTX 5080"}:
            offer.gpu = gpu
            offer.gpu_source = GPU_SOURCE_STRUCTURED_API
    offer.availability_source = offer.availability_source or AVAIL_SOURCE_PRODUCT_DETAIL
    offer.metadata = dict(offer.metadata or {})
    offer.metadata["detail_enriched"] = True
    return compute_verification(offer)


def _failure(started: float, code: str, detail: str) -> StoreScanResult:
    return StoreScanResult(
        store=STORE,
        ok=False,
        error=code,
        error_code=code,
        technical_detail=detail,
        duration_seconds=time.perf_counter() - started,
        collection_mode="failed",
    )


def _get_json(http: httpx.Client, url: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    try:
        resp = http.get(url)
    except httpx.TimeoutException:
        return None, "TIMEOUT", "timeout"
    except httpx.HTTPError as exc:
        code, detail = classify_store_failure(type(exc).__name__)
        return None, code, detail
    if resp.status_code >= 400:
        code, detail = classify_store_failure(status_code=resp.status_code)
        return None, code, detail
    if "json" not in (resp.headers.get("content-type") or "").lower():
        blocked = html_block_code(resp.text)
        if blocked:
            return None, blocked, blocked
        return None, PARSE_ERROR, "not_json"
    try:
        payload = resp.json()
    except ValueError:
        return None, PARSE_ERROR, "invalid_json"
    if not isinstance(payload, dict):
        return None, PARSE_ERROR, "payload_type"
    return payload, None, None


def collect(
    *,
    timeout: float = 90.0,
    client: httpx.Client | None = None,
) -> StoreScanResult:
    started = time.perf_counter()
    own = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True, timeout=timeout)
    discovered: list[ThailandOffer] = []
    try:
        for page in range(1, _MAX_PAGES + 1):
            url = (
                f"{BASE}/api/products/category/{CATEGORY}"
                f"?page={page}&pageSize={_PAGE_SIZE}&includeChildren=true"
            )
            payload, code, detail = _get_json(http, url)
            if payload is None:
                return _failure(started, code or PARSE_ERROR, detail or "request_failed")
            items = payload.get("items")
            if not isinstance(items, list):
                return _failure(started, PARSE_ERROR, "items_missing")
            now = datetime.now(timezone.utc)
            for item in items:
                offer = parse_invadeit_item(item, collected_at=now)
                if offer is not None:
                    discovered.append(offer)
            if not payload.get("hasNextPage"):
                break
        details = 0
        for offer in discovered:
            if details >= _MAX_DETAIL:
                break
            if offer.ram_gb is not None and offer.cpu is not None:
                continue
            if not offer.product_id:
                continue
            payload, _code, _detail = _get_json(
                http, f"{BASE}/api/products/{offer.product_id}/specifications"
            )
            details += 1
            if payload is not None:
                enrich_offer_from_specifications(offer, payload)
        verified = [o for o in discovered if is_verified_for_ranking(o)]
        unverified = [o for o in discovered if not is_verified_for_ranking(o)]
        return StoreScanResult(
            store=STORE,
            ok=True,
            offers=verified,
            unverified_candidates=unverified,
            error_code=NO_RESULTS if not discovered else None,
            duration_seconds=time.perf_counter() - started,
            collection_mode="http",
            discovered_count=len(discovered),
            verified_count=len(verified),
        )
    finally:
        if own:
            http.close()
