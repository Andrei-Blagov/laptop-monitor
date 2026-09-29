import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from models import Product


logger = logging.getLogger(__name__)

BASE_URL = "https://andpro.ru"
NOTEBOOKS_PATH = "/catalog/notebooks_accessories/notebooks/"

# Bitrix smart-filter по свойству videochipset (мобильные GPU).
TARGET_FILTER_URL = (
    f"{BASE_URL}{NOTEBOOKS_PATH}"
    "filter/videochipset-is-rtx-5070-ti-mobile-or-rtx-5080-mobile/apply/"
)

TARGET_GPU_PATTERNS = (
    re.compile(r"RTX\s*5070\s*Ti", re.IGNORECASE),
    re.compile(r"RTX\s*5080(?!\s*Ti)", re.IGNORECASE),
)

DATA_DIR = Path("data")


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_price(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def matches_target_gpu(*parts: str | None) -> bool:
    text = " ".join(p for p in parts if p)
    if not text:
        return False
    return any(pattern.search(text) for pattern in TARGET_GPU_PATTERNS)


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": f"{BASE_URL}/",
    }


def _save_debug(filename: str, payload: object) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / filename
        if isinstance(payload, (dict, list)):
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            path.write_text(str(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("Не удалось сохранить %s: %s", filename, exc)


def _client() -> httpx.Client:
    return httpx.Client(
        headers=_default_headers(),
        follow_redirects=True,
        timeout=30.0,
    )


def fetch_html(url: str, client: httpx.Client | None = None) -> str | None:
    owns_client = client is None
    if owns_client:
        client = _client()

    try:
        response = client.get(url)
        print(f"[andpro] status={response.status_code} final={response.url}")
        if response.status_code in {403, 404, 429}:
            logger.warning(
                "ANDPRO HTTP %s for %s", response.status_code, response.url
            )
            return None
        response.raise_for_status()
        return response.text
    except httpx.TimeoutException as exc:
        logger.error("ANDPRO timeout for %s: %s", url, exc)
        return None
    except httpx.HTTPError as exc:
        logger.error("ANDPRO HTTP error for %s: %s", url, exc)
        return None
    finally:
        if owns_client and client is not None:
            client.close()


def extract_sku(card) -> str | None:
    for span in card.select(".catalog-card__art"):
        text = clean_text(span.get_text(" ", strip=True))
        # Не путать manufacturer SKU с кодом склада.
        if text.lower().startswith("код склада"):
            continue
        if text.lower().startswith("артикул"):
            sku = text.split(":", 1)[-1].strip()
            return sku or None
    return None


def extract_availability(card) -> bool:
    stock = card.select_one(".card-stock")
    if stock is None:
        return False

    classes = set(stock.get("class") or [])
    title = (stock.get("data-title") or "").strip().lower()
    text = clean_text(stock.get_text(" ", strip=True)).lower()

    if "продано" in text or "нет в наличии" in text:
        return False
    if "--yes" in classes or title == "в наличии" or "в наличии" in text:
        return True
    return False


def parse_catalog_card(card) -> Product | None:
    try:
        external_id = card.get("data-id")
        if not external_id:
            return None

        name = clean_text(
            str(card.get("data-name") or "")
        ) or clean_text(
            card.select_one(".catalog-card__title").get_text(" ", strip=True)
            if card.select_one(".catalog-card__title")
            else ""
        )
        if not name:
            return None

        href_el = card.select_one("a.catalog-card__title[href]") or card.select_one(
            "a.catalog-card__link-photo[href]"
        )
        if href_el is None or not href_el.get("href"):
            return None
        url = urljoin(BASE_URL, href_el["href"])

        # Категорийный фильтр уже ноутбуки, но дополнительно отсекаем
        # настольные видеокарты и чужие разделы каталога.
        if "/notebooks_accessories/notebooks/" not in url:
            return None
        if not re.search(r"ноутбук", name, flags=re.IGNORECASE):
            return None

        desc_el = card.select_one(".catalog-card__desc")
        desc = clean_text(desc_el.get_text(" ", strip=True)) if desc_el else ""

        if not matches_target_gpu(name, desc):
            return None

        price = parse_price(card.get("data-price"))
        if price is None:
            price_el = card.select_one(".catalog-card__price")
            if price_el is not None:
                price = parse_price(price_el.get_text(" ", strip=True))

        return Product(
            store="andpro",
            external_id=str(external_id),
            url=url,
            name=name,
            sku=extract_sku(card),
            price=price,
            available=extract_availability(card),
            checked_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.exception("Ошибка разбора карточки ANDPRO: %s", exc)
        return None


def parse_catalog_html(html: str) -> list[Product]:
    soup = BeautifulSoup(html, "lxml")
    products: list[Product] = []
    seen: set[str] = set()

    for card in soup.select("div.catalog-card[data-id]"):
        product = parse_catalog_card(card)
        if product is None:
            continue
        if product.external_id in seen:
            continue
        seen.add(product.external_id)
        products.append(product)

    return products


def _listing_urls() -> list[str]:
    """Первая страница + запас по PAGEN на случай роста выдачи."""
    urls = [TARGET_FILTER_URL]
    for page in range(2, 6):
        urls.append(f"{TARGET_FILTER_URL}?PAGEN_1={page}")
    return urls


def fetch_target_laptops() -> list[Product]:
    """
    Ноутбуки RTX 5070 Ti Mobile / RTX 5080 Mobile с ANDPRO.

    Источник: HTML-каталог Bitrix smart-filter (videochipset).
    Цена берётся из data-price карточки каталога — это публичная цена,
    отображаемая на сайте.
    """
    by_id: dict[str, Product] = {}
    raw_debug: list[dict] = []

    with _client() as client:
        empty_pages = 0
        for url in _listing_urls():
            html = fetch_html(url, client=client)
            if html is None:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue

            products = parse_catalog_html(html)
            if not products:
                empty_pages += 1
                if empty_pages >= 2 and by_id:
                    break
                continue

            empty_pages = 0
            for product in products:
                by_id[product.external_id] = product
                raw_debug.append(
                    {
                        "external_id": product.external_id,
                        "sku": product.sku,
                        "name": product.name,
                        "price": product.price,
                        "available": product.available,
                        "url": product.url,
                    }
                )

            # Если на странице меньше типичного размера выдачи — дальше пусто.
            if len(products) < 20 and "?PAGEN_" in url:
                break
            if "?PAGEN_" not in url and len(products) < 40:
                # Обычно все целевые GPU помещаются на 1 страницу.
                break

    result = sorted(
        by_id.values(),
        key=lambda p: (p.price is None, p.price or 0, p.name),
    )
    _save_debug(
        "andpro_target_gpus.json",
        {
            "source": TARGET_FILTER_URL,
            "count": len(result),
            "items": raw_debug,
        },
    )
    return result
