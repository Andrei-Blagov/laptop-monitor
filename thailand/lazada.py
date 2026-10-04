from __future__ import annotations

"""Lazada Thailand marketplace adapter (marketplace=true channel)."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx

import config
from deal_ranking import canonical_gpu
from stores.common import looks_like_challenge_page
from thailand.models import StoreScanResult, ThailandOffer
from thailand.seller_trust import (
    annotate_marketplace_identity,
    classify_seller_trust,
    marketplace_confidence,
)
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
    PARTIAL,
    PRICE_SOURCE_STRUCTURED_API,
    UNVERIFIED,
    VERIFIED,
    compute_verification,
    is_verified_for_ranking,
)

logger = logging.getLogger(__name__)

STORE = "lazada"
BASE = "https://www.lazada.co.th"


def looks_like_lazada_punish(html: str) -> bool:
    """Lazada/Aliyun slide/punish interstitial (not Cloudflare; do not bypass)."""
    low = (html or "").lower()
    return any(
        marker in low
        for marker in (
            "sufei-punish",
            "bx-pu-qrcode",
            "x5secdata",
            "punish/baxia",
            "captcha",
        )
    ) and ("alicdn.com" in low or "lazada" in low or "x5sec" in low)

# Generic discovery + targeted trusted-seller discovery (one browser session).
# Query seller tokens are discovery hints only — never override listing seller/GPU.
LAZADA_SEARCH_PLAN: tuple[tuple[str, str | None], ...] = (
    ("RTX 5070 Ti notebook", None),
    ("RTX 5080 notebook", None),
    ("BaNANA IT RTX 5070 Ti notebook", "BaNANA IT"),
    ("BaNANA IT RTX 5080 notebook", "BaNANA IT"),
    ("JIB Computer Group RTX 5070 Ti notebook", "JIB"),
    ("ASUS Official Store RTX 5070 Ti notebook", "ASUS"),
    ("GIGABYTE AORUS RTX 5070 Ti notebook", "GIGABYTE"),
    ("Lenovo Official RTX 5070 Ti notebook", "Lenovo"),
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    }


def _search_url(query: str) -> str:
    return f"{BASE}/catalog/?q={quote_plus(query)}"


def _parse_sold(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).lower().replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*k", text)
    if m:
        try:
            return int(float(m.group(1)) * 1000)
        except ValueError:
            return None
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "official", "mall"}


def _extract_list_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    mods = payload.get("mods") or payload.get("data") or payload
    if isinstance(mods, dict):
        for key in ("listItems", "itemList", "products", "items", "results"):
            if isinstance(mods.get(key), list):
                return [x for x in mods[key] if isinstance(x, dict)]
    # deep search
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list) and cur and isinstance(cur[0], dict):
            if any("itemId" in x or "item_id" in x or "name" in x for x in cur[:3]):
                return [x for x in cur if isinstance(x, dict)]
        if isinstance(cur, dict):
            stack.extend(cur.values())
    return []


def extract_list_items_from_html(html: str) -> list[dict[str, Any]]:
    if not html:
        return []
    # window.pageData = {...}
    m = re.search(r"window\.pageData\s*=\s*(\{)", html)
    if m:
        start = m.start(1)
        depth = 0
        end = None
        for i, ch in enumerate(html[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                data = json.loads(html[start:end])
                items = _extract_list_items(data)
                if items:
                    return items
            except json.JSONDecodeError:
                pass
    # Embedded "listItems":[...]
    m = re.search(r'"listItems"\s*:\s*(\[)', html)
    if m:
        start = m.start(1)
        depth = 0
        end = None
        for i, ch in enumerate(html[start:], start=start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                arr = json.loads(html[start:end])
                if isinstance(arr, list):
                    return [x for x in arr if isinstance(x, dict)]
            except json.JSONDecodeError:
                pass
    return []


def parse_lazada_listing(
    raw: dict[str, Any],
    *,
    catalog_gpu: str | None = None,
    collected_at: datetime | None = None,
    from_price_only: bool = False,
) -> ThailandOffer | None:
    """
    Parse one Lazada listing/variant dict into ThailandOffer.

    Public listed/sale price only — never apply personalized vouchers.
    Variant price must be exact; from-price alone → unverified.
    """
    collected_at = collected_at or datetime.now(timezone.utc)
    name = str(
        raw.get("name")
        or raw.get("title")
        or raw.get("productTitle")
        or ""
    ).strip()
    item_id = str(
        raw.get("itemId")
        or raw.get("item_id")
        or raw.get("product_id")
        or raw.get("nid")
        or raw.get("external_id")
        or ""
    ).strip()
    sku_id = str(
        raw.get("skuId")
        or raw.get("sku_id")
        or raw.get("variant_id")
        or raw.get("sku")
        or ""
    ).strip()
    if not name or not item_id:
        return None
    if exclude_non_target_gpu(name):
        return None

    # Personalized voucher must NOT reduce price.
    price = parse_thb_price(
        raw.get("price")
        or raw.get("public_sale_price")
        or raw.get("salePrice")
        or raw.get("priceShow")
        or raw.get("listed_price")
    )
    listed = parse_thb_price(
        raw.get("listed_price") or raw.get("originalPrice") or raw.get("priceShow")
    )
    if price is None and listed is not None:
        price = listed
    voucher_text = None
    for key in ("voucher_text", "voucherInfo", "promotion", "promo_notes"):
        if raw.get(key):
            voucher_text = str(raw.get(key))[:240]
            break

    # from-price / multi-variant without exact mapping → do not verify price
    price_verified = bool(raw.get("price_verified_for_variant", True))
    if from_price_only or raw.get("from_price") or raw.get("price_is_from"):
        price_verified = False
    if raw.get("variants") and not raw.get("selected_variant"):
        # Multi-variant payload without selected variant
        price_verified = False

    gpu_source = GPU_SOURCE_UNKNOWN
    gpu = None
    if raw.get("gpu"):
        gpu = str(raw["gpu"])
        gpu_source = GPU_SOURCE_STRUCTURED_API
    else:
        # Prefer structured variant GPU over title when provided
        variant_gpu = raw.get("variant_gpu") or raw.get("selected_variant_gpu")
        if variant_gpu:
            gpu = str(variant_gpu)
            gpu_source = GPU_SOURCE_STRUCTURED_API
        else:
            title_gpu = gpu_from_explicit_text(name)
            if title_gpu:
                gpu = title_gpu
                gpu_source = GPU_SOURCE_EXPLICIT_TITLE
    # Title says 5080 but selected variant says otherwise → do not confirm title GPU
    selected_gpu = raw.get("selected_variant_gpu")
    if selected_gpu and gpu_from_explicit_text(name):
        if canonical_gpu(selected_gpu) != canonical_gpu(gpu_from_explicit_text(name)):
            gpu = str(selected_gpu)
            gpu_source = GPU_SOURCE_STRUCTURED_API
            price_verified = bool(raw.get("price_verified_for_variant", False))

    if not is_target_laptop_gpu(gpu):
        return None
    if price is None:
        return None

    specs = specs_from_text(name, catalog_gpu=None, apply_catalog_gpu=False)
    for key in ("cpu", "brand"):
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

    avail_text = str(
        raw.get("availability_status") or raw.get("availability") or ""
    )
    available, status = normalize_availability(
        available_flag=raw.get("available")
        if (avail_text or raw.get("available") is not None)
        else True,
        status_text=avail_text or "in_stock",
    )
    avail_confirmed = True
    if status == "unknown" and raw.get("available") is None and not avail_text:
        # Search card defaults to in_stock when Lazada shows buyable listing
        available, status = True, "in_stock"

    url = str(raw.get("url") or raw.get("productUrl") or raw.get("itemUrl") or "")
    if url and url.startswith("//"):
        url = "https:" + url
    if url and url.startswith("/"):
        url = urljoin(BASE, url)
    if not url:
        url = f"{BASE}/products/i{item_id}.html"

    seller_name = str(
        raw.get("seller_name")
        or raw.get("sellerName")
        or raw.get("shopName")
        or raw.get("seller")
        or ""
    ).strip() or None
    seller_id = str(
        raw.get("seller_id") or raw.get("sellerId") or raw.get("shopId") or ""
    ).strip() or None
    official = _boolish(
        raw.get("official_store")
        or raw.get("isOfficial")
        or raw.get("official")
        or (raw.get("seller_type") == "official")
    )
    mall = _boolish(raw.get("mall") or raw.get("isMall") or raw.get("tItemType") == "mall")
    rating = raw.get("seller_rating")
    if rating is None:
        rating = raw.get("ratingScore") or raw.get("rating")
    try:
        rating_f = float(rating) if rating is not None and str(rating) != "" else None
    except (TypeError, ValueError):
        rating_f = None
    reviews = raw.get("seller_reviews_count")
    if reviews is None:
        reviews = raw.get("review") or raw.get("reviewCount") or raw.get("evaluates")
    try:
        reviews_i = int(float(reviews)) if reviews is not None and str(reviews) != "" else None
    except (TypeError, ValueError):
        reviews_i = None
    sold = _parse_sold(raw.get("units_sold") or raw.get("sold") or raw.get("volume"))

    candidate = canonical_gpu(str(catalog_gpu)) if catalog_gpu else None
    eid = f"{item_id}:{sku_id}" if sku_id else item_id
    offer = ThailandOffer(
        store=STORE,
        external_id=eid,
        name=name,
        url=url,
        price_thb=int(price),
        regular_price_thb=int(listed) if listed and listed != price else None,
        available=available,
        availability_status=status,
        availability_confirmed=avail_confirmed,
        availability_source=AVAIL_SOURCE_STRUCTURED_API
        if avail_confirmed
        else AVAIL_SOURCE_UNKNOWN,
        price_source=PRICE_SOURCE_STRUCTURED_API,
        collected_at=collected_at,
        sku=sku_id or None,
        manufacturer_part_number=str(raw["mpn"]) if raw.get("mpn") else None,
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
        marketplace=True,
        seller_name=seller_name,
        seller_id=seller_id,
        seller_type=str(raw["seller_type"]) if raw.get("seller_type") else (
            "official" if official else "marketplace"
        ),
        seller_rating=rating_f,
        seller_reviews_count=reviews_i,
        units_sold=sold,
        official_store=official,
        mall=mall,
        listing_id=item_id,
        product_id=item_id,
        variant_id=sku_id or None,
        price_verified_for_variant=price_verified,
        voucher_text=voucher_text,
        promo_notes=str(raw["promo_notes"])[:240] if raw.get("promo_notes") else None,
        metadata={
            "source": raw.get("source") or "lazada_listing",
            "site": "lazada.co.th",
            "candidate_gpu_source": GPU_SOURCE_SEARCH_QUERY if candidate else None,
            "listed_price": listed,
            "public_sale_price": price,
        },
    )
    offer = compute_verification(offer)
    if not price_verified:
        offer.verification_status = (
            PARTIAL if offer.verification_status == VERIFIED else UNVERIFIED
        )
        offer.verification_reasons = list(offer.verification_reasons or [])
        offer.verification_reasons.append("variant_price_unverified")
    offer.seller_trust_tier = classify_seller_trust(offer)
    offer.marketplace_confidence = marketplace_confidence(
        offer, tier=offer.seller_trust_tier
    )
    return offer


def parse_lazada_payload(
    payload: Any,
    *,
    catalog_gpu: str | None = None,
    collected_at: datetime | None = None,
) -> list[ThailandOffer]:
    items = _extract_list_items(payload) if not isinstance(payload, list) else payload
    if isinstance(payload, dict) and not items:
        items = [payload]
    out: list[ThailandOffer] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        offer = parse_lazada_listing(
            raw, catalog_gpu=catalog_gpu, collected_at=collected_at
        )
        if offer:
            out.append(offer)
    return out


def _dedupe_offers(offers: list[ThailandOffer]) -> list[ThailandOffer]:
    by_key: dict[str, ThailandOffer] = {}
    for o in offers:
        keys = [
            o.external_id,
            o.listing_id or "",
            o.product_id or "",
            (o.url or "").split("?")[0],
        ]
        key = next((k for k in keys if k), o.external_id)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = o
            continue
        # Keep cheaper public price when duplicate.
        if (o.price_thb or 10**12) < (prev.price_thb or 10**12):
            by_key[key] = o
    return list(by_key.values())


def _collect_http_probe(
    client: httpx.Client, *, timeout: float, collected_at: datetime
) -> tuple[list[ThailandOffer], list[str], str | None]:
    """Fast HTTP probe — only first two generic queries (shell HTML expected)."""
    return _collect_http(
        client,
        timeout=timeout,
        collected_at=collected_at,
        plan=LAZADA_SEARCH_PLAN[:2],
    )


def _collect_http(
    client: httpx.Client,
    *,
    timeout: float,
    collected_at: datetime,
    plan: tuple[tuple[str, str | None], ...] | None = None,
) -> tuple[list[ThailandOffer], list[str], str | None]:
    offers: list[ThailandOffer] = []
    errors: list[str] = []
    mode: str | None = None
    for q, _hint in (plan if plan is not None else LAZADA_SEARCH_PLAN):
        # _hint is discovery-only; never applied as seller override.
        gpu = "RTX 5080" if "5080" in q else "RTX 5070 Ti"
        url = _search_url(q)
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
                batch = parse_lazada_payload(
                    resp.json(), catalog_gpu=gpu, collected_at=collected_at
                )
                offers.extend(batch)
                if batch:
                    mode = "http"
            except ValueError:
                errors.append("invalid_json")
            continue
        if looks_like_challenge_page(resp.text):
            errors.append("challenge")
            continue
        items = extract_list_items_from_html(resp.text)
        if items:
            batch = parse_lazada_payload(
                items, catalog_gpu=gpu, collected_at=collected_at
            )
            offers.extend(batch)
            if batch:
                mode = "html"
        elif not items:
            # Shell HTML without embedded products — need browser
            errors.append("shell_html")
    return offers, errors, mode


def _parse_json_response_text(text: str) -> Any | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # mtop callback / padded JSON
    m = re.search(r"(\{.*\}|\[.*\])\s*$", text, re.S)
    if not m:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return None


def _parse_dom_cards(
    cards: list[dict[str, Any]],
    *,
    catalog_gpu: str | None,
    collected_at: datetime,
) -> list[ThailandOffer]:
    batch: list[ThailandOffer] = []
    for card in cards or []:
        name = str(card.get("name") or "").strip()
        href = str(card.get("href") or "").strip()
        text = str(card.get("text") or "")
        if not name or not href:
            continue
        m = re.search(r"[Ii]-?(\d{6,})", href) or re.search(
            r"/products/[^?]*-i(\d+)", href
        )
        item_id = m.group(1) if m else href.rstrip("/").split("/")[-1]
        low_text = text.lower()
        official = any(
            x in low_text
            for x in ("official store", "flagship store", "ร้านค้าอย่างเป็นทางการ")
        )
        mall = any(x in low_text for x in ("lazmall", "laz mall", "mall"))
        seller = None
        for line in text.splitlines():
            line_s = line.strip()
            if not line_s or len(line_s) < 2:
                continue
            low = line_s.lower()
            if parse_thb_price(line_s) is not None and len(line_s) < 20:
                continue
            if any(
                k in low
                for k in (
                    "official",
                    "store",
                    "jib",
                    "banana",
                    "asus",
                    "lenovo",
                    "advice",
                    "mall",
                    "gigabyte",
                    "aorus",
                    "msi",
                    "acer",
                    "computer",
                    "smart",
                )
            ):
                seller = line_s[:80]
                break
        if seller is None:
            for line in reversed(text.splitlines()):
                line_s = line.strip()
                if len(line_s) < 3 or len(line_s) > 60:
                    continue
                if parse_thb_price(line_s) is not None:
                    continue
                if line_s.lower() in {name.lower(), "lazada"}:
                    continue
                seller = line_s[:80]
                break
        prices = re.findall(
            r"(?:฿|THB|บาท)?\s*([0-9]{2,3}(?:,[0-9]{3})+)", text
        )
        price_verified = len(prices) == 1 and bool(gpu_from_explicit_text(name))
        rating = None
        rm = re.search(r"\b([0-5]\.\d)\b", text)
        if rm:
            try:
                rating = float(rm.group(1))
            except ValueError:
                rating = None
        sold = None
        sm = re.search(r"([\d.,]+)\s*(?:sold|ชิ้น|ขายแล้ว)", text, re.I)
        if sm:
            sold = _parse_sold(sm.group(1))
        raw = {
            "name": name,
            "itemId": item_id,
            "url": href,
            "price": parse_thb_price(text),
            "seller_name": seller,
            "seller_rating": rating,
            "units_sold": sold,
            "available": True,
            "availability_status": "in_stock",
            "source": "lazada_browser_dom",
            "price_verified_for_variant": price_verified,
            "official_store": official
            or bool(seller and "official" in seller.lower()),
            "mall": mall,
        }
        offer = parse_lazada_listing(
            raw, catalog_gpu=catalog_gpu, collected_at=collected_at
        )
        if offer:
            batch.append(offer)
    return batch


def _collect_browser(
    *, timeout: float, collected_at: datetime
) -> tuple[list[ThailandOffer], list[str]]:
    """One Chromium session, generic + targeted seller queries, bounded waits."""
    from thailand.browser import chromium_page

    offers: list[ThailandOffer] = []
    errors: list[str] = []
    timeout_ms = int(max(8_000, min(timeout * 1000, 90_000)))
    wait_ms = int(getattr(config, "THAILAND_LAZADA_QUERY_WAIT_MS", 3500))
    try:
        # Do not block CSS — Lazada search cards often fail to hydrate without it.
        with chromium_page(
            timeout_ms=timeout_ms, block_heavy_resources=False
        ) as page:
            captured: list[Any] = []

            def on_resp(resp) -> None:
                try:
                    u = resp.url
                    ct = (resp.headers.get("content-type") or "").lower()
                    if resp.status != 200:
                        return
                    if not (
                        "json" in ct or "mtop" in u or "search" in u or "list" in u
                    ):
                        return
                    body = resp.text()
                    if "itemId" not in body and "listItems" not in body:
                        return
                    parsed = _parse_json_response_text(body)
                    if parsed is not None:
                        captured.append(parsed)
                except Exception:  # noqa: BLE001
                    return

            page.on("response", on_resp)
            for qi, (q, _seller_hint) in enumerate(LAZADA_SEARCH_PLAN):
                # seller_hint is discovery-only; never applied as seller override.
                gpu = "RTX 5080" if "5080" in q else "RTX 5070 Ti"
                url = _search_url(q)
                captured.clear()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    # First query gets a slightly longer hydrate window.
                    page.wait_for_timeout(max(wait_ms, 6000) if qi == 0 else wait_ms)
                    try:
                        page.wait_for_selector(
                            'a[href*="/products/"]',
                            timeout=min(10_000 if qi == 0 else 6_000, timeout_ms),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    html = page.content()
                except Exception as exc:  # noqa: BLE001
                    errors.append(type(exc).__name__)
                    continue
                if looks_like_challenge_page(html) or looks_like_lazada_punish(html):
                    errors.append("challenge")
                    break
                batch: list[ThailandOffer] = []
                for payload in list(captured):
                    batch.extend(
                        parse_lazada_payload(
                            payload, catalog_gpu=gpu, collected_at=collected_at
                        )
                    )
                if not batch:
                    items = extract_list_items_from_html(html)
                    if items:
                        batch = parse_lazada_payload(
                            items, catalog_gpu=gpu, collected_at=collected_at
                        )
                if not batch:
                    try:
                        page_data = page.evaluate(
                            "() => window.pageData || window.__INIT_DATA__ || null"
                        )
                    except Exception:  # noqa: BLE001
                        page_data = None
                    if page_data:
                        batch = parse_lazada_payload(
                            page_data, catalog_gpu=gpu, collected_at=collected_at
                        )
                if not batch:
                    try:
                        cards = page.evaluate(
                            """() => {
                            const as = [...document.querySelectorAll('a[href*="/products/"]')];
                            const out = [];
                            const seen = new Set();
                            for (const a of as) {
                              if (seen.has(a.href)) continue;
                              seen.add(a.href);
                              const root = a.closest('[data-qa-locator], .Bm3ON, .card') || a.parentElement;
                              const text = (root?.innerText || a.innerText || '').slice(0, 800);
                              out.push({
                                href: a.href,
                                name: (a.getAttribute('title') || a.innerText || '').trim().split('\\n')[0],
                                text
                              });
                              if (out.length >= 40) break;
                            }
                            return out;
                        }"""
                        )
                    except Exception:  # noqa: BLE001
                        cards = []
                    batch = _parse_dom_cards(
                        cards or [], catalog_gpu=gpu, collected_at=collected_at
                    )
                if not batch:
                    errors.append(f"browser_empty:{qi}")
                for o in batch:
                    o.metadata["collection_mode"] = "browser"
                    o.metadata["discovery_query"] = q
                    # Never trust query seller hint as listing seller.
                    annotate_marketplace_identity(o)
                offers.extend(batch)
            try:
                page.remove_listener("response", on_resp)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        errors.append(type(exc).__name__)
    return _dedupe_offers(offers), errors


def collect(
    *,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    allow_browser: bool | None = None,
) -> StoreScanResult:
    if not bool(getattr(config, "THAILAND_LAZADA_ENABLED", True)):
        return StoreScanResult(
            store=STORE,
            ok=False,
            offers=[],
            error="disabled",
            collection_mode="failed",
        )
    timeout = float(
        timeout
        if timeout is not None
        else config.THAILAND_PER_STORE_TIMEOUT_SECONDS
    )
    allow_browser = (
        bool(config.THAILAND_LAZADA_BROWSER_ENABLED)
        if allow_browser is None
        else bool(allow_browser)
    )
    started = time.perf_counter()
    owns = client is None
    client = client or httpx.Client(
        headers=_headers(), follow_redirects=True, timeout=timeout
    )
    collection_mode = "failed"
    try:
        now = datetime.now(timezone.utc)
        # Lazada search shell HTML rarely embeds products — probe 2 generic
        # HTTP queries only, then one browser session for full generic+targeted plan.
        offers, errors, mode = _collect_http_probe(
            client, timeout=min(timeout, 20.0), collected_at=now
        )
        if mode:
            collection_mode = mode
        # Prefer one browser session for full generic+targeted discovery.
        if allow_browser:
            b_offers, b_errors = _collect_browser(
                timeout=timeout, collected_at=now
            )
            errors.extend(b_errors)
            if b_offers:
                offers = _dedupe_offers(list(offers) + list(b_offers))
                collection_mode = "browser" if not mode else f"{mode}+browser"
            elif not offers and "challenge" in b_errors:
                collection_mode = "failed"
        elif not offers:
            collection_mode = "failed"
        for o in offers:
            annotate_marketplace_identity(o)
            o.seller_trust_tier = classify_seller_trust(o)
            o.marketplace_confidence = marketplace_confidence(
                o, tier=o.seller_trust_tier
            )
        final = _dedupe_offers(offers)
        verified = [o for o in final if is_verified_for_ranking(o)]
        unverified = [o for o in final if not is_verified_for_ranking(o)]
        if not verified:
            return StoreScanResult(
                store=STORE,
                ok=False,
                offers=[],
                unverified_candidates=unverified,
                error=",".join(errors) if errors else "no_target_offers",
                duration_seconds=time.perf_counter() - started,
                collection_mode=collection_mode,
            )
        return StoreScanResult(
            store=STORE,
            ok=True,
            offers=verified,
            unverified_candidates=unverified,
            error=",".join(errors) if errors else None,
            duration_seconds=time.perf_counter() - started,
            collection_mode=collection_mode,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lazada collect failed: %s", type(exc).__name__)
        return StoreScanResult(
            store=STORE,
            ok=False,
            offers=[],
            error=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
            collection_mode="failed",
        )
    finally:
        if owns:
            client.close()
