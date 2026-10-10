# Laptop Monitor

Мониторинг цен на игровые ноутбуки (RTX 5070 Ti / RTX 5080 Laptop) с multi-store adapter architecture, матчингом моделей, ranking v2, Telegram alerts / control, Price History charts и n8n OPS layer.

**Version:** читается из файла `VERSION` (сейчас `0.9.0`) — единственный source of truth.  
**Production target:** Linux VPS через **clean release bundle** + Docker + systemd  
**Windows:** только development / testing

---

## Что умеет (v0.9.0)

| Область | Описание |
|---------|----------|
| **Multi-store** | `StoreAdapter` registry. Enabled: Regard, ANDPRO, KNS, Citilink (experimental browser) |
| **Region** | `MONITOR_REGION=moscow` — cross-store только по одной географии |
| **Price semantics** | `Product.price` = публичная цена без membership/кредита/trade-in |
| **Price cap** | User-facing TOP / alerts / cross-store / Thailand: **≤ 330 000 ₽** (`MAX_TRACKED_PRICE_RUB`) |
| **Hard eligibility** | TOP, рекомендация, BUY и меню моделей: RTX 5070 Ti / 5080, установлено **≥ 32 GB RAM**, экран **> 1920 по горизонтали, ≥ 1440 по вертикали, ≥ 2560×1440 пикселей**. Неизвестные RAM / разрешение не проходят. Сбор и история не фильтруются. Модели одного магазина тоже в рейтинге, без cross-store saving. Одинаковая модель соединяется только при точном идентификаторе и полном совпадении CPU, GPU, RAM, SSD, диагонали и разрешения; все магазины и цены сохраняются. Региональный суффикс и `-wpro` остаются отдельными |
| **Priority watchlist** | `PRIORITY_MODELS`: модель отслеживается отдельно от рейтинга (цена, история, наличие, причина исключения). Если поиск Regard её не вернул, запрашивается одна карточка товара |
| **Independent snapshots** | Успешный store сохраняется отдельно; failed ≠ «все unavailable» |
| **Fresh TOP** | Только stores с последней попыткой `ok` и age ≤ `STORE_FRESHNESS_MAX_MINUTES` (180) |
| **Matching** | Exact SKU / MPN / strong ID + config conflict; без fuzzy auto-match |
| **CROSS_STORE** | Cheapest vs second among fresh offers; metadata содержит весь список |
| **Specs coverage** | Collected metadata → `product_specs.json` для всех stores; safe title parse; Regard/ANDPRO re-enrich on cache miss; non-destructive merge / `SPEC_CONFLICT` |
| **Ranking v2** | Explainable score **0..100** + **confidence**; GPU / price-value / CPU / RAM / SSD / screen / historical opportunity / cross-store saving (**weights unchanged**) |
| **Buy Opportunity** | Explainable BUY / STRONG_BUY + cooldown; при новом signal — enqueue Thailand job |
| **Thailand markets** | Async worker: **JIB**, **SpeedCom**, **InvadeIT**, **IT City**; targeted compare for a BUY or a model picked from the Russian TOP; **BaNANA** / **Lazada** off by default; rate-limit pause; TOP ≤330k ₽, price ASC |
| **Price History** | Telegram: карточка истории (v1) + PNG-графики 30 / 90 / all-time (v2), read-only |
| **Control bot** | Long polling; Run / TOP / История / **🌍 Рынки** / Status / Version; admin allowlist |
| **Scheduler** | Primary: **systemd timer** → `run_pipeline.py`. Thailand: **systemd.path** → async worker |
| **n8n OPS** | Pipeline Events (HMAC) + Failure Alert + Daily Digest; host watchdog; pinned **2.42.1** immutable digest |
| **Migration** | Явная команда `python -m scripts.migrate_db` |
| **Deploy** | Clean tar.gz bundle → `/opt/laptop-monitor` |

### Россия vs Таиланд

- **Россия** мониторится каждые ~2 часа (Regard / ANDPRO / KNS / Citilink) как раньше
- **Таиланд** не в Russian critical path:
  - Russian pipeline → BUY evaluate → enqueue job → `pipeline.completed` → exit
  - `laptop-monitor-thailand.path` → worker container → Thailand scan / comparison Telegram
