# Stage 2.2-A — контракты снимков и инварианты (draft v1)

**Base:** v0.10.2 / ff4d79889dbf78f78623fdac69734412c4bc42af.  
**Policy/acceptance source:** [STAGE22_DECISIONS.md](STAGE22_DECISIONS.md) и [STAGE22_WORK_ANALYSIS_20261014.md](STAGE22_WORK_ANALYSIS_20261014.md).  
**Status:** interface proposal for B/C/D/E; not connected to production.

Этот файл задаёт минимально обязательную семантику объектов. Речь об интерфейсах будущего кода, а не о готовых классах/миграциях. Не заменяет существующие v1 snapshots/jobs до реализации адаптеров.

## 0. Общие правила формата

Приоритет: согласованные решения в STAGE22_DECISIONS.md → уточнённый interface contract в этом файле → исходный анализ как архив предложений. Сценарии пакета A — упрощённые policy examples, **не serialized production snapshots**. Их `verified` — заданная синтетическая предпосылка, не замена field provenance, seller/variant evidence и дат наблюдений в runtime. A проверяет корректность примеров; выполнение matching/cache/queue алгоритмов проверяется в D/C/E.

- JSON UTF-8, object at root; schema_version integer, uuid/id string, instance_id string; обязательные для конкретного объекта timestamps (created_at / captured_at / started_at / finished_at / generated_at / fetched_at) — UTC ISO-8601 with explicit +00:00 or Z; запрет future timestamps для подтверждения freshness.
- Stable offer ID строится из (market, source, native external_id, variant_id при наличии), но не из price, scan timestamp или RU/TH-аналога. Stable model ID не должен меняться из-за курса/цены/score; нельзя объединять варианты без проверенной конфигурации.
- Не использовать один timestamp для всего: catalog_discovered_at, specs_checked_at, price_checked_at, availability_checked_at, fx.fetched_at и derived.generated_at — разная семантика.
- Числа RUB/THB: исходную цену хранить как число магазина (положительное), для промежуточных цен, курсов/expenses использовать **Decimal**. Все денежные значения finite; цена/курс/nominal строго положительны, подтверждённые расходы неотрицательны; bool не является числом цены. Сохранённый conversion/rate можно сериализовать десятичной строкой; рано округлять цену/разницу запрещено.
- Все поля критичной конфигурации имеют value и evidence (источник, external_id, captured_at, method). Полученное из title/spec/detail допускается только с явным provenance. Нельзя переносить характеристики из другой конфигурации или из поискового ключа.
- Immutable versioned JSON files в разрешённом root; файл сначала атомарно записан+fsync, после — атомарный latest manifest (pointer + sha256). Старые версии и referenced snapshots не удалять при retention. Все paths из job resolving only inside configured root; instance_id должен совпасть.
- Hash policy/config: eligibility_policy_id, ranking_policy_id, discovery_scope_hash позволяют отделить изменение правил от реального обновления магазина. Изменение ranking, RU TOP, BUY trigger или exchange rate не создаёт нового Thai discovery identity.
- Пустой каталог или не найденный SKU не равен доказательству отсутствия товара; всегда рядом completeness и coverage.

## 1. RussianMarketSnapshot (schema_version=1)

Минимальные корневые поля:

| Field | Semantics |
|---|---|
| snapshot_id / instance_id / schema_version | UUID, instance marker, 1 |
| pipeline_run_id / captured_at / created_at | Идентификатор pipeline; согласованное время RU observations и время экспорта |
| database_generation / specs_generation | Связанные consistent read epochs/hashes; no mixed-generation data |
| eligibility_policy_id / ranking_policy_id | Какие фильтры и ранжирование применялись |
| source_coverage[] | По магазину: latest_attempt_at, state (ok/partial/failed/stale), completed_work, limitations |
| offers[] | **Все** eligible fresh RU offers, включая single-store и не вошедшие в TOP, с собственной конфигурацией и ценой |
| models[] | Группы конфигураций: model_id, member_offer_ids, state, strong_identifiers, conflicts |
| top_model_ids[] / personal_profiles[] | Метки/ссылки, не фильтрация offers; TOP10 может быть короче всего каталога |
| excluded_count / exclusions | Диагностика, не смешивать с eligible catalog |

