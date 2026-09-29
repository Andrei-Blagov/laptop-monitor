from __future__ import annotations

import json
from pathlib import Path

from comparison import analyze_sku_overlap, match_products, normalize_sku
from identity_sync import identity_from_cache, load_specs_cache
from product_identity import ProductIdentity
from storage import DEFAULT_DB_PATH, get_all_identifiers, get_all_products


def _fmt_price(price: int | None) -> str:
    if price is None:
        return "цена неизвестна"
    return f"{price:,}".replace(",", " ") + " ₽"


def _availability_label(available: bool) -> str:
    return "в наличии" if available else "нет в наличии"


def _store_label(store: str) -> str:
    return store.upper() if store == "andpro" else store.capitalize()


def _print_match(match) -> None:
    print("=" * 50)
    print(match.name)
    print(f"SKU: {match.display_sku}")
    print(f"Match: {match.match_method}")
    if match.matched_identifier:
        print(f"Identifier: {match.matched_identifier}")
    print()

    for offer in match.offers:
        price = _fmt_price(offer.price)
        avail = _availability_label(offer.available)
        print(f"{_store_label(offer.store)}:  {price} [{avail}]")
        if offer.sku:
            print(f"  sku: {offer.sku}")
        if offer.url:
            print(f"  {offer.url}")

    print()
    if match.price_difference is not None:
        print(f"Разница: {_fmt_price(match.price_difference)}")
    else:
        print("Разница: н/д")

    if match.cheapest_available is not None:
        print(
            "Минимальная цена среди доступных: "
            f"{_store_label(match.cheapest_available.store)}"
        )
    else:
        print("Минимальная цена среди доступных: нет available-предложений с ценой")
    print("=" * 50)
    print()


def _load_specs_by_key() -> dict[tuple[str, str], ProductIdentity]:
    cache = load_specs_cache()
    result: dict[tuple[str, str], ProductIdentity] = {}
    for key, raw in cache.items():
        if not isinstance(raw, dict):
            continue
        store = str(raw.get("store") or "")
        external_id = str(raw.get("external_id") or "")
        if not store or not external_id:
            if ":" in key:
                store, external_id = key.split(":", 1)
            else:
                continue
        identity = identity_from_cache(cache, store, external_id)
        if identity is not None:
            result[(store, external_id)] = identity
    return result


def build_comparison(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    write_artifacts: bool = True,
    data_dir: Path | str | None = None,
):
    """
    Matching Regard↔ANDPRO.

    Returns comparison.MatchResult (matches, unmatched, stats, ...).
    """
    products = get_all_products(db_path)
    identifiers = get_all_identifiers(db_path)
    specs_by_key = _load_specs_by_key()
    result = match_products(
        products,
        identifiers,
        specs_by_key=specs_by_key,
    )

    if write_artifacts:
        out = Path(data_dir) if data_dir is not None else Path("data")
        out.mkdir(parents=True, exist_ok=True)
        (out / "comparison.json").write_text(
            json.dumps(
                [m.to_dict() for m in result.matches],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (out / "unmatched_products.json").write_text(
            json.dumps(
                [
                    {
                        "store": o.store,
                        "external_id": o.external_id,
                        "sku": o.sku,
                        "normalized_sku": normalize_sku(o.sku),
                        "name": o.name,
                        "price": o.price,
                        "available": o.available,
                        "url": o.url,
                    }
                    for o in result.unmatched
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (out / "identity_conflicts.json").write_text(
            json.dumps(
                list(result.ambiguous) + list(result.conflicts),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return result


def main() -> None:
    products = get_all_products(DEFAULT_DB_PATH)
    overlap = analyze_sku_overlap(products)
    result = build_comparison(DEFAULT_DB_PATH, write_artifacts=True)

    print("Диагностика SKU:")
    print(f"  SKU Regard: {overlap['regard_sku_count']}")
    print(f"  SKU ANDPRO: {overlap['andpro_sku_count']}")
    print(f"  Exact matches: {overlap['exact_matches']}")
    print(f"  Matches после нормализации: {overlap['normalized_matches']}")
    print()
    print("Matching:")
    print(f"  Exact/normalized SKU matches: {result.stats['sku_matches']}")
    print(
        f"  Strong identifier matches: {result.stats['strong_identifier_matches']}"
    )
    print(f"  AMBIGUOUS: {result.stats['ambiguous']}")
    print(f"  CONFLICT: {result.stats['conflicts']}")
    print()

    if not result.matches:
        print("Подтверждённых совпадений между магазинами нет.")
    else:
        for match in result.matches:
            _print_match(match)

    print(f"Совпавших моделей: {result.stats['matched_models']}")
    print(f"Совпавших offers: {result.stats['matched_offers']}")
    print(
        f"Товаров без подтверждённого совпадения: "
        f"{result.stats['unmatched_products']}"
    )


if __name__ == "__main__":
    main()