- Scan только при новом BUY signal или вручную из Telegram **🌍 Рынки** (enqueue-only в control bot)
- Сравнение предполагает локальную покупку в Таиланде (без авиа / таможни / пересылки)
- User-facing Thailand recommendations: **verified + purchasable + ≤ 330 000 ₽**, sorted **price_rub ASC**
- Sources: JIB (direct); Advice / BaNANA direct (experimental/fail-safe); Lazada marketplace (labels `Lazada · seller`, never as direct)
- Over-cap Thai offers may remain in snapshots; Telegram shows only the excluded count

---

## Version (single source of truth)

```
VERSION
```

```bash
./deploy/compose.sh build-image
./deploy/compose.sh run --rm pipeline python run_pipeline.py --version
```

---

## Region

```
MONITOR_REGION=moscow
```

Все enabled stores зафиксированы на Москву / МО. Snapshot с другой географией не используется для cross-store.

---

## Stores

| Store | Status | Mode | Reliability | Notes |
|-------|--------|------|-------------|-------|
| Regard | **enabled** | http | stable | HTML catalog |
| ANDPRO | **enabled** | http | stable | HTML catalog |
| KNS | **enabled** | http | stable | HTML + `window.goodsList`; public ≠ club price |
| Citilink | **enabled** | browser | experimental | Plain Playwright Chromium; catalog HTTP 429, product JSON-LD OK |
| DNS | disabled | — | — | HTTP 401 challenge / API 403; Playwright без стабильного каталога |
| XCOM | disabled | — | — | DDoS-Guard / captcha |
| Technopark | disabled | — | — | HTTP 401/403; нет стабильного публичного source |

**Alternatives probed (not enabled):** OnlineTrade (captcha), M.Video (captcha), Eldorado (503). Marketplace (Ozon/WB) — не приоритет (динамические/персональные цены).

### Price semantics

- `Product.price` — обычная публичная цена
- `metadata.member_price` / `promo_price` / `credit_price` — при наличии
- Cross-store и ranking используют только public price
- Global tracking / TOP / alerts cap: `MAX_TRACKED_PRICE_RUB = 330000` (inclusive)

### Availability

- `Product.available=True` только если товар реально можно заказать
- `metadata.availability_status`: `in_stock` / `preorder` / `out_of_stock` / `display_only` / `unknown`
- Preorder ≠ обычный in_stock
- 0 products при ожидаемом ассортименте → **STORE FAILED**, не «все unavailable»

### Collection modes

- `http` — обычный HTTP/HTML/JSON
- `browser` — обычный Playwright Chromium **без** stealth / CAPTCHA solving / fingerprint spoofing / proxy rotation

---

## Matching & ranking v2

Приоритет идентификации: normalized SKU → MPN → alternative PN → strong ID → config compatibility.  
Конфликт конфигурации блокирует плохой cluster.

**Scoring v2** (после eligibility: fresh + available + valid price ≤ 300k):

| Component | Max |
|-----------|-----|
| GPU / performance class | 25 |
| Price / value vs GPU target | 25 |
| CPU / platform class | 15 |
| RAM | 10 |
| SSD | 5 |
| Screen / form-factor | 8 |
| Historical opportunity | 8 |
| Cross-store saving | 4 |
| **Total** | **0..100** |

Отдельно: **confidence 0..100** (полнота GPU/CPU/RAM/SSD/screen/history) — не часть score.

Specs берутся из `product_specs.json` (structured store data > collected metadata > safe title parse) и при необходимости с любого offer в cluster; отсутствующие поля не выдумываются. Диагностика: `python -m scripts.diagnose_spec_coverage`.

Telegram «Топ предложений» и picker «История цены» используют один и тот же `rank_clusters`.

---

## Price History

- **v1:** карточка модели — текущая лучшая цена, исторический минимум, дельты 7d/30d, per-store строки
- **v2:** кнопка «График» → период 30 / 90 / all → PNG `sendPhoto` (matplotlib Agg, in-memory, step-function + availability gaps)
- Strictly **read-only** (без pipeline / DB writes / alerts)

---

## Fresh TOP semantics

Store **fresh**, если:

1. самая последняя запись в `store_runs` имеет `status=ok`;
2. `finished_at` не старше `STORE_FRESHNESS_MAX_MINUTES` (по умолчанию 180 мин).

Stale не участвуют в TOP / cross-store saving. Нет fresh → «Нет свежих данных. Запустите проверку.»

---

## n8n (OPS layer)

