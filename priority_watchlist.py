from __future__ import annotations

"""Watched models: status, price, and history shown apart from the ranking."""

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import config
from comparison import ProductMatch, match_products
from deal_ranking import cluster_specs, rank_clusters
from identity_sync import identity_from_cache, load_specs_cache
from laptop_eligibility import LOW_RANK, NOT_DISCOVERED, PRICE_OVER_CAP
from product_identity import normalize_identifier
from store_freshness import get_fresh_store_slugs
from storage import (
    DEFAULT_DB_PATH,
    get_all_identifiers_readonly,
    get_all_products_readonly,
    get_latest_store_runs_readonly,
    get_price_history_for_products_readonly,
)

IN_TOP = "IN_TOP"


@dataclass
class PriorityStatus:
    key: str
    brand: str
    model: str
    url: str | None
    status: str
    details: list[str] = field(default_factory=list)
    rank: int | None = None
    best_store: str | None = None
    best_price: int | None = None
    available: bool = False
    offers: list[dict[str, Any]] = field(default_factory=list)
    history_min: int | None = None
    history_min_at: str | None = None
    history_started_at: str | None = None
    previous_price: int | None = None
    price_changed_at: str | None = None
    specs: dict[str, Any] = field(default_factory=dict)
    duplicate_clusters: int = 0


def _model_codes(model: dict[str, Any]) -> set[str]:
    codes = {normalize_identifier(str(x)) for x in model.get("identifiers") or () if x}
    return {c for c in codes if c}


def _product_matches(
    product: dict[str, Any],
    model: dict[str, Any],
    codes: set[str],
    identifier_values: dict[int, set[str]],
) -> bool:
    store_ids = {str(k): str(v) for k, v in (model.get("store_ids") or {}).items()}
    if store_ids.get(str(product.get("store"))) == str(product.get("external_id")):
        return True
    sku = normalize_identifier(product.get("sku"))
    if sku and sku in codes:
        return True
    if identifier_values.get(int(product["id"]), set()) & codes:
        return True
    name = normalize_identifier(product.get("name")) or ""
    return any(len(code) >= 8 and "-" in code and code in name for code in codes)


def _history_summary(
    rows: Sequence[dict[str, Any]],
    best_product_id: int | None,
    all_rows: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "history_min": None,
        "history_min_at": None,
        "history_started_at": None,
        "previous_price": None,
        "price_changed_at": None,
    }
    priced = [r for r in rows if r.get("price") is not None and int(r["price"]) > 0]
    if priced:
        low = min(priced, key=lambda r: (int(r["price"]), str(r.get("checked_at"))))
        out["history_min"] = int(low["price"])
        out["history_min_at"] = low.get("checked_at")
    stamps = sorted(str(r.get("checked_at")) for r in rows if r.get("checked_at"))
    if stamps:
        out["history_started_at"] = stamps[0]
    if best_product_id is not None:
        own = sorted(
            (r for r in all_rows.get(best_product_id, []) if r.get("price") is not None),
            key=lambda r: str(r.get("checked_at")),
        )
        if len(own) >= 2:
            current = int(own[-1]["price"])
            for row in reversed(own[:-1]):
                if int(row["price"]) != current:
                    out["previous_price"] = int(row["price"])
                    break
            out["price_changed_at"] = own[-1].get("checked_at")
    return out


