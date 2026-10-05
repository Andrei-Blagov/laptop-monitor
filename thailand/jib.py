from __future__ import annotations

"""JIB Thailand adapter — discovery via search_suggestion, verify via readProduct."""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

import config
from deal_ranking import canonical_gpu
from stores.common import extract_specs_from_name
from thailand.models import StoreScanResult, ThailandOffer
from thailand.specs_parse import (
    exclude_non_target_gpu,
    extract_brand,
    extract_mpn_candidates,
    gpu_from_explicit_text,
    normalize_availability,
    parse_thb_price,
    specs_from_text,
)
from thailand.verification import (
    AVAIL_SOURCE_PRODUCT_DETAIL,
    AVAIL_SOURCE_SEARCH_SUGGESTION,
    AVAIL_SOURCE_UNKNOWN,
    GPU_SOURCE_EXPLICIT_TITLE,
    GPU_SOURCE_SEARCH_QUERY,
    GPU_SOURCE_STRUCTURED_PRODUCT_PAGE,
    GPU_SOURCE_UNKNOWN,
    PRICE_SOURCE_PRODUCT_DETAIL,
    PRICE_SOURCE_SEARCH_SUGGESTION,
    UNVERIFIED,
    compute_verification,
    is_verified_for_ranking,
)

logger = logging.getLogger(__name__)

STORE = "jib"
BASE = "https://www.jib.co.th"
SUGGEST_URL = f"{BASE}/web/product/search_suggestion"
READ_PRODUCT_URL = f"{BASE}/web/product/readProduct/{{pid}}"

# Discovery queries only — GPU from query is NEVER authoritative.
TARGET_QUERIES: tuple[tuple[str, str], ...] = (
    ("notebook rtx 5070 ti", "RTX 5070 Ti"),
    ("notebook rtx 5080", "RTX 5080"),
)

# Prefer explicit graphics bullet over SEO meta keywords (which can be wrong).
_GPU_LINE = re.compile(
    r"(?:กราฟิก|GPU|Graphics)\s*[:：]\s*[^<\n]{0,100}?(RTX\s*50\d0(?:\s*Ti)?)",
    re.I,
)
_CPU_LINE = re.compile(
    r"(?:ซีพียู|CPU|Processor)\s*[:：]\s*([^<\n]{5,80})",
    re.I,
)
_RAM_INLINE = re.compile(
    r"\bRAM\s*(\d{1,2})\s*GB\s*(?:DDR|LPDDR)",
    re.I,
)
_SSD_LINE = re.compile(
    r"(?:เอสเอสดี|SSD|Storage)\s*[:：]\s*([^<\n]{3,60})",
    re.I,
)
_SCREEN_LINE = re.compile(
    r"(?:จอแสดงผล|Display|Screen)\s*[:：]\s*([^<\n]{5,100})",
    re.I,
)
_QTY_ASSIGN = re.compile(r"\bqty\s*=\s*(\d+)", re.I)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (compatible; LaptopMonitor/0.4; +https://localhost)"
        ),
        "Accept": "application/json, text/html, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/",
    }


def _product_url(external_id: str, title: str) -> str:
    return READ_PRODUCT_URL.format(pid=external_id)


def _detail_url(external_id: str) -> str:
    return READ_PRODUCT_URL.format(pid=external_id)


