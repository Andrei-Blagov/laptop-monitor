import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from models import Product


BASE_URL = "https://www.regard.ru"
API_GOODS_LIST = f"{BASE_URL}/api/site/goods/list"
API_GOODS_ITEM = f"{BASE_URL}/api/site/goods/{{product_id}}"

# Актуальный ID категории «Ноутбуки» (seo_url=noutbuki) из /api/site/categories/tree
LAPTOP_CATEGORY_ID = 1127

# Целевые GPU для мониторинга
TARGET_GPU_QUERIES = (
    "RTX 5070 Ti",
    "RTX 5080",
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


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Content-Type": "application/json",
    }


def _client() -> httpx.Client:
    return httpx.Client(
        headers=_default_headers(),
        follow_redirects=True,
        timeout=30.0,
    )


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
    except OSError:
        # Диагностика не должна ломать сбор.
        pass


def product_url(product_id: int | str, seo_url: str | None = None) -> str:
    if seo_url:
        return f"{BASE_URL}/product/{product_id}/{seo_url}"
    return f"{BASE_URL}/product/{product_id}"


def extract_product_id(url: str) -> int | None:
    match = re.search(r"/product/(\d+)", url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def matches_target_gpu(*parts: str | None) -> bool:
    text = " ".join(p for p in parts if p)
    if not text:
        return False
    return any(pattern.search(text) for pattern in TARGET_GPU_PATTERNS)


def is_available(item: dict) -> bool:
    show_flag = item.get("show_flag")
    preorder = item.get("preorder")
    price = parse_price(item.get("price"))

    visible = show_flag in (1, True, "1")
    not_preorder_only = preorder in (0, False, "0", None)
    return bool(visible and not_preorder_only and price is not None and price > 0)


def item_to_product(item: dict) -> Product | None:
    try:
        product_id = item.get("id")
        if product_id is None:
            return None

        name = clean_text(
            str(item.get("full_title") or item.get("title") or "")
        )
        if not name:
            return None

        sku_raw = item.get("vendorcode")
        sku = clean_text(str(sku_raw)) if sku_raw else None
        if sku == "":
            sku = None

        return Product(
            store="regard",
            external_id=str(product_id),
            url=product_url(product_id, item.get("seo_url")),
            name=name,
            sku=sku,
            price=parse_price(item.get("price")),
            available=is_available(item),
            checked_at=datetime.now(timezone.utc),
        )
    except Exception:
        return None


def fetch_page(url: str) -> str | None:
    """Сохранён для совместимости: HTML-каталог Regard сейчас часто 404."""
    headers = _default_headers()
    headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    headers.pop("Content-Type", None)

    try:
        with httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            response = client.get(url)
    except httpx.HTTPError:
        return None

    print(f"[fetch_page] status={response.status_code} final={response.url}")

    if response.status_code == 404:
        return None

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        return None

    return response.text


def fetch_goods_list(
    *,
    category_id: int | None = LAPTOP_CATEGORY_ID,
    search: str | None = None,
    start: int = 0,
    length: int = 48,
    client: httpx.Client | None = None,
) -> dict:
    scope: dict = {}
    if category_id is not None:
        scope["byCategory"] = category_id
    if search:
        scope["search"] = search

    body = {
        "start": start,
        "length": length,
        "scope": scope,
    }

    owns_client = client is None
    if owns_client:
        client = _client()

    try:
        response = client.post(API_GOODS_LIST, json=body)
        print(
            f"[goods/list] status={response.status_code} "
            f"final={response.url} search={search!r} start={start}"
        )
        if response.status_code == 404:
            return {"data": [], "recordsFiltered": 0}
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return {"data": [], "recordsFiltered": 0}
        return data
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        print(f"[goods/list] error: {type(exc).__name__}: {exc}")
        return {"data": [], "recordsFiltered": 0}
    finally:
        if owns_client and client is not None:
            client.close()


def iter_goods_list(
    *,
    category_id: int | None = LAPTOP_CATEGORY_ID,
    search: str | None = None,
    page_size: int = 48,
    max_items: int | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    owns_client = client is None
    if owns_client:
        client = _client()

    items: list[dict] = []
    start = 0

    try:
        while True:
            payload = fetch_goods_list(
                category_id=category_id,
                search=search,
                start=start,
                length=page_size,
                client=client,
            )
            batch = payload.get("data") or []
            if not isinstance(batch, list) or not batch:
                break

            for raw in batch:
                if isinstance(raw, dict):
                    items.append(raw)
                    if max_items is not None and len(items) >= max_items:
                        return items

            total = payload.get("recordsFiltered")
            start += len(batch)
            if total is not None and start >= int(total):
                break
            if len(batch) < page_size:
                break
    finally:
        if owns_client and client is not None:
            client.close()

    return items


def fetch_laptops(
    *,
    search: str | None = None,
    max_items: int | None = None,
) -> list[Product]:
    """Список ноутбуков Regard через JSON API /api/site/goods/list."""
    raw_items = iter_goods_list(search=search, max_items=max_items)
    _save_debug(
        "regard_laptops_raw.json",
        {
            "search": search,
            "count": len(raw_items),
            "items": raw_items[:100],
        },
    )

    products: list[Product] = []
    for raw in raw_items:
        product = item_to_product(raw)
        if product is not None:
            products.append(product)
    return products


def fetch_target_laptops() -> list[Product]:
    """
    Ноутбуки с RTX 5070 Ti Laptop / RTX 5080 Laptop.

    Берём выдачу API по поисковым запросам внутри категории ноутбуков,
    затем дополнительно фильтруем по brief/title.
    """
    owns_client = True
    client = _client()
    by_id: dict[int, dict] = {}

    try:
        for query in TARGET_GPU_QUERIES:
            raw_items = iter_goods_list(search=query, client=client)
            for raw in raw_items:
                product_id = raw.get("id")
                if product_id is None:
                    continue
                try:
                    by_id[int(product_id)] = raw
                except (TypeError, ValueError):
                    continue
    finally:
        client.close()

    filtered_raw: list[dict] = []
    products: list[Product] = []

    for raw in by_id.values():
        if not matches_target_gpu(
            raw.get("full_title"),
            raw.get("title"),
            raw.get("brief"),
        ):
            continue
        filtered_raw.append(raw)
        product = item_to_product(raw)
        if product is not None:
            products.append(product)

    products.extend(
        fetch_missing_priority_products({p.external_id for p in products})
    )
    products.sort(key=lambda p: (p.price is None, p.price or 0, p.name))
    _save_debug(
        "regard_target_gpus.json",
        {
            "queries": list(TARGET_GPU_QUERIES),
            "count": len(products),
            "items": filtered_raw,
        },
    )
    return products


def structured_gpu_text(item: dict) -> str | None:
    """GPU model from the product card characteristics (not the title)."""
    for row in item.get("short_characteristics") or []:
        if isinstance(row, dict) and str(row.get("name") or "") == "Графический чипсет":
            return str(row.get("value") or "") or None
    for block in item.get("characteristics") or []:
        if not isinstance(block, dict) or str(block.get("title") or "") != "Графика":
            continue
        for row in block.get("data") or []:
            if isinstance(row, dict) and str(row.get("name") or "") == "Модель":
                return str(row.get("value") or "") or None
    return None


def priority_regard_ids() -> list[str]:
    import config

    ids: list[str] = []
    for model in getattr(config, "PRIORITY_MODELS", ()) or ():
        value = (model.get("store_ids") or {}).get("regard")
        if value and str(value) not in ids:
            ids.append(str(value))
    return ids


def fetch_missing_priority_products(
    collected_ids: set[str],
    *,
    client: httpx.Client | None = None,
) -> list[Product]:
    """
    One product card request per watched Regard id that the GPU searches missed.

    A hidden card (show_flag=0) is still returned as unavailable so its price
    history continues. A card without a target GPU is ignored.
    """
    missing = [pid for pid in priority_regard_ids() if pid not in collected_ids]
    if not missing:
        return []
    owns_client = client is None
    if owns_client:
        client = _client()
    out: list[Product] = []
    try:
        for pid in missing:
            try:
                response = client.get(API_GOODS_ITEM.format(product_id=pid))
            except httpx.HTTPError as exc:
                print(f"[priority goods/{pid}] error: {type(exc).__name__}")
                continue
            print(f"[priority goods/{pid}] status={response.status_code}")
            if response.status_code != 200:
                continue
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError):
                continue
            item = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
            if not isinstance(item, dict):
                continue
            if not matches_target_gpu(
                item.get("full_title"),
                item.get("title"),
                item.get("brief"),
                structured_gpu_text(item),
            ):
                continue
            product = item_to_product(item)
            if product is not None:
                out.append(product)
    finally:
        if owns_client and client is not None:
            client.close()
    return out


def fetch_product(url: str) -> Product | None:
    """
    Карточка товара.

    Сначала пробуем JSON API по ID из URL.
    HTML-страницы Regard (/product/..., /catalog/...) сейчас часто отдают 404
    из‑за проблем SSR на стороне магазина, поэтому HTML — только fallback.
    """
    product_id = extract_product_id(url)

    if product_id is not None:
        try:
            with _client() as client:
                response = client.get(API_GOODS_ITEM.format(product_id=product_id))
            print(
                f"[goods/{product_id}] status={response.status_code} "
                f"final={response.url}"
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            _save_debug(f"regard_product_{product_id}.json", data)
            if isinstance(data, dict):
                return item_to_product(data)
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            print(f"[goods/{product_id}] error: {type(exc).__name__}: {exc}")

    # HTML fallback (может вернуть None при 404)
    html = fetch_page(url)
    if html is None:
        return None

    _save_debug("regard.html", html)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        h1 = soup.find("h1")
        if not h1:
            return None

        name = clean_text(h1.get_text(" ", strip=True))
        page_text = clean_text(soup.get_text(" ", strip=True))

        sku_match = re.search(
            r"Код производителя\s+([A-Za-z0-9._-]+)",
            page_text,
            flags=re.IGNORECASE,
        )
        sku = sku_match.group(1) if sku_match else None

        price_match = re.search(
            r"Полные характеристики.{0,300}?([\d\s\u00a0]+)\s*₽",
            page_text,
            flags=re.IGNORECASE,
        )
        price = parse_price(price_match.group(1)) if price_match else None

        available = bool(
            re.search(r"\bВ наличии\b", page_text, flags=re.IGNORECASE)
        )

        external_id = str(product_id) if product_id is not None else None
        if not external_id:
            return None

        return Product(
            store="regard",
            external_id=external_id,
            url=url,
            name=name,
            sku=sku,
            price=price,
            available=available,
            checked_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        print(f"[fetch_product html] error: {type(exc).__name__}: {exc}")
        return None