Каждый RU offer: offer_id, store, external_id, source_url, observed_at, stock_state/availability evidence, public_price_rub, product_name, full unmodified SKU/MPN candidates, six core field values + provenance. Для сравнения пары нужен конкретный offer и точная цена **этого** offer, а не min price другого варианта в cluster. При неизвестных CPU/диагонали offer может оставаться в `offers[]`, если проходит существующий hardware gate, но confirmed price pair запрещён. Не прошедшие hard eligibility или свежесть предложения сохраняются только в exclusions/диагностике, не в `offers[]`. RU freshness: возраст собственного observation 0–180 минут включительно и latest source attempt ok, как в v0.10.2; время экспорта не обновляет observation.

**Consistent export:** одна read-only database transaction/bulk read под pipeline lock, immutable specs copy/generation; проверка неизменности generation до manifest publication. Если гарантия невозможна, не публиковать latest. Russian pipeline/BUY failure policy не ухудшать из-за сбоя TH export.

## 2. ThaiCatalogSnapshot (schema_version=1)

Корневые поля: snapshot_id, instance_id, schema_version, discovery_scope_hash, scanner/parser_version, started_at, finished_at, discovery_checked_at, parent_snapshot_id (nullable), source_coverage[], raw_observations[], variants[], offers[], exclusions/conflicts[], catalog_limitations[], eligibility_policy_id, retained_at (только archiving).

- **Offer observation:** offer_id, source, native external_id, variant_id, direct URL, seller_channel/trust, price_thb, public_price_source, price_checked_at, availability_state (in_stock / online_available / store_pickup_only / out_of_stock / preorder / unknown), availability_confirmed, availability_checked_at, stock evidence, warranty/region where known, specs field evidence.
- **Variant:** sku, manufacturer_part_number, brand, family/generation, six core fields, optional known VRAM/TGP/panel/Hz/OS/keyboard/warranty; variant_id, refs to different offers of **same proven config**. Distinct region SKU not silently merged.
- **Source coverage:** store, enabled/policy_disabled, status (ok/partial/failed/429/deferred), request_count, completed query/page/detail keys, not_completed keys, next_allowed_at/retry_after where present. Last scan status does not change old offer timestamps.
- **Catalog completeness:** sufficient for discovery reuse only if age `now − discovery_checked_at` находится в 0–26h включительно и discovery_scope_hash compatible. `discovery_checked_at` означает время широкого прохода; incremental detail refresh и повторное построение manifest его не меняют. completed/not_completed source keys определяют фактическое покрытие, а не finished_at нового derived снимка. This is distinct from current quote: price_checked_at and availability_checked_at both в диапазоне 0–6h включительно with positive verified availability, usable FX, confirmed seller/variant price and hard eligibility.
- If out_of_stock, unknown, offer partial/unverified, missing resolution, observation age >6h or source policy_disabled: store for research/history but not current purchase TOP. Partial scan, timeout/429 другого запроса или открытый circuit breaker сами по себе не отменяют уже подтверждённую свежую пару; они ограничивают coverage. Нельзя считать непроверенные источники/запросы успешно обследованными.
- Recheck behavior 6–26h: append new observation only for actually queried offer, preserve untouched offers with original timestamps. On 429 stop store immediately; no wider rescan induced by daily/BUY retry.

**Legacy reading:** old Thailand scan without per-offer observation times/evidence may be shown as historical/candidate only, never automatically current. Retain last-good while new scan is partial; don't resurrect old current quotes without freshness check.

## 3. FxObservation

Fields: fx_id, source CBR, currency THB, nominal positive integer, published_date (official local date), rub_per_thb (Decimal string), fetched_at UTC, error (nullable). Recompute current freshness on **each read**: fetched_at age ≤36h, official published date not future and at most 4 calendar days old относительно даты `now` в Europe/Moscow. fetched_at age должен находиться в 0–36h включительно; 4 дня — включительная граница. error, future timestamp, неположительный или non-finite rate запрещают текущий RUB verdict. Source rate is indicative and not a guaranteed transaction exchange rate.