def parse_suggestion_discovery(
    items: Iterable[dict[str, Any]],
    *,
    search_context_gpu: str,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    """
    Discovery-only parse of search_suggestion `rec` rows.

    Does NOT set authoritative gpu from search query.
    Does NOT claim in_stock availability.
    """
    collected_at = collected_at or datetime.now(timezone.utc)
    out: list[ThailandOffer] = []
    seen: set[str] = set()
    candidate_gpu = canonical_gpu(search_context_gpu)
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        eid = str(raw.get("id") or "").strip()
        if not title or not eid or eid in seen:
            continue
        # Desktop VGA cards etc.
        if exclude_non_target_gpu(title) and not gpu_from_explicit_text(title):
            continue
        specs = specs_from_text(title, catalog_gpu=search_context_gpu, apply_catalog_gpu=False)
        title_gpu = gpu_from_explicit_text(title)
        # If title explicitly has a non-target GPU, drop.
        if title_gpu and exclude_non_target_gpu(title_gpu):
            continue
        if title and exclude_non_target_gpu(title) and title_gpu is None:
            # Title says VGA/5090/5050 etc.
            if re.search(r"\bVGA\b|การ์ดแสดงผล|RTX\s*5050|RTX\s*5090|RTX\s*5060", title, re.I):
                continue

        regular = parse_thb_price(raw.get("price"))
        sale = parse_thb_price(raw.get("salePrice"))
        if sale and regular and sale < regular:
            price = sale
        else:
            price = sale or regular
        if price is None:
            continue

        available, status = normalize_availability(available_flag=None, status_text=None)
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
            availability_confirmed=False,
            availability_source=AVAIL_SOURCE_SEARCH_SUGGESTION,
            price_source=PRICE_SOURCE_SEARCH_SUGGESTION,
            collected_at=collected_at,
            sku=str(mpn) if mpn else None,
            manufacturer_part_number=str(mpn) if mpn else None,
            brand=str(specs["brand"]) if specs.get("brand") else None,
            cpu=str(specs["cpu"]) if specs.get("cpu") else None,
            gpu=title_gpu,  # only if explicit in title
            gpu_source=GPU_SOURCE_EXPLICIT_TITLE if title_gpu else GPU_SOURCE_UNKNOWN,
            candidate_gpu=candidate_gpu,
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
                "search_context_gpu": search_context_gpu,
                "discovery_only": True,
                "image": raw.get("link"),
            },
        )
        # Mark search query provenance on candidate only
        if candidate_gpu and not title_gpu:
            offer.metadata["candidate_gpu_source"] = GPU_SOURCE_SEARCH_QUERY
        compute_verification(offer)
        seen.add(eid)
        out.append(offer)
    return out


