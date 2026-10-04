from __future__ import annotations

"""KNS (kns.ru, Moscow) HTML catalog parser — public price (not club)."""

import logging
import re
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from models import Product
from stores.common import (
    clean_text,
    extract_specs_from_name,
    looks_like_challenge_page,
    matches_target_gpu,
    parse_price,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.kns.ru"
CATALOG_URLS = (
    f"{BASE_URL}/catalog/noutbuki/_videokarta_nvidia-geforce-rtx-5070ti/",
    f"{BASE_URL}/catalog/noutbuki/_videokarta_nvidia-geforce-rtx-5080/",
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": f"{BASE_URL}/",
    }


def _parse_goods_list(html: str) -> dict[str, dict[str, str | None]]:
    """Parse window.goodsList.push({...}) analytics blocks."""
    out: dict[str, dict[str, str | None]] = {}
    for block in re.findall(r"window\.goodsList\.push\(\{(.*?)\}\);", html, re.S):

        def field(name: str) -> str | None:
            m = re.search(rf"{name}\s*:\s*'([^']*)'", block)
            return m.group(1) if m else None

        item_id = field("item_id")
        if not item_id:
            continue
        out[item_id] = {
            "item_id": item_id,
            "item_name": field("item_name"),
            "price": field("price"),
            "item_brand": field("item_brand"),
            "item_variant": field("item_variant"),
        }
    return out


def _card_member_price(text: str) -> int | None:
    m = re.search(r"([\d\s\u00a0]{4,})\s*руб\.?\s*Клубная", text, re.I)
    if not m:
        return None
    return parse_price(m.group(1))


def _availability_from_text(text: str) -> tuple[bool, str]:
    low = text.lower()
    if "нет в наличии" in low or "под заказ" in low and "в наличии" not in low:
        if "под заказ" in low:
            return False, "preorder"
        return False, "out_of_stock"
    if "в наличии" in low:
        return True, "in_stock"
    return False, "unknown"


def catalog_gpu_from_url(url: str) -> str | None:
    """GPU implied by KNS filtered catalog URL (explicit catalog metadata)."""
    low = url.lower()
    if "rtx-5070ti" in low or "rtx-5070-ti" in low or "5070ti" in low:
        return "RTX 5070 Ti"
    if "rtx-5080" in low or "5080" in low:
        return "RTX 5080"
    return None


def parse_kns_catalog_html(
    html: str,
    *,
    now: datetime | None = None,
    catalog_gpu: str | None = None,
) -> list[Product]:
    if looks_like_challenge_page(html):
        raise RuntimeError("KNS challenge/error page detected")
    now = now or datetime.now(timezone.utc)
    goods = _parse_goods_list(html)
    soup = BeautifulSoup(html, "lxml")
    products: list[Product] = []
    seen: set[str] = set()

    for a in soup.select("a[href^='/product/']"):
        href = a.get("href") or ""
        name = clean_text(a.get_text(" ", strip=True))
        if not href or len(name) < 12:
            continue
        # Climb to card containing code + price.
        card = a
        card_text = ""
        for _ in range(12):
            card = card.parent
            if card is None:
                break
            card_text = card.get_text(" ", strip=True)
            if "Код:" in card_text and "руб" in card_text:
                break
        code_m = re.search(r"Код:\s*(\d+)", card_text)
        external_id = code_m.group(1) if code_m else None
        g = goods.get(external_id or "")
        if external_id is None:
            # Try match by name in goodsList
            for gid, meta in goods.items():
                if meta.get("item_name") and meta["item_name"] == name:
                    external_id = gid
                    g = meta
                    break
        if not external_id or external_id in seen:
            continue
        if not matches_target_gpu(
            name, (g or {}).get("item_name"), card_text, catalog_gpu
        ):
            # GPU-filtered catalog pages already scope the listing.
            if catalog_gpu is None:
                continue
        price = parse_price((g or {}).get("price"))
        if price is None:
            # First large price before "руб" that isn't club
            prices = [
                parse_price(x)
                for x in re.findall(r"([\d\s\u00a0]{4,})\s*руб", card_text)
            ]
            prices = [p for p in prices if p and p > 50_000]
            price = prices[0] if prices else None
        if price is None:
            continue
        available, avail_status = _availability_from_text(card_text)
        sku = (g or {}).get("item_variant") or None
        member = _card_member_price(card_text)
        url = urljoin(BASE_URL, href)
        specs = extract_specs_from_name(name)
        if catalog_gpu and not specs.get("gpu"):
            specs["gpu"] = catalog_gpu
        meta: dict = {"availability_status": avail_status, **specs}
        if catalog_gpu:
            meta["catalog_gpu"] = catalog_gpu
        if member is not None and member != price:
            meta["member_price"] = int(member)
        products.append(
            Product(
                store="kns",
                external_id=str(external_id),
                url=url,
                name=name,
                sku=sku,
                price=int(price),
                available=available,
                checked_at=now,
                metadata=meta,
            )
        )
        seen.add(external_id)

    # Fallback: goodsList-only if cards were thin.
    # Catalog URLs are already GPU-filtered; accept goods without GPU in title.
    if not products:
        for gid, meta in goods.items():
            name = clean_text(meta.get("item_name") or "")
            if not name:
                continue
            if (
                not matches_target_gpu(name, catalog_gpu)
                and "videokarta_nvidia-geforce-rtx-5070" not in html
                and "videokarta_nvidia-geforce-rtx-5080" not in html
            ):
                continue
            price = parse_price(meta.get("price"))
            if price is None:
                continue
            specs = extract_specs_from_name(name)
            if catalog_gpu and not specs.get("gpu"):
                specs["gpu"] = catalog_gpu
            product_meta: dict = {**specs}
            if catalog_gpu:
                product_meta["catalog_gpu"] = catalog_gpu
            products.append(
                Product(
                    store="kns",
                    external_id=str(gid),
                    url=f"{BASE_URL}/search/?q={gid}",
                    name=name,
                    sku=meta.get("item_variant"),
                    price=int(price),
                    available=True,
                    checked_at=now,
                    metadata=product_meta,
                )
            )
    return products


def fetch_target_laptops(
    *,
    client: httpx.Client | None = None,
    html_pages: Sequence[str] | None = None,
) -> list[Product]:
    """
    Collect target GPU laptops from KNS Moscow catalog pages.

    Product.price = public catalog price (not club/member).
    """
    if html_pages is not None:
        out: list[Product] = []
        seen: set[str] = set()
        for html in html_pages:
            gpu = None
            # Prefer GPU from page markers when fixture HTML includes catalog URLs.
            if "5070ti" in html.lower() or "5070-ti" in html.lower():
                gpu = "RTX 5070 Ti"
            elif "5080" in html.lower() and "5070" not in html.lower():
                gpu = "RTX 5080"
            for p in parse_kns_catalog_html(html, catalog_gpu=gpu):
                if p.external_id in seen:
                    continue
                seen.add(p.external_id)
                out.append(p)
        return out

    owns = client is None
    client = client or httpx.Client(headers=_headers(), follow_redirects=True, timeout=30.0)
    try:
        out: list[Product] = []
        seen: set[str] = set()
        for url in CATALOG_URLS:
            resp = client.get(url)
            if resp.status_code >= 400:
                raise RuntimeError(f"KNS HTTP {resp.status_code} for {url}")
            if looks_like_challenge_page(resp.text):
                raise RuntimeError("KNS challenge page")
            gpu = catalog_gpu_from_url(url)
            for p in parse_kns_catalog_html(resp.text, catalog_gpu=gpu):
                if p.external_id in seen:
                    continue
                seen.add(p.external_id)
                out.append(p)
        return out
    finally:
        if owns:
            client.close()
