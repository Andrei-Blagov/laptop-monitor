# Stage 2.2-D — интерфейс чистого ядра сравнения

База: пакет B, b3d585e6cd8cad277f8b04f32e1ae08a6138aaf0. Stacked PR поверх feature/stage-2-2-ru-snapshot. Согласованные источники: STAGE22_DECISIONS.md, STAGE22_CONTRACTS.md, STAGE22_A_ACCEPTANCE.md.

## Граница интеграции

Новые top-level модули market_contracts.py, thai_market_ranking.py, cross_market_matching.py и cross_market_comparison.py принимают данные в памяти. Нет HTTP, чтения snapshots с диска, записи SQLite, Telegram, расписаний или изменения российского ranking/BUY. Пакет C передаёт ThaiCatalogSnapshot schema 1; adapters и чистое ядро проверяют per-offer evidence и времена, а не доверяют готовому флагу purchase_eligible.

RussianMarketSnapshot schema 1 читается в формате фактического exporter B: public_price_rub, sku_candidates/mpn_candidates, specs с value/evidence, screen_inch, known_extras, source_coverage, top_model_ids и personal_profiles. Никакой TOP-обрезки offers.

B пока не экспортирует отдельные brand/manufacturer_family/manufacturer_generation. Явный бренд в product_name можно подтвердить этим собственным title observation. Подтверждение family/generation принимается через optional identity_evidence, связанное с source/external_id данного offer; без него региональная EQUIVALENT не разрешается. Удалять региональный суффикс или угадывать поколение по похожему названию нельзя. Изменения market_snapshot.py не нужны для EXACT; interface enrichment для региональных пар — задача последующей интеграции Cursor.

specs_cache capture_state=unknown и captured_at=null в B сохраняются как отсутствие подтверждения времени; ядро D не переносит на них дату нового price/stock observation. Такие поля не дают доказанной равной конфигурации.

## План входа C

Root: schema_version=1, snapshot_id, instance_id, discovery_scope_hash, discovery_checked_at, source_coverage, offers. Каждый offer содержит offer_id, source, external_id, variant_id, цену price_thb, price_checked_at, availability_state/availability_confirmed/availability_checked_at/availability_evidence, seller_channel/seller_trust, price_verified_for_variant, собственные specs value/evidence и optional known_extras/identity_evidence.

Evidence: source, external_id, captured_at UTC, method. Факт из search_query/catalog_filter не подтверждает характеристику. Цены Decimal; данные без обязательных подтверждений сохраняются в diagnostic exclusions. Все raw offers остаются в каталоге результата.

## Результат

Самостоятельный полный TH value ranking, TOP-5, cheapest ranking, все магазины одной доказанной конфигурации; матрица всех RU offers и TH variants с EXACT/EQUIVALENT/SAME_FAMILY/ALTERNATIVE и confirmed/incomplete/conflict/stale/oos/not_comparable. Membership RU full list, общий TOP и личный профиль — отдельные поля. Saving nullable, Decimal, bands comparable/slight/clear, known и unknown costs отдельно. Unmatched описывает обследованное покрытие и не является BUY.

Детальные API и результаты приёмки будут дополнены в этом PR после реализации.