THB catalogue remains independently useful when FX unavailable, but no RUB cap/current country price verdict/value score using imaginary or stale RUB.

## 4. CrossMarketComparison (schema_version=1)

Input IDs: comparison_id, instance_id, ru_snapshot_id (nullable), th_snapshot_id, fx_id (nullable), eligibility_policy_id, comparison_policy_id, generated_at.

`ru_snapshot_id=null` разрешён для TH-only результата, если свежий совместимый RU snapshot отсутствует; тогда `pairs[]` пуст, current country verdict отсутствует, а unmatched reason — `limited_coverage` с причиной `ru_snapshot_unavailable`. Нельзя выдавать это за доказательство отсутствия RU-модели. Свежий пустой RU snapshot имеет настоящий ID и своё coverage. Цена конкретной подтверждённой пары допустима при partial coverage, глобальный минимум рынка — нет.

Output independent sections:
- thai_top_value[]: distinct confirmed current purchasable variants, score DESC, confidence DESC, price RUB ASC, stable ID; default 5.
- thai_cheapest[]: same eligibility, price RUB ASC.
- pairs[]: **all** identified potential comparable RU/TH offers, with pair_id, ru_offer_id, th_offer_id, th_variant_id, match_class (EXACT/EQUIVALENT/SAME_FAMILY/ALTERNATIVE или null), comparison_status (confirmed/incomplete/conflict/stale/oos/not_comparable), identifier_evidence, six-core comparisons, known_extras, missing_fields, conflicting_fields, field provenance, store timestamps, prices, known_costs, unknown_costs, saving_rub / saving_pct nullable.
- unmatched_th[]: each eligible TH variant with **no confirmed RU pair** over the full eligible RU dataset, but reason (not_observed / incomplete / conflict / limited_coverage), score, price and TH/RU TOP/profile membership. Never assert global absence.
- by_ru_offer_id / by_th_variant_id: mappings ID → list[pair_id], включая несколько сопоставимых магазинов; indexes into same pairs[]; no separate diverging match engine.
- source_coverage / data_freshness / policy_versions / limitations: feed all user-facing views.

**Match classes and gates:**
1. EXACT: identical **complete strong manufacturer ID** (without regional suffix stripping), same brand and six **confirmed** equal fields и отсутствие известных дополнительных hardware conflicts. Identical ID plus conflicting CPU/RAM/SSD/resolution ⇒ comparison_status=conflict, not EXACT; missing required field ⇒ incomplete. Полный identifier hit сохраняется отдельно в identifier_evidence, но не получает EXACT при incomplete/conflict; match_class может быть null.
2. EQUIVALENT: same evidenced manufacturer family and generation; different regional/model SKU accepted only after six core fields are **confirmed equal**, plus no known extra hardware conflicts. Explicit commercial differences (OS, keyboard, warranty) are noted.
3. SAME_FAMILY: evidenced family, not a confirmed equal config (different or incomplete); saving is null.
4. ALTERNATIVE: different brand/device or same GPU candidate without family proof; cannot be labeled same-model saving.
5. NO_CONFIRMED_RU_MATCH: **derived absence of confirmed pairs** in the actually inspected complete eligible RU snapshot, with coverage reason, not proof that the model is absent from Russia.

Six core fields: CPU precise model (not rough class), laptop GPU precise model, installed RAM, SSD nominal capacity with documented TB normalization, screen diagonal, numeric built-in resolution. Where known, contradictory VRAM, TGP, refresh, panel prohibit «same hardware» proof. Confirmation of price also requires matching variant, fresh stock, seller trust, fresh RU price and FX.

NO_CONFIRMED_RU_MATCH — status списка unmatched, **не** класс пары. comparison_status=confirmed только для EXACT/EQUIVALENT с usable quote/FX и свежей RU стороной; ALTERNATIVE имеет not_comparable. Все nullable FK/ID явно отражают отсутствие данных, строки пустого ID запрещены.

