from __future__ import annotations

import json
from pathlib import Path

from enrichment import enrich_unmatched
from product_identity import pair_unmatched


def _store_label(store: str) -> str:
    return store.upper() if store == "andpro" else store.capitalize()


def main() -> None:
    unmatched_path = Path("data/unmatched_products.json")
    if not unmatched_path.exists():
        raise SystemExit(
            "Нет data/unmatched_products.json — сначала запустите python compare.py"
        )

    unmatched = json.loads(unmatched_path.read_text(encoding="utf-8"))
    regard_raw = [x for x in unmatched if x.get("store") == "regard"]
    andpro_raw = [x for x in unmatched if x.get("store") == "andpro"]

    print(f"Unmatched Regard: {len(regard_raw)}")
    print(f"Unmatched ANDPRO: {len(andpro_raw)}")
    print()
    print("Обогащение характеристик из магазинов...")

    enriched = enrich_unmatched(unmatched)
    regard_ids = [x for x in enriched if x.store == "regard"]
    andpro_ids = [x for x in enriched if x.store == "andpro"]

    result = pair_unmatched(regard_ids, andpro_ids)

    report = {
        "summary": {
            "unmatched_regard": len(regard_raw),
            "unmatched_andpro": len(andpro_raw),
            "confirmed": len(result["confirmed"]),
            "candidates": len(result["candidates"]),
            "rejected": len(result["rejected"]),
            "unpaired_regard": len(result["unpaired_regard"]),
            "unpaired_andpro": len(result["unpaired_andpro"]),
        },
        "enriched_products": [p.to_dict() for p in enriched],
        "confirmed": [c.to_dict() for c in result["confirmed"]],
        "candidates": [c.to_dict() for c in result["candidates"]],
        "rejected": [c.to_dict() for c in result["rejected"]],
        "unpaired_regard": [p.to_dict() for p in result["unpaired_regard"]],
        "unpaired_andpro": [p.to_dict() for p in result["unpaired_andpro"]],
    }

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/unmatched_analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(
        f"CONFIRMED по дополнительным идентификаторам: {report['summary']['confirmed']}"
    )
    print(
        f"CANDIDATE по полной конфигурации: {report['summary']['candidates']}"
    )
    print(f"REJECTED: {report['summary']['rejected']}")
    print(
        "Без подходящей пары: "
        f"{report['summary']['unpaired_regard'] + report['summary']['unpaired_andpro']}"
        f" (Regard {report['summary']['unpaired_regard']}, "
        f"ANDPRO {report['summary']['unpaired_andpro']})"
    )
    print()

    for status_key, title in (
        ("confirmed", "CONFIRMED"),
        ("candidates", "CANDIDATE"),
    ):
        items = result[status_key]
        if not items:
            continue
        print("=" * 60)
        print(title)
        print("=" * 60)
        for cmp in items:
            left = cmp.left if cmp.left.store == "regard" else cmp.right
            right = cmp.right if cmp.right.store == "andpro" else cmp.left
            print(f"Regard SKU: {left.sku}")
            print(f"ANDPRO SKU: {right.sku}")
            print(f"  {left.name}")
            print(f"  {right.name}")
            print(f"  reason: {cmp.reason}")
            if cmp.matching_fields:
                print(f"  matching: {cmp.matching_fields}")
            if cmp.different_fields:
                print(f"  different: {cmp.different_fields}")
            print()

    if result["rejected"]:
        print("=" * 60)
        print("REJECTED (только пары с пересечением бренда/модели)")
        print("=" * 60)
        for cmp in result["rejected"]:
            left = cmp.left if cmp.left.store == "regard" else cmp.right
            right = cmp.right if cmp.right.store == "andpro" else cmp.left
            print(f"Regard SKU: {left.sku} | ANDPRO SKU: {right.sku}")
            print(f"  reason: {cmp.reason}")
            if cmp.different_fields:
                print(f"  different: {cmp.different_fields}")
            print()

    # Special ASUS focus
    print("=" * 60)
    print("ASUS focus: G614PR-RV027 vs 90NR0NJ7-M001J0")
    print("=" * 60)
    left = next((p for p in regard_ids if p.sku == "G614PR-RV027"), None)
    right = next((p for p in andpro_ids if p.sku == "90NR0NJ7-M001J0"), None)
    if left and right:
        from product_identity import compare_identities

        cmp = compare_identities(left, right)
        print(f"status: {cmp.status}")
        print(f"reason: {cmp.reason}")
        print(f"Regard identifiers: vendorcode={left.manufacturer_part_number}, alt={left.alternative_part_numbers}")
        print(f"ANDPRO identifiers: mpn={right.manufacturer_part_number}, model={right.model}")
        print(f"matching_fields: {cmp.matching_fields}")
        print(f"different_fields: {cmp.different_fields}")
        print(f"missing_fields: {cmp.missing_fields}")
    else:
        print("Одна из позиций не найдена после enrich")

    print()
    print("Отчёт сохранён: data/unmatched_analysis.json")


if __name__ == "__main__":
    main()
