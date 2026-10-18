from __future__ import annotations

"""InvadeIT (invadeit.co.th) public JSON API adapter."""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

import config
from deal_ranking import canonical_gpu
from thailand.eligibility import price_within_cap
from thailand.errors import NO_RESULTS, PARSE_ERROR, RATE_LIMITED, classify_store_failure, html_block_code
from thailand.fx import fx_usable_for_verdict, thb_to_rub
from thailand.models import FxRate, StoreScanResult, ThailandOffer
from thailand.specs_parse import extract_brand, extract_mpn_candidates, gpu_from_explicit_text, specs_from_text
from thailand.verification import (
    AVAIL_SOURCE_PRODUCT_DETAIL,
    AVAIL_SOURCE_STRUCTURED_API,
    GPU_SOURCE_STRUCTURED_API,
    PRICE_SOURCE_STRUCTURED_API,
    compute_verification,
    is_purchasable_confirmed,
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
    from thailand.field_evidence import record_field_evidence

    record_field_evidence(
        offer,
        {
            "brand": specs.get("brand"),
            "cpu": specs.get("cpu"),
            "ram_gb": specs.get("ram_gb"),
            "ssd_gb": specs.get("ssd_gb"),
            "screen_inch": specs.get("screen_inch"),
            "screen_resolution": specs.get("screen_resolution"),
        },
        method="structured_api",
        captured_at=collected_at,
    )
    return compute_verification(offer)


def _spec_pairs(payload: object) -> list[tuple[str, str]]:
    """Leaf name/value rows, including items nested inside a specification group."""
    pairs: list[tuple[str, str]] = []
    if isinstance(payload, list):
        for row in payload:
            pairs.extend(_spec_pairs(row))
        return pairs
    if not isinstance(payload, dict):
        if payload:
            pairs.append(("", str(payload)))
        return pairs
    items = payload.get("items")
    if isinstance(items, list):
        pairs.extend(_spec_pairs(items))
    elif isinstance(payload.get("specifications"), list):
        pairs.extend(_spec_pairs(payload.get("specifications")))
    value = payload.get("value")
    if value in (None, ""):
        value = payload.get("text") or payload.get("displayValue") or ""
    if value:
        label = str(payload.get("name") or payload.get("label") or payload.get("key") or "")
        pairs.append((label, str(value)))
    return pairs


def specifications_to_text(payload: object) -> str:
    return "\n".join(f"{label}: {value}" if label else value for label, value in _spec_pairs(payload))


def display_field_value(payload: object) -> str | None:
    """Built-in panel text. HDMI, webcam, and external-monitor rows are ignored."""
    for label, value in _spec_pairs(payload):
        if label.strip().casefold() == "display":
            return value
    return None


def enrich_offer_from_specifications(
    offer: ThailandOffer,
    payload: object,
    *,
    fetched_at: datetime | None = None,
) -> ThailandOffer:
    """Fill missing specs from a product-detail payload. Does not invent a GPU or a resolution."""
    text = specifications_to_text(payload)
    if not text.strip():
        return offer
    specs = specs_from_text(text)
    filled: dict[str, Any] = {}
    if offer.cpu is None and specs.get("cpu"):
        offer.cpu = specs["cpu"]
        filled["cpu"] = offer.cpu
    if offer.ram_gb is None and specs.get("ram_gb"):
        offer.ram_gb = specs["ram_gb"]
        filled["ram_gb"] = offer.ram_gb
    if offer.ssd_gb is None and specs.get("ssd_gb"):
        offer.ssd_gb = specs["ssd_gb"]
        filled["ssd_gb"] = offer.ssd_gb
    if offer.screen_size_inch is None and specs.get("screen_inch"):
        offer.screen_size_inch = specs["screen_inch"]
        filled["screen_inch"] = offer.screen_size_inch
    display = display_field_value(payload)
    resolution = None
    if display:
        resolution = specs_from_text(display).get("screen_resolution")
    if offer.screen_resolution is None and resolution:
        offer.screen_resolution = str(resolution)
        filled["screen_resolution"] = offer.screen_resolution
        offer.metadata = dict(offer.metadata or {})
        offer.metadata["screen_resolution_evidence"] = {
            "product_id": offer.product_id,
            "display_value": display,
            "screen_resolution": str(resolution),
            "source": "/specifications",
            "fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat(),
        }
    if offer.gpu is None:
        gpu = canonical_gpu(gpu_from_explicit_text(text))
        if gpu in {"RTX 5070 Ti", "RTX 5080"}:
            offer.gpu = gpu
            offer.gpu_source = GPU_SOURCE_STRUCTURED_API
            filled["gpu"] = offer.gpu
    offer.availability_source = offer.availability_source or AVAIL_SOURCE_PRODUCT_DETAIL
    offer.metadata = dict(offer.metadata or {})
    offer.metadata["detail_enriched"] = True
    from thailand.field_evidence import record_field_evidence

    record_field_evidence(
        offer,
        filled,
        method="structured_api",
        captured_at=fetched_at or offer.collected_at,
    )
    return compute_verification(offer)


def _needs_specification(offer: ThailandOffer) -> bool:
    return bool(offer.product_id) and (
        offer.cpu is None or offer.ram_gb is None or not offer.screen_resolution
    )


def _priority_screen_candidate(offer: ThailandOffer, fx: FxRate | None) -> bool:
    """Purchase-shaped card that only lacks a numeric built-in resolution."""
    if offer.screen_resolution or offer.gpu not in {"RTX 5070 Ti", "RTX 5080"}:
        return False
    if offer.ram_gb is None or offer.ram_gb < int(config.MIN_INSTALLED_RAM_GB):
        return False
    if offer.ssd_gb is None or offer.ssd_gb < int(config.MIN_SSD_GB):
        return False
    if not is_purchasable_confirmed(offer):
        return False
    if not fx_usable_for_verdict(fx):
        return False
    return price_within_cap(thb_to_rub(offer.price_thb, fx))


def _detail_order(offers: list[ThailandOffer], fx: FxRate | None) -> list[ThailandOffer]:
    needed = [offer for offer in offers if _needs_specification(offer)]
    needed.sort(
        key=lambda offer: (
            0 if _priority_screen_candidate(offer, fx) else 1,
            str(offer.product_id or ""),
        )
    )
    return needed


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


def _read_json(http: httpx.Client, url: str) -> tuple[Any, str | None, str | None]:
    """HTTP JSON of any shape. Catalog callers still require an object."""
    try:
        resp = http.get(url)
    except httpx.TimeoutException:
        return None, "TIMEOUT", "timeout"
    except httpx.HTTPError as exc:
        code, detail = classify_store_failure(type(exc).__name__)
        return None, code, detail
    if resp.status_code == 429:
        return None, RATE_LIMITED, "HTTP_429"
    if resp.status_code >= 400:
        code, detail = classify_store_failure(status_code=resp.status_code)
        return None, code, detail
    if "json" not in (resp.headers.get("content-type") or "").lower():
        blocked = html_block_code(resp.text)
        if blocked:
            return None, blocked, blocked
        return None, PARSE_ERROR, "not_json"
    try:
        return resp.json(), None, None
    except ValueError:
        return None, PARSE_ERROR, "invalid_json"


def _get_json(http: httpx.Client, url: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Catalog responses must be JSON objects. A list is not a category page."""
    payload, code, detail = _read_json(http, url)
    if code or payload is None:
        return None, code, detail
    if not isinstance(payload, dict):
        return None, PARSE_ERROR, "payload_type"
    return payload, None, None


def _spec_query(product_id: str) -> str:
    return f"specifications:{product_id}"


def _pack_result(
    started: float,
    discovered: list[ThailandOffer],
    skipped: int,
    *,
    rate_limited: bool,
    catalog_complete: bool,
    spec_ids: list[str],
    finished_spec_ids: list[str],
) -> StoreScanResult:
    verified = [offer for offer in discovered if is_verified_for_ranking(offer)]
    unverified = [offer for offer in discovered if not is_verified_for_ranking(offer)]
    finished_specs = [spec_id for spec_id in finished_spec_ids if spec_id in spec_ids]
    unchecked_ids = [spec_id for spec_id in spec_ids if spec_id not in finished_specs]
    limitations = [
        "notebook category catalog",
        f"at most {_MAX_PAGES} pages",
        f"detail fetches capped at {_MAX_DETAIL}",
        "non-target GPU rows are not stored",
        f"rows skipped outside the target GPU: {skipped}",
        "built-in resolution comes only from the Display field",
    ]
    if unchecked_ids:
        limitations.append("not_checked specifications: " + ",".join(unchecked_ids))
    if rate_limited:
        limitations.append("stopped_on_429")
    result = StoreScanResult(
        store=STORE,
        ok=not rate_limited,
        offers=verified,
        unverified_candidates=unverified,
        error="RATE_LIMITED" if rate_limited else None,
        error_code=(
            RATE_LIMITED if rate_limited else (NO_RESULTS if not discovered else None)
        ),
        duration_seconds=time.perf_counter() - started,
        collection_mode="http",
        discovered_count=len(discovered),
        verified_count=len(verified),
    )
    result.discovery_mode = "catalog"
    result.discovery_queries = [CATEGORY] + [_spec_query(spec_id) for spec_id in spec_ids]
    result.queries_finished = (
        [CATEGORY] if catalog_complete else []
    ) + [_spec_query(spec_id) for spec_id in finished_specs]
    result.queries_completed = len(result.queries_finished)
    result.discovery_limitations = limitations
    result.discovery_rate_limited = rate_limited
    return result


def collect(
    *,
    timeout: float = 90.0,
    client: httpx.Client | None = None,
    fx: FxRate | None = None,
) -> StoreScanResult:
    started = time.perf_counter()
    own = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True, timeout=timeout)
    discovered: list[ThailandOffer] = []
    skipped = 0
    try:
        for page in range(1, _MAX_PAGES + 1):
            url = (
                f"{BASE}/api/products/category/{CATEGORY}"
                f"?page={page}&pageSize={_PAGE_SIZE}&includeChildren=true"
            )
            payload, code, detail = _get_json(http, url)
            if code == RATE_LIMITED:
                return _pack_result(
                    started,
                    discovered,
                    skipped,
                    rate_limited=True,
                    catalog_complete=False,
                    spec_ids=[
                        str(offer.product_id)
                        for offer in discovered
                        if offer.product_id and not offer.screen_resolution
                    ],
                    finished_spec_ids=[],
                )
            if payload is None:
                return _failure(started, code or PARSE_ERROR, detail or "request_failed")
            items = payload.get("items")
            if not isinstance(items, list):
                return _failure(started, PARSE_ERROR, "items_missing")
            now = datetime.now(timezone.utc)
            for item in items:
                offer = parse_invadeit_item(item, collected_at=now)
                if offer is None:
                    skipped += 1
                    continue
                discovered.append(offer)
            if not payload.get("hasNextPage"):
                break
        candidates = _detail_order(discovered, fx)
        spec_ids = [str(offer.product_id) for offer in candidates if offer.product_id]
        finished_spec_ids: list[str] = []
        fetches = 0
        rate_limited = False
        for offer in candidates:
            if fetches >= _MAX_DETAIL or rate_limited or not offer.product_id:
                break
            payload, code, _detail = _read_json(
                http, f"{BASE}/api/products/{offer.product_id}/specifications"
            )
            fetches += 1
            if code == RATE_LIMITED:
                rate_limited = True
                break
            finished_spec_ids.append(str(offer.product_id))
            if isinstance(payload, (dict, list)):
                enrich_offer_from_specifications(
                    offer, payload, fetched_at=datetime.now(timezone.utc)
                )
        return _pack_result(
            started,
            discovered,
            skipped,
            rate_limited=rate_limited,
            catalog_complete=True,
            spec_ids=spec_ids,
            finished_spec_ids=finished_spec_ids,
        )
    finally:
        if own:
            http.close()
