# Changelog

## Unreleased

Selection criteria for the current purchase, and a watched MSI model.

- Price cap 330 000 ₽ (inclusive) for TOP, recommendation, BUY candidates, alerts, cross-store saving, and Thailand TOP / comparison. Thai captions read the configured cap
- Hard eligibility for TOP, recommendation, BUY candidates, and model menus: RTX 5070 Ti / 5080, at least 32 GB installed RAM, and a screen wider than 1920, at least 1440 tall, and at least 2560×1440 pixels. Unknown RAM or resolution does not pass. Collection and price history keep every product
- TOP, history picker, recommendation picker, BUY, and the n8n top payload read one filtered list
- Title parsing ignores upgrade limits such as «до 64 ГБ». Resolutions written as `2560*1440` are recognized
- Priority watchlist (`PRIORITY_MODELS`) with MSI Vector 17 HX AI A2XWIG-063XRU: price, last change, minimum, availability, and the exclusion reason, shown under the TOP. The Regard collector requests one product card if the GPU searches miss it
- Models sold by one store are ranked with the same filters. They get no cross-store saving. When a single-store offer is the same laptop as another ranked offer (shared model code, same GPU, RAM, and screen), only the cheaper one stays
- Daily Digest workflow JSON in the repo is generated with the cap from `config` (`generate_daily_digest_workflows.py --digest-only`). The running n8n workflow is not updated by this

### Unchanged

- Ranking weights / BUY rules and the 14-day maturity / GPU target prices / Thailand matching / SQLite schema / n8n workflows / systemd

## 0.8.1 — 2026-10-06

Telegram slash commands for the actions that already exist as inline buttons.

- Commands `/start`, `/menu`, `/recommend`, `/top`, `/history`, `/markets`, `/run`, `/status`, `/version`, and `/help` call the same handlers as the menu
- `/command@BotUsername`, extra spaces, and letter case are accepted. Plain text is not a command. An unknown slash command points to `/help`
- `/help` lists the commands and shows the main menu. Admin checks still run before any command
- Inline callback ids are unchanged

### Unchanged

- Deal Ranking / BUY rules / purchase recommendation verdicts / Thailand matching / SQLite schema / n8n workflows / Russian timers

## 0.8.0 — 2026-10-05

Deterministic recommendation: what to buy now, from the current Russian TOP and one Thailand comparison.

- Main menu: «Что покупать сейчас» picks the best fresh Russian model or one of the current TOP 5, then enqueues the existing Thailand job
- The verdict uses the existing BUY evaluator, price history, and EXACT / SAME_FAMILY / EQUIVALENT trust. A shared platform token is not treated as the same product
- Thailand at least 10% cheaper on a verified available exact or same-config model is THAILAND_BETTER. BUY_NOW_RUSSIA also needs a mature history, STRONG_BUY, and complete Thailand coverage
- An incomplete Thailand check does not become «buy in Russia now» or «wait» just because a store failed. Out of stock is not a purchasable Thailand price
- An automatic BUY still sends one Russian signal, then one consolidated recommendation instead of a separate comparison and full Thailand TOP

### Unchanged

- Deal Ranking weights / BUY rules and the 14-day maturity threshold / Thailand matching / HTTP 429 handling / SQLite schema / n8n workflows / Russian timers

## 0.7.1 — 2026-10-05

Targeted Thailand family matching now uses every strong identifier, not only the canonical part number.

- A regional variant such as `G614PR-RV027` against `G614PR-TS113W` is SAME_FAMILY (`G614PR`), even when the manufacturer part number is `90NR0NJ7-M001J0`
- EXACT still requires a full identifier match (`ASUS-G614PR-TS113W` and `G614PR-TS113W`). A shared platform token alone is not EXACT
- An MSI `9S7` prefix is still not a family, and unrelated platforms such as `G614PR` and `G614FR` stay apart

### Unchanged

- Deal Ranking / BUY rules and the 14-day maturity threshold / HTTP 429 handling / Thailand sources / Russian pipeline / SQLite schema / n8n workflows / Russian timers

## 0.7.0 — 2026-10-05

Targeted Thailand comparison for a chosen Russian model, and safer HTTP 429 handling.

- Automatic BUY / STRONG_BUY enqueues one Thailand job that compares that exact Russian model and then the general Thailand TOP
- Manual search from the Russian TOP: Markets → find a model in Thailand, with a human-readable picker (model, GPU, price)
- Match levels EXACT / SAME_FAMILY / EQUIVALENT / NOT_FOUND. SAME_FAMILY requires a real platform code; an MSI `9S7` prefix is not a family
- Retailer-prefixed SKUs stay one exact model (`ASUS-G614PR-TS113W` and `G614PR-TS113W`)
- Model-specific comparison is sent before the market TOP. An exact model above 300 000 ₽ is shown in the comparison and kept out of the TOP. Out of stock is reported as found, not missing
- HTTP 429 is `RATE_LIMITED`: Retry-After is honored, otherwise a 60 minute pause, no immediate retry, and it does not increment the generic failure breaker
- A bare legacy `HTTP_ERROR` is not guessed to be a 429