def build_priority_statuses(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    specs_path: Path | str | None = None,
    models: Sequence[dict[str, Any]] | None = None,
    top_limit: int | None = None,
) -> list[PriorityStatus]:
    """Status of every watched model from the existing DB. No network calls."""
    from russian_deals import load_russian_ranked_deals

    watched = list(models if models is not None else getattr(config, "PRIORITY_MODELS", ()) or ())
    if not watched:
        return []
    limit = int(top_limit if top_limit is not None else config.TOP_DEALS_LIMIT)
    products = get_all_products_readonly(db_path)
    identifiers = get_all_identifiers_readonly(db_path)
    by_pid: dict[int, set[str]] = {}
    for row in identifiers:
        value = normalize_identifier(row.get("value"))
        if value:
            by_pid.setdefault(int(row["product_id"]), set()).add(value)
    comparison = match_products(products, identifiers)
    cache = load_specs_cache(specs_path) if specs_path is not None else load_specs_cache()
    specs_by_key: dict[tuple[str, str], Any] = {}
    for p in products:
        identity = identity_from_cache(cache, str(p["store"]), str(p["external_id"]))
        if identity is not None:
            specs_by_key[(str(p["store"]), str(p["external_id"]))] = identity
    fresh = get_fresh_store_slugs(get_latest_store_runs_readonly(db_path))
    eligible = load_russian_ranked_deals(db_path, limit=0, specs_path=specs_path)
    rank_by_name = {d.cluster_name: i for i, d in enumerate(eligible, start=1)}

    statuses: list[PriorityStatus] = []
    for model in watched:
        codes = _model_codes(model)
        hits = [p for p in products if _product_matches(p, model, codes, by_pid)]
        status = PriorityStatus(
            key=str(model.get("key") or model.get("model") or ""),
            brand=str(model.get("brand") or ""),
            model=str(model.get("model") or ""),
            url=model.get("url"),
            status=NOT_DISCOVERED,
        )
        if not hits:
            status.details = ["no_product_row"]
            statuses.append(status)
            continue
        hit_keys = {(str(p["store"]), str(p["external_id"])) for p in hits}
        clusters = [
            m
            for m in comparison.matches
            if any((o.store, str(o.external_id)) in hit_keys for o in m.offers)
        ]
        status.duplicate_clusters = max(0, len(clusters) - 1)
        single_store = not clusters
        if single_store:
            # Ranking only covers models matched in two or more stores, so a
            # watched model sold by one store is still tracked here.
            alone = [o for o in comparison.unmatched if (o.store, str(o.external_id)) in hit_keys]
            if not alone:
                status.details = ["no_cluster"]
                statuses.append(status)
                continue
            match = ProductMatch(
                normalized_sku=str(alone[0].sku or alone[0].external_id),
                display_sku=str(alone[0].sku or alone[0].external_id),
                name=alone[0].name,
                offers=alone,
                match_method="watchlist",
                matched_identifier=status.key,
            )
        else:
            match = max(clusters, key=lambda m: len(m.offers))
        status.offers = [
            {
                "store": o.store,
                "external_id": str(o.external_id),
                "price": o.price,
                "available": bool(o.available),
                "fresh": o.store in fresh,
                "url": o.url,
            }
            for o in sorted(match.offers, key=lambda o: (o.price is None, o.price or 0, o.store))
        ]
        specs = cluster_specs(match, specs_by_key)
        for o in match.offers:
            identity = specs_by_key.get((o.store, str(o.external_id)))
            if identity is None:
                continue
            for name in ("gpu_vram_gb", "screen_refresh_hz"):
                if specs.get(name) is None:
                    specs[name] = getattr(identity, name, None)
        status.specs = specs

        selling = [
            o for o in match.offers
            if o.available and o.price is not None and int(o.price) > 0 and o.store in fresh
        ]
        best = min(selling, key=lambda o: (int(o.price), o.store)) if selling else None
        status.available = best is not None
        status.best_store = best.store if best else None
        status.best_price = int(best.price) if best else None

        pids = [int(o.product_id) for o in match.offers if getattr(o, "product_id", None) is not None]
        histories = get_price_history_for_products_readonly(db_path, pids) if pids else {}
        rows = [r for pid in pids for r in histories.get(pid, [])]
        best_pid = int(best.product_id) if best is not None and getattr(best, "product_id", None) is not None else None
        for k, v in _history_summary(rows, best_pid, histories).items():
            setattr(status, k, v)

        exclusions: list[dict[str, Any]] = []
        kept = rank_clusters(
            [match],
            specs_by_key=specs_by_key,
            fresh_stores=fresh,
            hard_filters=True,
            exclusions=exclusions,
        )
        if kept:
            rank = None if single_store else rank_by_name.get(match.name)
            status.rank = rank
            status.status = IN_TOP if rank is not None and rank <= limit else LOW_RANK
            if single_store:
                status.details = ["single_store_not_ranked"]
        elif exclusions:
            status.status = str(exclusions[0].get("reason") or NOT_DISCOVERED)
            status.details = list(exclusions[0].get("details") or [])
            if status.status == PRICE_OVER_CAP and status.best_price is None:
                status.best_price = exclusions[0].get("price")
                status.best_store = exclusions[0].get("store")
        statuses.append(status)
    return statuses


_STORE_LABEL = {"kns": "KNS", "andpro": "ANDPRO", "regard": "Regard", "citilink": "Citilink"}


def _rub(value: int | None) -> str:
    return "н/д" if value is None else f"{int(value):,}".replace(",", " ")