Primary schedule остаётся **systemd**. n8n — event receiver / digest / failure workflows.

Production OPS (active):

| Component | Role |
|-----------|------|
| Pipeline Events | HMAC-verified `pipeline.completed` |
| Failure Alert | Telegram on PARTIAL / FAILED |
| Daily Digest v1 | Morning summary from archived events |
| Host n8n watchdog | Independent systemd health check |

Production n8n image pinned to **2.42.1** immutable digest  
`sha256:e7634e62f766044dc770460db8defdcd84034eb6d767dd6a1101d49fe98f814f`  
(не `:beta`).

```
N8N_WEBHOOK_URL=
N8N_WEBHOOK_SECRET=
N8N_WEBHOOK_TIMEOUT_SECONDS=8
```

Пустой URL → integration disabled. Ошибки webhook **не** меняют pipeline status.

HMAC-SHA256: `X-Laptop-Monitor-Timestamp` + `X-Laptop-Monitor-Signature` over `timestamp + "." + raw_body`.

Документация: [`deploy/n8n/README.md`](deploy/n8n/README.md).

**Не** монтировать `/var/run/docker.sock` в n8n. **Не** давать n8n shell control VPS.

---

## Database migration

```bash
python -m scripts.migrate_db --db data/laptop_monitor.db
```

Control bot при старте проверяет schema и завершается с ошибкой, если migrate ещё не выполнен.

---

## Telegram control

Кнопки и slash-команды вызывают одни и те же действия. Список для BotFather:

```text
start - Открыть главное меню
menu - Главное меню
recommend - Что покупать сейчас
top - Топ предложений
history - История цен
markets - Россия и Таиланд
run - Запустить проверку
status - Статус системы
version - Версия бота
help - Помощь
```

`/recommend` и `/markets` только открывают меню. Проверка и поиск Таиланда запускаются отдельным выбором, как с кнопок.

Кнопки:

- Запустить проверку
- Топ предложений
- История цены
- Что покупать сейчас
- **🌍 Рынки** → Россия / Проверить Таиланд / Сравнить сейчас
- Статус
- Версия

**🌍 Рынки:** ставит Thailand job в очередь (без Russian collection и без ожидания JIB/CBR в bot). Worker присылает comparison отдельным сообщением. Автоматический BUY тоже только enqueue.

Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_CHAT_ID`, `LAPTOP_MONITOR_INSTANCE`.

HTTP ошибки Telegram логируются без URL с token (httpx request logging silenced).

---

## Dev quick start

```bash
python -m venv .venv
# activate
pip install -r requirements.txt
# for Citilink browser mode:
playwright install chromium
cp .env.example .env
python run_pipeline.py --version
python -m unittest discover -s tests
```

Windows helpers: `scripts/windows/` (не production).

---

## Production deploy (clean bundle)

GitHub = source repo. VPS получает **release artifact**, не `git clone`.

### На Windows / CI

```bash
python -m scripts.backup_db --db data/laptop_monitor.db
python -m scripts.build_deploy_bundle
# -> dist/laptop-monitor-<VERSION>.tar.gz (+ RELEASE_MANIFEST.json inside)
python -m scripts.release_verify --bundle dist/laptop-monitor-<VERSION>.tar.gz
```

Bundle contents: single source `scripts/release_manifest.py`. The Docker image is
built from the extracted bundle (`COPY . ./` + whitelist `.dockerignore`).

### На VPS

```
/opt/laptop-monitor/
  VERSION, *.py, parsers/, stores/, integrations/, scripts/, deploy/
  .env, data/, logs/
```

```bash
python3 -m scripts.release_verify --tree .   # no stale / modified runtime files
./deploy/compose.sh build-image
python3 -m scripts.release_verify --image laptop-monitor:$(cat VERSION)
python -m scripts.migrate_db --db data/laptop_monitor.db
./deploy/compose.sh up -d control-bot
```

Подробности: [`deploy/README.md`](deploy/README.md). Release history: [`CHANGELOG.md`](CHANGELOG.md).

---

## Production instance

| Instance | Role |
|----------|------|
| `vps-prod` | единственный production |
| `local-dev` / Windows | development only |

---

## Out of scope / disabled stores

- CAPTCHA bypass / stealth browser / fingerprint spoofing
- Enable DNS / XCOM / Technopark без стабильного публичного source
- Marketplace (Ozon / Wildberries) как primary price source
