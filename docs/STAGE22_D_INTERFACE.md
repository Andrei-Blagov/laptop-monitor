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

## Реализованный API

- `market_contracts.russian_snapshot(mapping, identity_evidence=...)` принимает **фактический** B JSON без файлового чтения и без вызова exporter. `thai_snapshot(mapping)` принимает C JSON. Результат — frozen MarketSnapshot с tuple offers, SpecField и Evidence, SourceCoverage и личными membership metadata. `fx_observation(mapping)` принимает FxObservation; опубликованная дата — ISO YYYY-MM-DD, nominal — положительный integer, rub_per_thb — уже цена одного THB (повторно на nominal не делится).
- `thai_market_ranking.build_thai_rankings(snapshot, fx, now=UTC, policy=ComparisonPolicy(...))`: all_offers, eligible_models **без обрезки**, thai_top_value[:5], полный thai_cheapest, exclusions, discovery_current и coverage_complete.
- `cross_market_matching.compare_configuration(left, right, now=UTC)`: одна нормализация и MatchProof для обеих сторон. EXACT требует полного общего кода, одинакового подтверждённого бренда и шести полей; известные дополнительные hardware conflicts блокируют его. EQUIVALENT требует подтверждённого family+generation, бренда и конфигурации. Подтверждённое family без generation даёт SAME_FAMILY/incomplete. ALTERNATIVE не даёт экономии. ОС/клавиатура/гарантия раскрываются как коммерческие отличия.
- `cross_market_comparison.compare_markets(thai, russian_or_none, fx_or_none, now=UTC, policy=..., expenses=..., unknown_costs=...)`: один CrossMarketComparison для всех views. `comparison_view(result, all/general_top/personal_top3)` выбирает те же pair rows без второго пересчёта. Выдачу следует использовать как read-only derived data, а не как повторный ThaiCatalogSnapshot input.
- `Expense(label, amount_rub, confirmed, evidence_ref, offer_id=None)`: в known costs идут только confirmed=True с evidence_ref. offer_id задаёт конкретное предложение; None явно означает расход, применимый к каждому предложению. Неподтверждённая категория остаётся unknown. Полученная сумма называется thai_known_total_rub/cost_scope=known_costs_only; BUY в ядре отсутствует.

## Точные требования к C

C должен сериализовать **собственные** spec и availability facts каждого offer. Нельзя заменять captured_at на время пересчёта каталога, копировать поля из profile или другого SKU. Evidence.source/external_id должны ссылаться на этот offer; разрешённые methods: title, explicit_title, specs_cache, structured_api, structured_product_page, product_detail, store_listing. capture_state=unknown, отсутствующий/будущий timestamp и поисковый method не подтверждают факт. Spec evidence не имеет отдельного TTL: подтверждается известное историческое наблюдение неизменной конфигурации; stock/price имеют отдельные сроки 6h TH и 3h RU.

При availability_checked_at требуется собственное availability_evidence с **тем же** captured_at; старое доказательство нельзя оживить новым внешним timestamp. Цена должна быть положительной, finite, принадлежать варианту; `price_verified_for_variant=true` обязателен. TH seller_trust: direct_retailer или A/B для проверенного marketplace. SourceCoverage enabled/policy_disabled отражает текущую политику источника; 429/partial ограничивает coverage и не аннулирует уже проверенную свежую пару. source/state (или status), checked_at (или latest_attempt_at), not_completed_keys — canonical coverage input.

`specs.screen_size_inch` принимается как alias `screen_inch`; GPU — точная ноутбучная RTX 5070 Ti/5080 без маркетинговых догадок. Для доказанного 1 TB можно передать текст `1 TB` либо nominal_ssd_gb=1000 рядом с собственным подтверждённым SSD-полем; числовые 1000 и 1024 без такой семантики не считаются автоматически одинаковыми. Экран требует числового разрешения. Optional extras: gpu_vram_gb, gpu_tgp_w, screen_refresh_hz, panel_type и коммерческие поля в known_extras как value/evidence.

Текущий B не требует правки: D читает его screen_inch и SKU/MPN candidates, source freshness, profile match_kind и capture_state. Для family/generation допустим optional identity_evidence, bound к native B offer, с доказательством из реально наблюдённого источника. Эту информацию поставляет последующая интеграция; D не угадывает family по региональному suffix и не подтверждает поля неизвестного времени. Изученные ограничения B сохранены и покрыты тестом настоящего экспорта.

Полный синтетический формат C: [d_catalog_snapshot.json](../tests/fixtures/stage22/d_catalog_snapshot.json). В тесте он обрабатывается только с reference_now из fixture; значения не являются котировками живых магазинов. Корневой variants[] — metadata; authoritative spec fields должны присутствовать на offer. Variant ID группировки D синтезируется из доказанной конфигурации; исходный variant_id C сохраняется в all_offers и pair observations.

## Поведение и ограничения

Тайский TOP сортируется score DESC → confidence DESC → точная RUB price ASC → stable ID. Компоненты существующего international score сохранены; price/value curve посчитана Decimal, затем применена прежняя точность компонента 0,01. История/cross-store российского score не участвуют. TOP и all eligible models не зависят от RU input.

Объединяются только EXACT-конфигурации с общим полным ID **у всех** членов, без пробелов/коммерческих конфликтов; неизвестное поле не соединяет две разные конфигурации. Региональные SKU не объединяются молча. Все raw offers и отказные причины сохранены. Неполные конфигурации могут оставаться отдельными TOP rows, если четыре обязательных hardware gates подтверждены; неизвестные CPU/диагональ дают 0 баллов по соответствующей части и запрещают подтверждённую пару.

Матрица содержит все RU×TH offers (включая диагностику отказов) без TOP cap. Membership отражает published B model_ids/personal_profiles; series membership не означает точный личный SKU. Для итоговой пары выбирается **минимальная сопоставимая RU цена**, затем минимальная TH known total: дорогой RU listing не увеличивает искусственно заявленную экономию. Матрица и views используют одну функцию price_gap, bands <5 / 5–<10 / ≥10%, положительный saving означает TH дешевле.

Unmatched создаётся по всему eligible TH list, включая модели ниже TOP-5; причины not_observed/incomplete/conflict/limited_coverage относятся к обследованным источникам. Стартовый value candidate: score≥75 и price≤GPU target, настраивается через ComparisonPolicy. Полный список сохранён, короткий unmatched_value_top содержит первые три. При отсутствующем/устаревшем RU snapshot — TH-only, pairs пуст и ru_snapshot_id null. При плохом FX остаются THB/raw diagnostics, текущие RUB TOP/экономия отсутствуют.

Возраст широкого discovery отображается отдельно. Свежая собственная котировка может оставаться полезной при старом/неполном broad coverage; результат discovery_current=false и market_coverage_complete=false требует обновления C и не утверждает минимальную цену всего рынка. D сам не делает refresh/reuse/очередь/доставку. Размер полной матрицы O(RU offers × TH offers); искусственный market cap не введён. Future C/E/F должен хранить snapshot/result IDs и ограничивать сетевое планирование своими контрактами.

Все денежные входы проверяются на finite; для ресурсоограничения числового парсинга отвергаются экстремальные показатели степени за ±1000. Бюджет сравнивается до округления, включая тонкие дроби и низкую точность Decimal у caller. Known total отдельно проверяется против бюджета; unknown costs не дают гарантию конечной цены.