### Unchanged

- Deal Ranking / BUY rules and the 14-day maturity threshold / Russian pipeline / SQLite schema / n8n workflows / Russian timers

## 0.6.0 — 2026-10-05

Thailand coverage: three direct stores, source health, and stricter GPU provenance.

- SpeedCom Thailand adapter (Shopify public `products.json`; variant price belongs to the variant whose GPU is confirmed)
- InvadeIT Thailand adapter (public category JSON API)
- IT City Thailand adapter (public search index; desktop GPUs and PC sets are not laptops)
- Source health file and circuit breaker (3 consecutive transport failures, 24h cooldown, one probe after cooldown; manual diagnostics can bypass)
- Normalized store error codes (`BLOCKED`, `CHALLENGE`, `NO_RESULTS`, `TIMEOUT`, `NETWORK_ERROR`, `PARSE_ERROR`, `HTTP_ERROR`, `UNSUPPORTED`); technical detail stays in the snapshot
- BaNANA and Lazada remain in the tree and are disabled in the automatic scan by default
- `NO_RESULTS` and intentionally disabled sources do not mark the scan PARTIAL
- Explicit product GPU line wins over collection tags (an RTX 5050 spec is not a 5070 Ti)
- Cross-store grouping: one best verified offer, price_rub ASC, cap ≤ 300 000 ₽. A retailer-prefixed SKU (`ASUS-G614PR-TS113W`) matches the bare model code (`G614PR-TS113W`) so the same laptop is not listed twice

### Unchanged

- Deal Ranking / BUY rules and thresholds / Russian pipeline / SQLite schema / n8n workflows / Russian timers

## 0.5.3 — 2026-10-05

Reliability release: release packaging, BUY repeat semantics, Thailand enqueue retry.

