from __future__ import annotations

"""IT City retail (itcity.in.th) public product-search adapter.

The storefront publishes an Algolia app id, search key, and index name in the
page config. Collection reads only those three fields and queries the index.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from deal_ranking import canonical_gpu
from thailand.errors import (
    NO_RESULTS,
    PARSE_ERROR,
    UNSUPPORTED,
    classify_store_failure,
    html_block_code,
)
from thailand.models import StoreScanResult, ThailandOffer
from thailand.specs_parse import (
    extract_mpn_candidates,
    gpu_from_explicit_text,
    is_accessory_listing,
    specs_from_text,
)
from thailand.verification import (
    AVAIL_SOURCE_STRUCTURED_API,
    GPU_SOURCE_STRUCTURED_API,
    PRICE_SOURCE_STRUCTURED_API,
    compute_verification,
    is_verified_for_ranking,
)

logger = logging.getLogger(__name__)

STORE = "itcity"
BASE = "https://www.itcity.in.th"
QUERIES = ("RTX 5070 Ti", "RTX 5080", "โน้ตบุ๊ค RTX 5070 Ti", "โน้ตบุ๊ค RTX 5080")
_APP = re.compile(r'algoliaAppId:"([A-Z0-9]+)"')
_KEY = re.compile(r'algoliaApiKey:"([a-f0-9]+)"')
_INDEX = re.compile(r'algoliaProductIndexTh:"([^"]+)"')
_DESKTOP = re.compile(r"การ์ดจอ|\bVGA\b|เซทคอม|คอมประกอบ|comset|graphics\s*card", re.I)
_NOTEBOOK = re.compile(r"notebook|โน้ตบุ๊ค|โน๊ตบุ๊ค|โน้ตบุค", re.I)
_PRIVATE_PRICE = re.compile(r"member|bank|coupon|installment|ผ่อน|สมาชิก", re.I)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (compatible; LaptopMonitor/0.5; +https://localhost)",
        "Accept": "text/html,application/json",
    }


def read_search_config(html: str) -> dict[str, str] | None:
    app = _APP.search(html or "")
    key = _KEY.search(html or "")
    index = _INDEX.search(html or "")
    if not (app and key and index):
        return None
    return {"app_id": app.group(1), "api_key": key.group(1), "index": index.group(1)}


def _num(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        return _num(value.get("value") or value.get("amount"))
    try:
        amount = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return int(round(amount))


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value.replace("'", '"'))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            return []
    return []


def _brand_label(value: object) -> str | None:
    if isinstance(value, dict):
        label = value.get("label") or value.get("value")
        return str(label) if label else None
    if isinstance(value, str) and value.strip().startswith("{"):
        return None
    return str(value) if value else None


def _sku(hit: dict[str, Any]) -> str | None:
    raw = hit.get("sku")
    if isinstance(raw, list) and raw:
        return str(raw[0])
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    sku_id = hit.get("skuProductId")
    return str(sku_id) if sku_id else None


def _is_notebook(hit: dict[str, Any], name: str) -> bool:
    blob = " ".join(
        str(hit.get(key) or "")
        for key in ("categories", "categoryName", "categoryNames", "categorySlug")
    )
    blob = f"{blob} {name}"
    if _DESKTOP.search(blob) or is_accessory_listing(blob):
        return False
    return bool(_NOTEBOOK.search(blob) or "NOTEBOOK" in blob.upper())


def _prices(hit: dict[str, Any]) -> tuple[int | None, int | None]:
    regular = _num(hit.get("price"))
    special = hit.get("specialPrice")
    remark = ""
    sale = None
    if isinstance(special, dict):
        remark = str(special.get("remark") or "")
        sale = _num(special.get("value"))
    elif hit.get("isSpecialPrice"):
        sale = _num(special)
    if remark and _PRIVATE_PRICE.search(remark):
        sale = None
    if sale and regular and sale < regular:
        return sale, regular
    if sale and regular is None:
        return sale, None
    return regular, None


def _product_url(hit: dict[str, Any]) -> str:
    explicit = hit.get("url") or hit.get("slug")
    if isinstance(explicit, str) and explicit.strip():
        if explicit.startswith("http"):
            return explicit
        return f"{BASE}{explicit if explicit.startswith('/') else '/' + explicit}"
    pid = hit.get("productId") or hit.get("objectID")
    if pid:
        return f"{BASE}/product/{pid}"
    return BASE


def parse_itcity_hit(
    hit: dict[str, Any],
    *,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    if not isinstance(hit, dict):
        return []
    children = [c for c in _as_list(hit.get("children")) if isinstance(c, dict)]
    if children and any(c.get("productName") or c.get("name") for c in children):
        offers: list[ThailandOffer] = []
        for child in children:
            merged = dict(hit)
            merged.pop("children", None)
            merged.update({k: v for k, v in child.items() if v not in (None, "", [])})
            offers.extend(parse_itcity_hit(merged, collected_at=collected_at))
        return offers
    name = str(hit.get("productName") or hit.get("name") or "").strip()
    if not name or not _is_notebook(hit, name):
        return []
    description = str(hit.get("shortDescription") or hit.get("description") or "")
    evidence = f"{name}\n{description}"
    gpu = canonical_gpu(gpu_from_explicit_text(evidence))
    if gpu not in {"RTX 5070 Ti", "RTX 5080"}:
        return []
    specs = specs_from_text(evidence)
    price, regular = _prices(hit)
    stock = hit.get("stockAvailable")
    if stock is True:
        available, status, confirmed = True, "in_stock", True
    elif stock is False:
        available, status, confirmed = False, "out_of_stock", True
    else:
        available, status, confirmed = False, "unknown", False
    sku = _sku(hit)
    mpns = extract_mpn_candidates(name)
    collected_at = collected_at or datetime.now(timezone.utc)
    external = str(hit.get("objectID") or hit.get("productId") or sku or name)
    offer = ThailandOffer(
        store=STORE,
        external_id=external,
        name=name,
        url=_product_url(hit),
        price_thb=price,
        available=available,
        collected_at=collected_at,
        sku=sku,
        manufacturer_part_number=mpns[0] if mpns else sku,
        brand=specs.get("brand") or _brand_label(hit.get("brand")),
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
        product_id=str(hit.get("productId") or "") or None,
        metadata={"index_object": hit.get("objectID")},
    )
    return [compute_verification(offer)]


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
    target_codes: list[str] | None = None,
) -> StoreScanResult:
    started = time.perf_counter()
    own = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True, timeout=timeout)
    discovered: list[ThailandOffer] = []
    seen: set[str] = set()
    try:
        try:
            page = http.get(BASE + "/")
        except httpx.TimeoutException:
            return _failure(started, "TIMEOUT", "timeout")
        except httpx.HTTPError as exc:
            code, detail = classify_store_failure(type(exc).__name__)
            return _failure(started, code, detail)
        if page.status_code >= 400:
            code, detail = classify_store_failure(status_code=page.status_code)
            return _failure(started, code, detail)
        blocked = html_block_code(page.text)
        if blocked:
            return _failure(started, blocked, blocked)
        cfg = read_search_config(page.text)
        if cfg is None:
            return _failure(started, UNSUPPORTED, "search_config_missing")
        endpoint = (
            f"https://{cfg['app_id']}-dsn.algolia.net/1/indexes/{cfg['index']}/query"
        )
        headers = {
            "X-Algolia-Application-Id": cfg["app_id"],
            "X-Algolia-API-Key": cfg["api_key"],
            "Content-Type": "application/json",
        }
        now = datetime.now(timezone.utc)
        queries = list(QUERIES)
        for code in list(target_codes or [])[:1]:
            if code and code not in queries:
                queries.append(str(code))
        requests = 1  # storefront page that publishes the search config
        for query in queries:
            for page_no in range(3):
                try:
                    resp = http.post(
                        endpoint,
                        headers=headers,
                        json={"query": query, "hitsPerPage": 40, "page": page_no},
                    )
                    requests += 1
                except httpx.TimeoutException:
                    return _failure(started, "TIMEOUT", "timeout")
                except httpx.HTTPError as exc:
                    code, detail = classify_store_failure(type(exc).__name__)
                    return _failure(started, code, detail)
                if resp.status_code == 429:
                    result = _failure(started, "RATE_LIMITED", "HTTP_429")
                    result.request_count = requests
                    return result
                if resp.status_code >= 400:
                    code, detail = classify_store_failure(status_code=resp.status_code)
                    return _failure(started, code, detail)
                try:
                    payload = resp.json()
                except ValueError:
                    return _failure(started, PARSE_ERROR, "invalid_json")
                hits = payload.get("hits") if isinstance(payload, dict) else None
                if not isinstance(hits, list):
                    return _failure(started, PARSE_ERROR, "hits_missing")
                for hit in hits:
                    for offer in parse_itcity_hit(hit, collected_at=now):
                        if offer.external_id in seen:
                            continue
                        seen.add(offer.external_id)
                        discovered.append(offer)
                nb_pages = int(payload.get("nbPages") or 1)
                if page_no + 1 >= nb_pages:
                    break
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
            request_count=requests,
        )
    finally:
        if own:
            http.close()
