from __future__ import annotations

"""Citilink Moscow — browser collection via search + JSON-LD product pages."""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urljoin

from models import Product
from stores.common import (
    clean_text,
    extract_specs_from_name,
    looks_like_challenge_page,
    matches_target_gpu,
    parse_price,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.citilink.ru"
SEARCH_QUERIES = (
    "ноутбук RTX 5070 Ti",
    "ноутбук RTX 5080",
)


def _product_id_from_url(url: str) -> str | None:
    m = re.search(r"-(\d{6,})/?$", url.rstrip("/"))
    return m.group(1) if m else None


def _mpn_from_name(name: str) -> str | None:
    m = re.search(r"\[([^\]]+)\]", name)
    if m:
        return clean_text(m.group(1)).lstrip("_")
    return None


def parse_citilink_product_html(
    html: str, url: str, *, now: datetime | None = None
) -> Product | None:
    """Parse a single product page (JSON-LD Offer preferred)."""
    if looks_like_challenge_page(html) and "application/ld+json" not in html:
        raise RuntimeError("Citilink challenge page without product data")
    now = now or datetime.now(timezone.utc)
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") != "Product":
                continue
            name = clean_text(str(node.get("name") or ""))
            if not name or not matches_target_gpu(name):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                continue
            price = parse_price(offers.get("price"))
            if price is None or price < 10_000:
                continue
            availability = str(offers.get("availability") or "")
            available = "InStock" in availability or "instock" in availability.lower()
            avail_status = (
                "in_stock"
                if available
                else (
                    "preorder"
                    if "PreOrder" in availability
                    else "out_of_stock"
                    if "OutOfStock" in availability
                    else "unknown"
                )
            )
            external_id = str(node.get("sku") or _product_id_from_url(url) or "")
            if not external_id:
                continue
            sku = node.get("mpn") or _mpn_from_name(name)
            specs = extract_specs_from_name(name)
            meta = {"availability_status": avail_status, **specs}
            return Product(
                store="citilink",
                external_id=external_id,
                url=url.split("?")[0],
                name=name,
                sku=str(sku) if sku else None,
                price=int(price),
                available=available,
                checked_at=now,
                metadata=meta,
            )
    # Fallback: visible price on product page (public, not credit).
    name_m = re.search(
        r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S
    ) or re.search(r'itemprop="name"[^>]*>(.*?)<', html, re.I | re.S)
    name = clean_text(re.sub(r"<[^>]+>", " ", name_m.group(1))) if name_m else ""
    if not name or not matches_target_gpu(name):
        return None
    price = None
    for pat in (
        r'data-price=["\'](\d+)',
        r'"price"\s*:\s*"?(\d{5,})"?',
        r'itemprop="price"\s+content="(\d+)',
        r'class="[^"]*ProductHeader__price-default[^"]*"[^>]*>\s*([\d\s\u00a0]+)',
    ):
        m = re.search(pat, html, re.I)
        if m:
            price = parse_price(m.group(1))
            if price and price >= 10_000:
                break
    if price is None or price < 10_000:
        return None
    external_id = _product_id_from_url(url) or ""
    if not external_id:
        return None
    specs = extract_specs_from_name(name)
    return Product(
        store="citilink",
        external_id=external_id,
        url=url.split("?")[0],
        name=name,
        sku=_mpn_from_name(name),
        price=int(price),
        available=True,
        checked_at=now,
        metadata={"availability_status": "unknown", **specs},
    )


def extract_product_links(html: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r'href="(/product/noutbuk-[^"?#]+)', html, re.I):
        if "/otzyvy" in href:
            continue
        full = urljoin(BASE_URL, href)
        if full in seen:
            continue
        if not re.search(r"rtx\s*5070|rtx5070|rtx\s*5080|rtx5080", href, re.I):
            continue
        seen.add(full)
        links.append(full)
    return links


def fetch_target_laptops(
    *,
    html_pages: Sequence[tuple[str, str]] | None = None,
    max_products: int = 40,
) -> list[Product]:
    """
    html_pages: optional list of (url, html) product pages for unit tests.
    Live mode uses Playwright Chromium (no stealth / no bypass).
    """
    if html_pages is not None:
        out: list[Product] = []
        seen: set[str] = set()
        for url, html in html_pages:
            product = parse_citilink_product_html(html, url)
            if product is None or product.external_id in seen:
                continue
            seen.add(product.external_id)
            out.append(product)
        return out

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Citilink browser collector requires playwright. "
            "Install: pip install playwright && playwright install chromium"
        ) from exc

    products: list[Product] = []
    seen: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        links: list[str] = []
        for query in SEARCH_QUERIES:
            url = f"{BASE_URL}/search/?text={query.replace(' ', '+')}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            html = page.content()
            if looks_like_challenge_page(html) and "noutbuk" not in html.lower():
                raise RuntimeError("Citilink search blocked by challenge")
            links.extend(extract_product_links(html))
        uniq_links: list[str] = []
        seen_links: set[str] = set()
        for link in links:
            if link in seen_links:
                continue
            seen_links.add(link)
            uniq_links.append(link)
        for link in uniq_links[:max_products]:
            try:
                page.goto(link, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                html = page.content()
                product = parse_citilink_product_html(html, link)
                if (
                    product is None
                    or product.external_id in seen
                    or product.price is None
                    or product.price < 10_000
                ):
                    continue
                seen.add(product.external_id)
                products.append(product)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Citilink product failed %s: %s", link, type(exc).__name__
                )
                continue
        browser.close()
    return products