**Saving:** Decimal RU offer price − (Decimal THB × Decimal RUB/THB + confirmed expense RUB). Saving % = saving / RU price × 100. When comparable price pair unconfirmed, saving_rub and saving_pct MUST be null; no fabricated 0%. Known/unknown expenses separately disclosed. Price advantage bands abs(delta): <5%, 5–<10%, ≥10%; signed direction. Машинные bands: comparable / slight / clear; positive saving — TH дешевле, negative — RU дешевле, zero — сопоставимо. Сохранённый RUB пересчёт и расходы — decimal strings; допустимость бюджета проверяется до округления. BUY/historical urgency is separate from this.

## 5. MarketEvent / queue contract (schema_version=2)

Fields: event_id UUID, instance_id, created_at, trigger_type (daily/manual/buy/personal), requested_view, recipients[], source_ru_snapshot_id (BUY trigger snapshot immutable), requested_ru_snapshot_id (resolved on execution), subscription_scan_id (nullable), result_comparison_id (nullable), delivery_state (pending/sent/failed/deferred), not_before (nullable), retry_count.

- One discovery key = instance_id + TH market + discovery_scope_hash; deliberately no chat_id, minute bucket, RU TOP/profile or trigger.
- enqueue/dedupe/capacity/write and subscribe/publish under a cross-process lock. Multiple subscribers are not merged/deleted; each event may need own recipient/format.
- Workers claim by created_at with fair user/BUY priority; only one heavy scan at a time. If fresh compatible observations exist, view/delivery only; store HTTP=0.
- Daily at 08:00 Europe/Moscow; fresh broad scan <18h reused and current quote 6h revalidated only if necessary.
- Deferred event with future not_before **must not** remain in watched pending and cause systemd.path loop. Retry timer moves due deferred events without touching stores early.
- Failed Telegram delivery retries delivery from stored result без нового full scan. Перед retry проверяется свежесть исходных наблюдений: истёкший country verdict не повторяется как текущий; разрешён явно датированный исторический результат или новый derived view из уже свежих данных. A crash between network send and durable acknowledgement prevents true exactly-once; report this limitation.
- v1 pending jobs supported by adapter: russian_context is only historical trigger context (capped), not a full RU market. Missing fresh RU snapshot ⇒ TH-only result without current country verdict. Old BUY never sent twice during conversion.

## 6. Non-negotiable regression acceptance

Use offline fixtures in tests/fixtures/stage22, mock clock, isolated file roots, no live HTTP and no Telegram. Сценарии A проверяют входные предпосылки и ожидаемую policy, не вызывают будущие реализации D/C/E. Полная матрица исходного анализа — обязательства следующих пакетов, не утверждение о покрытии всех 56 сценариев пакетом A. Cases: RU 25 models/TH match RU #17, empty RU, matching MPN conflicts, regional equivalence, brand alternative, stale offers, one daily+manual+BUY with 1 scan, FX stale, stored SOURCE-429, disabled stores no calls, personal Lenovo family not EXACT. Use baseline RU replay to confirm original rank/BUY unchanged.

## 7. File/module ownership and rollout

**Передача 15.10.2026:** текущий единственный исполнитель всех модулей и тестов — ChatGPT Work. Cursor больше не участвует. Ниже зафиксировано историческое распределение A–F2; повторная реализация не требуется. См. [новый порядок работы](STAGE22_WORK_HANDOFF.md) и [комментарий владельца](https://github.com/Andrei-Blagov/laptop-monitor/pull/12#issuecomment-6194956542). Семантика контрактов и ограничения выпуска не изменяются.

Для следующего пакета интерфейсы фиксируются этим документом; исполняемые serializers/readers будут в B/C, matching в D. before source policy hash/schema change требуется согласованное обновление contract и fixture. `NO_CONFIRMED_RU_MATCH` и incomplete не разрешают переносить specs от cluster best, от series profile или другого offer.

Work A owns these documents and isolated fixtures. Work D later owns pure comparison/evidence algorithms + tests (separate branch/PR). Cursor B owns RU exporter/pipeline. Cursor C owns TH catalogue/reuse/fx; Cursor E owns queue/systemd, Cursor F owns Telegram/deploy. Agree file boundaries before parallel coding; never edit same file concurrently.

**Do not deploy, merge or tag** as part of A. Only after accepted offline tests and explicit user approval may the integrated release change VERSION, main, schedules, n8n or production.
