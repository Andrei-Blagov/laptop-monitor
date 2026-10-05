from __future__ import annotations

"""SpeedCom (speedcom.co.th) Shopify products.json adapter."""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from deal_ranking import canonical_gpu
from thailand.errors import NO_RESULTS, PARSE_ERROR, classify_store_failure, html_block_code
from thailand.models import StoreScanResult, ThailandOffer
from thailand.specs_parse import (
    exclude_non_target_gpu,
    extract_mpn_candidates,
    gpu_from_explicit_text,
    is_accessory_listing,
    specs_from_text,
)
from thailand.verification import (
    AVAIL_SOURCE_STRUCTURED_API,
    GPU_SOURCE_EXPLICIT_TITLE,
    GPU_SOURCE_STRUCTURED_API,
    PRICE_SOURCE_STRUCTURED_API,
    compute_verification,
    is_verified_for_ranking,
)

logger = logging.getLogger(__name__)

STORE = "speedcom"
BASE = "https://speedcom.co.th"
# Notebook-heavy public collections. GPU still comes from the product, not the collection.
COLLECTIONS = (
    "nvidia-rtx-50-series",
    "notebook-pc-rtx-5070",
    "laptop-pc-nvidia-5000",
)
_DEFAULT_VARIANT = {"default title", "default"}
_NOTEBOOK = re.compile(r"notebook|laptop|โน้ต|โน๊ต|แล็ปท็อป", re.I)
_DESKTOP = re.compile(r"การ์ดจอ|การ์ดแสดงผล|\bVGA\b|graphics\s*card", re.I)
_GPU_LINE = re.compile(
    r"(?:กราฟิก|GPU|Graphics)\s*[:：]\s*.{0,120}?(RTX\s*50\d0(?:\s*Ti)?)",
    re.I,
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (compatible; LaptopMonitor/0.5; +https://localhost)",
        "Accept": "application/json,text/html;q=0.8",
    }


def _plain(html: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _thb(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return int(round(amount))


def _is_notebook(product: dict[str, Any]) -> bool:
    title = str(product.get("title") or "")
    ptype = str(product.get("product_type") or "")
    blob = f"{ptype} {title}"
    if _DESKTOP.search(blob):
        return False
    if is_accessory_listing(blob):
        return False
    if "notebook" in ptype.lower() or "laptop" in ptype.lower():
        return True
    return bool(_NOTEBOOK.search(title))


def _product_gpu(product: dict[str, Any]) -> tuple[str | None, str]:
    """Explicit graphics line wins over tags. A 5050/5060 line is not a 5070 Ti."""
    body = _plain(product.get("body_html"))
    line = _GPU_LINE.search(body)
    if line:
        gpu = canonical_gpu(line.group(1))
        if gpu in {"RTX 5070 Ti", "RTX 5080"}:
            return gpu, GPU_SOURCE_STRUCTURED_API
        return None, GPU_SOURCE_STRUCTURED_API
    tags = " ".join(str(t) for t in (product.get("tags") or []) if t)
    title = str(product.get("title") or "")
    for text, source in (
        (tags, GPU_SOURCE_STRUCTURED_API),
        (title, GPU_SOURCE_EXPLICIT_TITLE),
    ):
        gpu = canonical_gpu(gpu_from_explicit_text(text))
        if gpu in {"RTX 5070 Ti", "RTX 5080"}:
            return gpu, source
    return None, GPU_SOURCE_EXPLICIT_TITLE


def _variant_text(variant: dict[str, Any]) -> str:
    parts = [
        variant.get("title"),
        variant.get("option1"),
        variant.get("option2"),
        variant.get("option3"),
    ]
    return " ".join(str(p) for p in parts if p).strip()


def _variant_gpu(variant: dict[str, Any]) -> str | None:
    """Target GPU named by this variant, 'reject' is not used — None means inherit or drop."""
    text = _variant_text(variant)
    if not text or text.lower() in _DEFAULT_VARIANT:
        return None
    gpu = canonical_gpu(gpu_from_explicit_text(text))
    if gpu:
        return gpu
    if exclude_non_target_gpu(text):
        return ""
    return None


def _public_price(variant: dict[str, Any]) -> tuple[int | None, int | None]:
    price = _thb(variant.get("price"))
    regular = _thb(variant.get("compare_at_price"))
    if price is None:
        return None, regular
    if regular is not None and regular <= price:
        regular = None
    return price, regular


def parse_speedcom_product(
    product: dict[str, Any],
    *,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    """
    One offer per target-GPU variant.

    A cheaper variant that names a different GPU is never used as the target price.
    """
    if not isinstance(product, dict) or not _is_notebook(product):
        return []
    collected_at = collected_at or datetime.now(timezone.utc)
    product_gpu, gpu_source = _product_gpu(product)
    title = str(product.get("title") or "").strip()
    handle = str(product.get("handle") or "").strip()
    product_id = str(product.get("id") or "")
    body = _plain(product.get("body_html"))
    spec_blob = " ".join(
        p
        for p in (
            title,
            body,
            " ".join(str(t) for t in (product.get("tags") or [])),
        )
        if p
    )
    specs = specs_from_text(spec_blob)
    variants = [v for v in (product.get("variants") or []) if isinstance(v, dict)]
    if not variants:
        return []

    chosen: list[tuple[dict[str, Any], str, str]] = []
    for variant in variants:
        own = _variant_gpu(variant)
        if own == "":
            continue
        gpu = own or product_gpu
        source = GPU_SOURCE_EXPLICIT_TITLE if own else gpu_source
        if gpu not in {"RTX 5070 Ti", "RTX 5080"}:
            continue
        chosen.append((variant, gpu, source))
    if not chosen:
        return []

    # Same GPU across color variants: keep each variant's own price, prefer available.
    offers: list[ThailandOffer] = []
    for variant, gpu, source in chosen:
        price, regular = _public_price(variant)
        available_flag = variant.get("available")
        if available_flag is True:
            available, status = True, "in_stock"
        elif available_flag is False:
            available, status = False, "out_of_stock"
        else:
            available, status = False, "unknown"
        sku = str(variant.get("sku") or "").strip() or None
        variant_id = str(variant.get("id") or "")
        mpns = extract_mpn_candidates(" ".join(p for p in (title, sku or "") if p))
        url = f"{BASE}/products/{handle}" if handle else BASE
        if variant_id and handle:
            url = f"{url}?variant={variant_id}"
        offer = ThailandOffer(
            store=STORE,
            external_id=f"{product_id}:{variant_id}" if variant_id else product_id or handle,
            name=title or handle or product_id,
            url=url,
            price_thb=price,
            available=available,
            collected_at=collected_at,
            sku=sku,
            manufacturer_part_number=mpns[0] if mpns else sku,
            brand=specs.get("brand"),
            cpu=specs.get("cpu"),
            gpu=gpu,
            ram_gb=specs.get("ram_gb"),
            ssd_gb=specs.get("ssd_gb"),
            screen_size_inch=specs.get("screen_inch"),
            screen_resolution=specs.get("screen_resolution"),
            regular_price_thb=regular,
            availability_status=status,
            gpu_source=source,
            availability_source=AVAIL_SOURCE_STRUCTURED_API,
            availability_confirmed=isinstance(available_flag, bool),
            price_source=PRICE_SOURCE_STRUCTURED_API,
            channel="direct",
            product_id=product_id or None,
            variant_id=variant_id or None,
            metadata={"product_type": product.get("product_type"), "handle": handle},
        )
        compute_verification(offer)
        offers.append(offer)
    return offers


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


def collect(
    *,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> StoreScanResult:
    started = time.perf_counter()
    own = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True, timeout=timeout)
    discovered: list[ThailandOffer] = []
    seen: set[str] = set()
    try:
        for collection in COLLECTIONS:
            page = 1
            while page <= 6:
                url = f"{BASE}/collections/{collection}/products.json?limit=250&page={page}"
                try:
                    resp = http.get(url)
                    if resp.status_code == 429:
                        time.sleep(2.0)
                        resp = http.get(url)
                except httpx.TimeoutException:
                    return _failure(started, "TIMEOUT", "timeout")
                except httpx.HTTPError as exc:
                    code, detail = classify_store_failure(type(exc).__name__)
                    return _failure(started, code, detail)
                if resp.status_code >= 400:
                    code, detail = classify_store_failure(status_code=resp.status_code)
                    return _failure(started, code, detail)
                if "json" not in (resp.headers.get("content-type") or "").lower():
                    blocked = html_block_code(resp.text)
                    if blocked:
                        return _failure(started, blocked, blocked)
                    return _failure(started, PARSE_ERROR, "not_json")
                try:
                    payload = resp.json()
                except ValueError:
                    return _failure(started, PARSE_ERROR, "invalid_json")
                products = payload.get("products") if isinstance(payload, dict) else None
                if not isinstance(products, list):
                    return _failure(started, PARSE_ERROR, "products_missing")
                now = datetime.now(timezone.utc)
                for product in products:
                    for offer in parse_speedcom_product(product, collected_at=now):
                        if offer.external_id in seen:
                            continue
                        seen.add(offer.external_id)
                        discovered.append(offer)
                if len(products) < 250:
                    break
                page += 1
        verified = [o for o in discovered if is_verified_for_ranking(o)]
        unverified = [o for o in discovered if not is_verified_for_ranking(o)]
        result = StoreScanResult(
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
        return result
    finally:
        if own:
            http.close()