def _specs_line(specs: dict[str, Any]) -> str:
    parts: list[str] = []
    gpu = specs.get("gpu")
    if gpu:
        label = str(gpu).replace(" LAPTOP", "").replace(" TI", " Ti")
        if specs.get("gpu_vram_gb"):
            label += f" {int(specs['gpu_vram_gb'])} GB"
        parts.append(label)
    if specs.get("cpu"):
        parts.append(str(specs["cpu"]))
    if specs.get("ram_gb"):
        parts.append(f"{int(specs['ram_gb'])} GB RAM")
    ssd = specs.get("ssd_gb")
    if ssd:
        parts.append(f"{int(ssd) // 1024} TB SSD" if int(ssd) >= 1000 and int(ssd) % 1024 == 0 else f"{int(ssd)} GB SSD")
    screen = []
    if specs.get("screen_inch"):
        screen.append(f'{float(specs["screen_inch"]):g}"')
    if specs.get("screen_resolution"):
        screen.append(str(specs["screen_resolution"]))
    if specs.get("screen_refresh_hz"):
        screen.append(f"{int(specs['screen_refresh_hz'])} Гц")
    if screen:
        parts.append(" ".join(screen))
    return " · ".join(parts)


def _detail_text(details: Sequence[str]) -> str:
    out: list[str] = []
    for item in details:
        if item.startswith("also:"):
            continue
        m = re.fullmatch(r"ram_(\d+)gb", item)
        if m:
            out.append(f"RAM {m.group(1)} GB, нужно от {int(config.MIN_INSTALLED_RAM_GB)} GB")
            continue
        m = re.fullmatch(r"resolution_(\d+)x(\d+)", item)
        if m:
            out.append(f"экран {m.group(1)}x{m.group(2)} ниже 2560x1440")
            continue
        if item == "ram_unknown":
            out.append("объём RAM не подтверждён")
        elif item == "resolution_unknown":
            out.append("разрешение экрана не подтверждено")
    return ", ".join(out)


def status_text(status: PriorityStatus) -> str:
    cap = config.format_price_cap_label()
    if status.status == IN_TOP:
        return f"в ТОП, №{status.rank}"
    if status.status == LOW_RANK:
        if "single_store_not_ranked" in status.details:
            return "подходит, но продаётся в одном магазине, а рейтинг сравнивает модели минимум из двух"
        where = f"№{status.rank} в рейтинге" if status.rank else "в рейтинге"
        return f"подходит, {where}, за пределами ТОП {int(config.TOP_DEALS_LIMIT)}"
    if status.status == PRICE_OVER_CAP:
        return f"не в ТОП: цена {_rub(status.best_price)} ₽ выше лимита {cap} ₽"
    if status.status == NOT_DISCOVERED:
        return "не найдена в магазинах"
    if status.status == "NOT_AVAILABLE":
        return "нет в наличии в проверенных магазинах"
    if status.status == "GPU_NOT_DETECTED":
        return "не в ТОП: видеокарта не подтверждена"
    detail = _detail_text(status.details)
    if status.status == "SPECS_MISSING":
        return "не в ТОП: " + (detail or "характеристики не подтверждены")
    return "не в ТОП: " + (detail or "не подходит по требованиям")


def format_priority_block(statuses: Sequence[PriorityStatus]) -> str:
    if not statuses:
        return ""
    lines = ["⭐ <b>Приоритетная модель</b>" if len(statuses) == 1 else "⭐ <b>Приоритетные модели</b>"]
    for status in statuses:
        lines.append(html.escape(f"{status.brand} {status.model}".strip(), quote=False))
        specs = _specs_line(status.specs)
        if specs:
            lines.append(html.escape(specs, quote=False))
        if status.best_price is not None and status.best_store:
            stock = "в наличии" if status.available else "нет в наличии"
            lines.append(
                f"Цена: {_rub(status.best_price)} ₽ — {_STORE_LABEL.get(status.best_store, status.best_store)}, {stock}"
            )
        elif status.offers:
            lines.append("Цена: нет предложения в наличии")
        if status.previous_price is not None and status.best_price is not None:
            delta = int(status.best_price) - int(status.previous_price)
            sign = "+" if delta > 0 else "−"
            lines.append(f"Было: {_rub(status.previous_price)} ₽ ({sign}{_rub(abs(delta))} ₽)")
        if status.history_min is not None:
            lines.append(f"Минимум: {_rub(status.history_min)} ₽")
        lines.append("Статус: " + html.escape(status_text(status), quote=False))
        if status.url:
            lines.append(str(status.url))
    return "\n".join(lines)