- Hardened release packaging: single release manifest (`scripts/release_manifest.py`), `scripts.release_verify` for bundle / deployed tree / image (sha256, import graph, LF, mtime, executable `.sh`, no secrets / tests / DB, stale files)
- Canonical image build: `./deploy/compose.sh build-image` (plain `docker compose build` was a no-op for the `manual`-profile service)
- Reduced repeated BUY notifications: 24h cooldown expiry alone no longer re-notifies; repeat only on a meaningful event (≥3% / ≥5000 ₽ better price, BUY→STRONG_BUY, new historical low, new #1), deduped per model cluster
- Automatic BUY requires **≥14 days** of stored price history for the cluster (`BUY_MIN_HISTORY_DAYS`); manual Thailand comparison not gated
- Thailand enqueue state independent from the Russian BUY notification: a failed enqueue is retried only as an enqueue (every 110 min, max 3 retries) without re-sending the BUY message; a new meaningful event supersedes a stale retry
- Enqueue filesystem / permission failures no longer abort the BUY notification
- Tests no longer write repository `data/` / `logs/` / `dist/` (injectable specs / BUY state / jobs / worker state paths)
- Removed confirmed dead code (`main.py`, `analyze_unmatched.py`, obsolete diagnostics, unused `RANK_*` aliases / imports)

### Unchanged

- Deal Ranking / BUY rules A/B/C and thresholds / SQLite schema / Thailand parsers / n8n workflows / Russian timers

## 0.5.2 — 2026-10-05

Thailand user-facing recommendations hardened for production.

- Thailand recommendations capped at **≤ 300 000 ₽** (inclusive); over-cap offers stay in snapshots only
- Thailand Telegram TOP sorted by **price_rub ASC** (value score / confidence are secondary)
- New Telegram heading: **ЛУЧШИЕ ЦЕНЫ В ТАИЛАНДЕ ДО 300 000 ₽**
- Lazada Thailand marketplace adapter (browser; seller trust; variant price verification)
- Marketplace labels (`Lazada · seller`) — never mislabeled as direct retailer
- BaNANA: HTTP → bounded Playwright fallback; Cloudflare / challenge fail-safe (no bypass)
- Direct / marketplace duplicate grouping (one best offer + optional alt channels)
- Async Thailand worker architecture unchanged from v0.5.1

### Unchanged

- Russian ranking / BUY rules / SQLite schema / n8n workflows / Russian timers

## 0.5.1 — 2026-10-04

Thailand scans decoupled from the Russian pipeline critical path.

- Asynchronous file queue: `data/thailand_jobs/{pending,processing,archive,failed}`
- Dedicated Thailand worker (`python -m thailand.worker --drain`) via Docker + systemd `.path`
- Automatic BUY and manual Telegram **🌍 Рынки** enqueue jobs; they no longer block pipeline/control bot
- Atomic enqueue / claim, dedupe, stale processing recovery (max 2 attempts), archive/failed retention
- Filesystem process lock for `buy_opportunity_state.json` RMW
- `pipeline.completed` reports Thailand as `queued` (optional `thailand_job_id`) before the worker finishes
- Worker never writes Russian SQLite products / pipeline_runs / store_runs

### Unchanged

- BUY thresholds / Deal Ranking / Thailand parsers / FX / SQLite schema / n8n workflows / Russian timers

## 0.5.0 — 2026-10-04

Buy Opportunity signal and on-demand Thailand market comparison (verified offers only).

### Buy Opportunity (Russia)

- Explainable **BUY** / **STRONG_BUY** from Deal Ranking v2 + history (config-driven rules A/B/C)
- No automatic signal without historical minimum
- 24h cooldown / dedupe with bypass on ≥3% better price, ≥5000 ₽ drop, BUY→STRONG_BUY, or new #1 model
- State: `data/buy_opportunity_state.json` (atomic JSON; corrupt → fail-safe empty)

### Thailand on-demand market

- Separate `thailand/` subsystem (not in Russian 2h `stores.registry`)
- Scan triggers: new BUY signal **or** admin Telegram **🌍 Рынки**
- **JIB**: discovery via `search_suggestion` + verification via `readProduct` (GPU / availability / specs)
- **Advice** / **BaNANA** (`bnn.in.th`): experimental / fail-safe — live HTTP may return Cloudflare 403
- THB→RUB via Bank of Russia daily FX (nominal-aware); stale FX not used for automatic country verdict
- Match levels: EXACT / SAME_FAMILY / EQUIVALENT / ALTERNATIVE
- `international_value_score` + `international_confidence` (no Russian history component)
- Country verdict only for VERIFIED Thai offers with comparable config
- Snapshots: `data/thailand_scans/` (retention ≤20); Thailand never writes Russian SQLite products
- Thailand failures isolated from Russian SUCCESS / PARTIAL / FAILED

### Offer verification hardening

- Search query GPU is **not** authoritative (`candidate_gpu` only)
- Unknown availability excluded from automatic Thailand BEST / TOP
- Unverified candidates excluded from automatic TOP / strong country verdict
- Regression: TUF A18 FA808UH (RTX 5050) must not appear as RTX 5080 in Verified TOP

### Telegram

- Menu **🌍 Рынки** → Россия / Проверить Таиланд / Сравнить сейчас (admin allowlist)
- Manual Thailand scan does **not** run Russian collection
- Automatic BUY + Thailand report: ≤3 messages

### Unchanged

- Russian Deal Ranking weights
- Price cap ≤ 300 000 ₽
- Price History / charts
- SQLite schema
- n8n workflows / pin 2.42.1
- systemd schedules

## 0.4.0 — 2026-10-04

Better specification coverage and safer enrichment/cache merging for Deal Ranking v2.

### Specs / enrichment

- Collected `Product.metadata` is merged into `product_specs.json` for **all** successful stores before SQLite drops it
- KNS / Citilink catalog + title (+ Citilink already-loaded page) specs reach the cache
- Regard / ANDPRO structured enrich runs again on **cache miss** (identifiers alone no longer skip specs recovery)
- Safer title extraction: GPU / CPU / RAM / SSD / screen / numeric resolution; GPU VRAM is not treated as system RAM
- Non-destructive cache merge + field provenance; `SPEC_CONFLICT` keeps the trusted value
- Atomic `product_specs.json` writes (temp → fsync → replace)
- Read-only diagnostic: `python -m scripts.diagnose_spec_coverage`

### Unchanged

- Scoring weights (Ranking v2)
- DB schema
- Price cap ≤ 300 000 ₽
- Store set / systemd / n8n pin

## 0.3.0 — 2026-10-04

Post-v0.2.0 production features packaged as a stable release.

### User-visible

- **Deal Ranking / Scoring v2** — explainable score `0..100` with confidence; components: GPU, price/value, CPU class, RAM, SSD, screen, historical opportunity, cross-store saving; cluster-wide specs resolution
- **Telegram TOP** — shows score / confidence / short reasons; uses ranking v2
- **Price History v1** — per-model history card from Telegram control bot
- **Price History v2** — PNG charts for 30 / 90 / all-time periods (`sendPhoto`, in-memory matplotlib)
- History picker order follows the same ranking as TOP

### Operations

- Independent **n8n host watchdog** (systemd)
- **Failure Alert** for PARTIAL / FAILED pipeline events
- **Daily Digest v1** from `pipeline.completed` summaries
- Production **n8n pinned** to immutable image `2.42.1` (`sha256:e7634e62…`)
- Telegram / httpx logging does not expose bot token URLs

### Packaging

- Clean release bundle normalizes `VERSION` / systemd / shell scripts to LF for Linux VPS deploy

### Unchanged from v0.2.0

- Global price cap **≤ 300 000 ₽** for TOP / alerts / cross-store tracking
- Four enabled stores: Regard, ANDPRO, KNS, Citilink
- Systemd primary scheduler; n8n as OPS layer only

## 0.2.0 — 2026-09-30

Initial multi-store production cutover (Regard / ANDPRO / KNS / Citilink), systemd deploy, Telegram control bot, price cap, baseline ranking.