# Backward-compatible name used by older tests — discovery semantics.
def parse_suggestion_rec(
    items: Iterable[dict[str, Any]],
    *,
    catalog_gpu: str,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    return parse_suggestion_discovery(
        items, search_context_gpu=catalog_gpu, collected_at=collected_at
    )


def _parse_ssd_amount(text: str) -> int | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(TB|GB|ทบ|กบ)", text, re.I)
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    unit = m.group(2).upper()
    if unit in {"TB", "ทบ"}:
        return int(amount * 1024)
    return int(amount)


def parse_read_product_html(
    html: str,
    *,
    external_id: str,
    fallback_name: str | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    """Extract product-specific fields from JIB readProduct HTML."""
    collected_at = collected_at or datetime.now(timezone.utc)
    out: dict[str, Any] = {"external_id": external_id, "collected_at": collected_at}
    if not html or len(html) < 80:
        out["error"] = "empty_html"
        return out

    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    name = None
    if h1_m:
        name = re.sub(r"<[^>]+>", " ", h1_m.group(1))
        name = re.sub(r"\s+", " ", name).strip()
    if not name and title_m:
        name = re.sub(r"\s+", " ", title_m.group(1)).strip()
    out["name"] = name or fallback_name or external_id

    # Authoritative GPU from graphics bullet (ignore SEO keyword soup).
    gpu = None
    gpu_source = GPU_SOURCE_UNKNOWN
    observed_gpu_raw = None
    for m in _GPU_LINE.finditer(html):
        observed_gpu_raw = m.group(1).strip()
        key = canonical_gpu(m.group(1))
        if key:
            gpu = key
            gpu_source = GPU_SOURCE_STRUCTURED_PRODUCT_PAGE
            break
        # Non-target but product-evidenced GPU (e.g. RTX 5050) — record, do not promote.
        if observed_gpu_raw:
            out["observed_non_target_gpu"] = observed_gpu_raw
            gpu_source = GPU_SOURCE_STRUCTURED_PRODUCT_PAGE
            break
    if gpu is None and "observed_non_target_gpu" not in out:
        # Fallback: explicit title GPU only (not meta keywords block).
        gpu = gpu_from_explicit_text(out["name"])
        if gpu:
            gpu_source = GPU_SOURCE_EXPLICIT_TITLE
    out["gpu"] = gpu
    out["gpu_source"] = gpu_source

    cpu_m = _CPU_LINE.search(html)
    if cpu_m:
        out["cpu"] = re.sub(r"\s+", " ", cpu_m.group(1)).strip()
    else:
        title_specs = extract_specs_from_name(out["name"])
        if title_specs.get("cpu"):
            out["cpu"] = title_specs["cpu"]

    ram_m = _RAM_INLINE.search(html)
    if ram_m:
        try:
            out["ram_gb"] = int(ram_m.group(1))
        except ValueError:
            pass

    ssd_m = _SSD_LINE.search(html)
    if ssd_m:
        ssd = _parse_ssd_amount(ssd_m.group(1))
        if ssd:
            out["ssd_gb"] = ssd

    screen_m = _SCREEN_LINE.search(html)
    if screen_m:
        sc = screen_m.group(1)
        sm = re.search(r"(15\.6|16\.1|17\.3|18|17|16)", sc)
        if sm:
            out["screen_size_inch"] = float(sm.group(1))
        rm = re.search(r"(\d{3,4})\s*[xх×]\s*(\d{3,4})", sc, re.I)
        if rm:
            out["screen_resolution"] = f"{rm.group(1)}x{rm.group(2)}"

    # Availability from qty assignment when present on product page.
    qty_vals = [int(m.group(1)) for m in _QTY_ASSIGN.finditer(html)]
    if re.search(r"สินค้าหมด|out\s*of\s*stock", html, re.I):
        out["available"] = False
        out["availability_status"] = "out_of_stock"
        out["availability_confirmed"] = True
        out["availability_source"] = AVAIL_SOURCE_PRODUCT_DETAIL
    elif qty_vals:
        qty = max(qty_vals)
        if qty > 0:
            out["available"] = True
            out["availability_status"] = "in_stock"
            out["availability_confirmed"] = True
            out["availability_source"] = AVAIL_SOURCE_PRODUCT_DETAIL
        else:
            out["available"] = False
            out["availability_status"] = "out_of_stock"
            out["availability_confirmed"] = True
            out["availability_source"] = AVAIL_SOURCE_PRODUCT_DETAIL
    else:
        out["available"] = False
        out["availability_status"] = "unknown"
        out["availability_confirmed"] = False
        out["availability_source"] = AVAIL_SOURCE_UNKNOWN

    # Price on detail page (optional) — look for salePrice numeric near product id.
    price_m = re.search(
        rf"['\"]?salePrice['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)",
        html,
        re.I,
    )
    if price_m:
        out["price_thb"] = parse_thb_price(price_m.group(1))
        out["price_source"] = PRICE_SOURCE_PRODUCT_DETAIL

    mpns = extract_mpn_candidates(out["name"])
    if mpns:
        out["manufacturer_part_number"] = mpns[0]
        out["sku"] = mpns[0]
    out["brand"] = extract_brand(out["name"])
    return out


def apply_detail_verification(
    offer: ThailandOffer,
    detail: dict[str, Any],
) -> ThailandOffer:
    """Merge verified detail fields into a discovered offer."""
    if detail.get("name"):
        offer.name = str(detail["name"])
    if detail.get("gpu"):
        # Product evidence always wins over search context.
        offer.gpu = str(detail["gpu"])
        offer.gpu_source = str(
            detail.get("gpu_source") or GPU_SOURCE_STRUCTURED_PRODUCT_PAGE
        )
    if detail.get("cpu") and not offer.cpu:
        offer.cpu = str(detail["cpu"])
    elif detail.get("cpu"):
        offer.cpu = str(detail["cpu"])
    for key in ("ram_gb", "ssd_gb"):
        if detail.get(key) is not None:
            setattr(offer, key, int(detail[key]))
    if detail.get("screen_size_inch") is not None:
        offer.screen_size_inch = float(detail["screen_size_inch"])
    if detail.get("screen_resolution"):
        offer.screen_resolution = str(detail["screen_resolution"])
    if detail.get("manufacturer_part_number"):
        offer.manufacturer_part_number = str(detail["manufacturer_part_number"])
        offer.sku = offer.sku or offer.manufacturer_part_number
    if detail.get("brand"):
        offer.brand = str(detail["brand"])
    if detail.get("price_thb"):
        offer.price_thb = int(detail["price_thb"])
        offer.price_source = str(
            detail.get("price_source") or PRICE_SOURCE_PRODUCT_DETAIL
        )
    if detail.get("availability_confirmed"):
        offer.available = bool(detail.get("available"))
        offer.availability_status = str(detail.get("availability_status") or "unknown")
        offer.availability_confirmed = True
        offer.availability_source = str(
            detail.get("availability_source") or AVAIL_SOURCE_PRODUCT_DETAIL
        )
    offer.url = _detail_url(offer.external_id)
    offer.metadata["detail_verified"] = True
    offer.metadata["discovery_only"] = False
    # If detail shows non-target GPU, clear authoritative target claim.
    if offer.gpu and exclude_non_target_gpu(offer.gpu):
        offer.metadata["rejected_gpu"] = offer.gpu
        offer.gpu = None
        offer.gpu_source = GPU_SOURCE_STRUCTURED_PRODUCT_PAGE
        offer.verification_status = UNVERIFIED
    if detail.get("observed_non_target_gpu"):
        offer.metadata["observed_non_target_gpu"] = detail["observed_non_target_gpu"]
        offer.gpu = None
        offer.gpu_source = GPU_SOURCE_STRUCTURED_PRODUCT_PAGE
    compute_verification(offer)
    return offer


def fetch_read_product(
    client: httpx.Client,
    external_id: str,
    *,
    timeout: float,
    fallback_name: str | None = None,
) -> dict[str, Any]:
    try:
        resp = client.get(_detail_url(external_id), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"external_id": external_id, "error": type(exc).__name__}
    if resp.status_code >= 400:
        return {"external_id": external_id, "error": f"HTTP_{resp.status_code}"}
    return parse_read_product_html(
        resp.text,
        external_id=external_id,
        fallback_name=fallback_name,
    )


def collect(
    *,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    verify_details: bool = True,
    max_detail_fetches: int = 24,
) -> StoreScanResult:
    """
    Discover via search_suggestion, optionally verify via readProduct.

    Returns StoreScanResult.offers = verified ranking-eligible offers only.
    unverified_candidates retained on the result for diagnostics/snapshots.
    """
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
    discovered: list[ThailandOffer] = []
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
            discovered.extend(
                parse_suggestion_discovery(
                    rec, search_context_gpu=gpu, collected_at=now
                )
            )

        by_id: dict[str, ThailandOffer] = {}
        for o in discovered:
            prev = by_id.get(o.external_id)
            if prev is None:
                by_id[o.external_id] = o
                continue
            # Merge candidate GPUs; keep explicit title GPU if any.
            if o.gpu and not prev.gpu:
                by_id[o.external_id] = o
            elif o.candidate_gpu and not prev.candidate_gpu:
                prev.candidate_gpu = o.candidate_gpu

        candidates = list(by_id.values())
        if verify_details and candidates:
            for i, offer in enumerate(candidates):
                if i >= max_detail_fetches:
                    break
                detail = fetch_read_product(
                    client,
                    offer.external_id,
                    timeout=min(timeout, 20.0),
                    fallback_name=offer.name,
                )
                if detail.get("error"):
                    offer.metadata["detail_error"] = detail["error"]
                    compute_verification(offer)
                    continue
                apply_detail_verification(offer, detail)

        verified: list[ThailandOffer] = []
        unverified: list[ThailandOffer] = []
        for o in candidates:
            compute_verification(o)
            # Ranking offers: VERIFIED target GPU only.
            if is_verified_for_ranking(o) and canonical_gpu(o.gpu) in {
                "RTX 5070 Ti",
                "RTX 5080",
            }:
                verified.append(o)
            else:
                unverified.append(o)

        if not candidates:
            return StoreScanResult(
                store=STORE,
                ok=True,
                offers=[],
                unverified_candidates=[],
                error_code="NO_RESULTS",
                duration_seconds=time.perf_counter() - started,
                collection_mode="http",
                discovered_count=0,
                verified_count=0,
            )
        # Store OK if discovery worked even when zero verified (partial market view).
        return StoreScanResult(
            store=STORE,
            ok=True,
            offers=verified,
            unverified_candidates=unverified,
            duration_seconds=time.perf_counter() - started,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("JIB collect failed: %s", type(exc).__name__)
        return StoreScanResult(
            store=STORE,
            ok=False,
            offers=[],
            unverified_candidates=[],
            error=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
        )
    finally:
        if owns:
            client.close()
