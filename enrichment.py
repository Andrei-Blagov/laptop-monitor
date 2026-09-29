from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from product_identity import (
    ProductIdentity,
    normalize_brand,
    normalize_cpu,
    normalize_gpu,
    normalize_model,
    normalize_os,
    normalize_ram_gb,
    normalize_refresh_hz,
    normalize_resolution,
    normalize_screen_size_inch,
    normalize_ssd_gb,
    normalize_vram_gb,
)

logger = logging.getLogger(__name__)

REGARD_API = "https://www.regard.ru/api/site/goods/{product_id}"


def _headers(json_mode: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    if json_mode:
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.regard.ru",
                "Referer": "https://www.regard.ru/",
            }
        )
    else:
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )
    return headers


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _block_map(raw: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for block in raw.get("characteristics") or []:
        title = str(block.get("title") or "")
        vals: dict[str, str] = {}
        for item in block.get("data") or []:
            name = item.get("name")
            value = item.get("value")
            if name and value is not None:
                vals[str(name)] = str(value)
        if title:
            result[title] = vals
    return result


def _short_map(raw: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.get("short_characteristics") or []:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            result[str(name)] = str(value)
    return result


def enrich_regard_product(
    item: dict[str, Any],
    client: httpx.Client | None = None,
) -> ProductIdentity:
    owns = client is None
    if owns:
        client = httpx.Client(
            headers=_headers(json_mode=True),
            follow_redirects=True,
            timeout=30.0,
        )

    product_id = item["external_id"]
    raw: dict[str, Any] = {}
    try:
        response = client.get(REGARD_API.format(product_id=product_id))
        print(f"[enrich regard] status={response.status_code} id={product_id}")
        if response.status_code == 200:
            raw = response.json()
        else:
            logger.warning(
                "Regard enrich HTTP %s for %s", response.status_code, product_id
            )
    except httpx.HTTPError as exc:
        logger.error("Regard enrich error %s: %s", product_id, exc)
    finally:
        if owns and client is not None:
            client.close()

    blocks = _block_map(raw)
    short = _short_map(raw)
    main = blocks.get("Основные", {})
    display = blocks.get("Дисплей", {})
    cpu_block = blocks.get("Процессор", {})
    ram_block = blocks.get("Оперативная память", {})
    storage = blocks.get("Накопители данных", {})
    graphics = blocks.get("Графика", {})
    extra = blocks.get("Дополнительно", {})

    vendorcode = raw.get("vendorcode") or main.get("Код производителя") or item.get("sku")
    alt = raw.get("alternative_pn") or []
    if isinstance(alt, str):
        alt = [alt]
    elif not isinstance(alt, list):
        alt = []

    brand = normalize_brand(main.get("Производитель") or raw.get("vendor"))
    cpu_raw = short.get("Процессор") or " ".join(
        x
        for x in [
            cpu_block.get("Производитель"),
            cpu_block.get("Линейка"),
            cpu_block.get("Модель"),
        ]
        if x
    )
    gpu_raw = short.get("Графический чипсет") or graphics.get("Модель")
    vram = graphics.get("Объём видеопамяти")
    ram_raw = ram_block.get("Объём установленной памяти") or short.get(
        "Оперативная память"
    )
    ssd_raw = storage.get("Объём установленного SSD") or short.get(
        "Объём установленного SSD"
    )

    flat_raw = {
        f"{block}/{key}": value
        for block, vals in blocks.items()
        for key, value in vals.items()
    }
    flat_raw.update({f"short/{k}": v for k, v in short.items()})

    return ProductIdentity(
        store="regard",
        external_id=str(product_id),
        sku=item.get("sku"),
        name=item.get("name") or raw.get("full_title") or raw.get("title") or "",
        price=item.get("price"),
        available=bool(item.get("available")),
        url=item.get("url"),
        brand=brand,
        model_family=None,
        model=normalize_model(str(vendorcode) if vendorcode else None),
        manufacturer_part_number=str(vendorcode) if vendorcode else None,
        alternative_part_numbers=[str(x) for x in alt if x],
        cpu=normalize_cpu(cpu_raw),
        gpu=normalize_gpu(gpu_raw),
        gpu_vram_gb=normalize_vram_gb(vram),
        ram_gb=normalize_ram_gb(ram_raw),
        ram_type=_clean(ram_block.get("Тип памяти")),
        ssd_gb=normalize_ssd_gb(ssd_raw),
        screen_size_inch=normalize_screen_size_inch(display.get("Диагональ")),
        screen_resolution=normalize_resolution(display.get("Разрешение")),
        screen_refresh_hz=normalize_refresh_hz(display.get("Частота обновления")),
        os=normalize_os(main.get("Операционная система") or short.get("Операционная система")),
        color=_clean(extra.get("Цвет") or short.get("Цвет")),
        ean=None,
        gtin=None,
        raw_characteristics=flat_raw,
        source_identifiers={
            "vendorcode": vendorcode,
            "alternative_pn": alt,
            "manufacturer_code_field": "Код производителя / vendorcode",
            "note": "alternative_pn — дополнительные коды производителя из API Regard",
        },
    )


def _parse_andpro_props(soup: BeautifulSoup) -> dict[str, str]:
    props: dict[str, str] = {}
    for tr in soup.select("tr.itempage-info__row, .itempage-info tr"):
        th = tr.select_one("th")
        td = tr.select_one("td")
        if th and td:
            props[th.get_text(" ", strip=True)] = td.get_text(" ", strip=True)
    for li in soup.select(".itempage-tldr__item"):
        text = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
        for label in (
            "Производитель",
            "Модель",
            "Цвет",
            "Диагональ экрана",
            "Тип ноутбука/планшет",
        ):
            if text.startswith(label):
                props.setdefault(label, text[len(label) :].strip())
    return props


def _parse_andpro_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
    return {}


def enrich_andpro_product(
    item: dict[str, Any],
    client: httpx.Client | None = None,
) -> ProductIdentity:
    owns = client is None
    if owns:
        client = httpx.Client(
            headers=_headers(json_mode=False),
            follow_redirects=True,
            timeout=40.0,
        )

    html = ""
    url = item.get("url") or ""
    try:
        response = client.get(url)
        print(f"[enrich andpro] status={response.status_code} id={item.get('external_id')}")
        if response.status_code == 200:
            html = response.text
        else:
            logger.warning("ANDPRO enrich HTTP %s for %s", response.status_code, url)
    except httpx.HTTPError as exc:
        logger.error("ANDPRO enrich error %s: %s", url, exc)
    finally:
        if owns and client is not None:
            client.close()

    soup = BeautifulSoup(html or "<html></html>", "lxml")
    props = _parse_andpro_props(soup)
    ld = _parse_andpro_ld(soup)

    for prop in ld.get("additionalProperty") or []:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        value = prop.get("value")
        if name and value and name not in props:
            props[str(name)] = str(value).replace("&quot;", '"')

    brand_raw = props.get("Производитель")
    if not brand_raw:
        brand_obj = ld.get("brand")
        if isinstance(brand_obj, dict):
            brand_raw = brand_obj.get("name")
        elif isinstance(brand_obj, str):
            brand_raw = brand_obj

    mpn = ld.get("mpn") or item.get("sku")
    gtin = ld.get("gtin13") or ld.get("gtin") or ld.get("gtin12") or ld.get("gtin8")
    cpu_raw = " ".join(
        x
        for x in [
            props.get("Производитель процессора"),
            props.get("Тип процессора"),
            props.get("Индекс процессора"),
        ]
        if x
    ) or None

    return ProductIdentity(
        store="andpro",
        external_id=str(item.get("external_id")),
        sku=item.get("sku"),
        name=item.get("name") or ld.get("name") or "",
        price=item.get("price"),
        available=bool(item.get("available")),
        url=url,
        brand=normalize_brand(brand_raw),
        model_family=None,
        model=normalize_model(props.get("Модель") or ld.get("model")),
        manufacturer_part_number=str(mpn) if mpn else None,
        alternative_part_numbers=[],
        cpu=normalize_cpu(cpu_raw),
        gpu=normalize_gpu(props.get("Видеочипсет")),
        gpu_vram_gb=normalize_vram_gb(props.get("Видеопамять")),
        ram_gb=normalize_ram_gb(props.get("Установлено памяти")),
        ram_type=None,
        ssd_gb=normalize_ssd_gb(props.get("Объём диска")),
        screen_size_inch=normalize_screen_size_inch(props.get("Диагональ экрана")),
        screen_resolution=normalize_resolution(props.get("Разрешение экрана")),
        screen_refresh_hz=None,
        os=normalize_os(props.get("Установленная ОС")),
        color=_clean(props.get("Цвет")),
        ean=str(gtin) if gtin else None,
        gtin=str(gtin) if gtin else None,
        raw_characteristics=props,
        source_identifiers={
            "mpn": mpn,
            "ld_sku": ld.get("sku"),
            "article_field": "JSON-LD mpn / Артикул на карточке",
            "model_field": props.get("Модель") or ld.get("model"),
            "note": "model часто содержит manufacturer model code (например G614PR-RV027)",
        },
    )


def enrich_unmatched(items: list[dict[str, Any]]) -> list[ProductIdentity]:
    enriched: list[ProductIdentity] = []
    regard_client = httpx.Client(headers=_headers(True), follow_redirects=True, timeout=30.0)
    andpro_client = httpx.Client(headers=_headers(False), follow_redirects=True, timeout=40.0)
    try:
        for item in items:
            store = item.get("store")
            try:
                if store == "regard":
                    enriched.append(enrich_regard_product(item, client=regard_client))
                elif store == "andpro":
                    enriched.append(enrich_andpro_product(item, client=andpro_client))
                else:
                    logger.warning("Unknown store in unmatched: %s", store)
            except Exception:
                logger.exception(
                    "Failed to enrich %s/%s", store, item.get("external_id")
                )
    finally:
        regard_client.close()
        andpro_client.close()
    return enriched
