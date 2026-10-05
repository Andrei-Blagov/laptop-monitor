# Changelog

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
