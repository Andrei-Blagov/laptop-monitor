# Laptop Monitor

Мониторинг цен на игровые ноутбуки (RTX 5070 Ti / RTX 5080 Laptop) с multi-store adapter architecture, матчингом моделей, ranking, Telegram alerts, remote control и optional n8n OPS webhooks.

**Version:** читается из файла `VERSION` (сейчас `0.2.0`) — единственный source of truth.
**Production target:** Linux VPS через **clean release bundle** + Docker + systemd
**Windows:** только development / testing

---

## Что умеет (v0.2.0)

| Область | Описание |
|---------|----------|
| **Multi-store** | `StoreAdapter` registry. Enabled: Regard, ANDPRO, KNS, Citilink (experimental browser) |
| **Region** | `MONITOR_REGION=moscow` — cross-store только по одной географии |
| **Price semantics** | `Product.price` = публичная цена без membership/кредита/trade-in |
| **Independent snapshots** | Успешный store сохраняется отдельно; failed ≠ «все unavailable» |
| **Fresh TOP** | Только stores с последней попыткой `ok` и age ≤ `STORE_FRESHNESS_MAX_MINUTES` (180) |
| **Matching** | Exact SKU / MPN / strong ID + config conflict; без fuzzy auto-match |
| **CROSS_STORE** | Cheapest vs second among fresh offers; metadata содержит весь список |
| **Ranking** | Explainable `priority_score`, Telegram TOP sort DESC |
| **Control bot** | Long polling; Run / TOP / Status / Version; admin allowlist |
| **Scheduler** | Primary: **systemd timer** → `run_pipeline.py`. Manual: Telegram bot |
| **n8n** | Optional OPS webhook (`pipeline.completed` + HMAC); не единственный scheduler |
| **Migration** | Явная команда `python -m scripts.migrate_db` |
| **Deploy** | Clean tar.gz bundle → `/opt/laptop-monitor` |

---

## Version (single source of truth)

```
VERSION
```

```bash
./deploy/compose.sh build
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

### Availability

- `Product.available=True` только если товар реально можно заказать
- `metadata.availability_status`: `in_stock` / `preorder` / `out_of_stock` / `display_only` / `unknown`
- Preorder ≠ обычный in_stock
- 0 products при ожидаемом ассортименте → **STORE FAILED**, не «все unavailable»

### Collection modes

- `http` — обычный HTTP/HTML/JSON
- `browser` — обычный Playwright Chromium **без** stealth / CAPTCHA solving / fingerprint spoofing / proxy rotation

---

## Matching & ranking

Приоритет идентификации: normalized SKU → MPN → alternative PN → strong ID → config compatibility.
Конфликт конфигурации блокирует плохой cluster.

Ranking (config-driven веса): public price, GPU (5070 Ti / 5080), RAM, SSD, CPU class, screen, historical low, cross-store saving.
Предпочтения: performance/cooling > вес; 17–18" желательно; 32GB RAM; SSD ≥1TB; HX CPU; ориентир ~200k.

Telegram «Топ предложений»: только fresh enabled stores, score DESC, без дублей одной physical model, без stale cheapest.

---

## Fresh TOP semantics

Store **fresh**, если:

1. самая последняя запись в `store_runs` имеет `status=ok`;
2. `finished_at` не старше `STORE_FRESHNESS_MAX_MINUTES` (по умолчанию 180 мин).

Stale не участвуют в TOP / cross-store saving. Нет fresh → «Нет свежих данных. Запустите проверку.»

---

## n8n (OPS layer)

Primary schedule остаётся **systemd**. n8n — event receiver / digest / failure workflows / analytics.

```
N8N_WEBHOOK_URL=
N8N_WEBHOOK_SECRET=
N8N_WEBHOOK_TIMEOUT_SECONDS=8
```

Пустой URL → integration disabled. Ошибки webhook (timeout/5xx/DNS) **не** меняют pipeline status.

HMAC-SHA256: `X-Laptop-Monitor-Timestamp` + `X-Laptop-Monitor-Signature` over `timestamp + "." + raw_body`.

Документация и JSON skeletons: [`deploy/n8n/README.md`](deploy/n8n/README.md).

**Не** монтировать `/var/run/docker.sock` в n8n. **Не** давать n8n shell control VPS.

---

## Database migration

```bash
python -m scripts.migrate_db --db data/laptop_monitor.db
```

Control bot при старте проверяет schema и завершается с ошибкой, если migrate ещё не выполнен.

---

## Telegram control

Кнопки: Запустить проверку | Топ | Статус | Версия

Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_CHAT_ID`, `LAPTOP_MONITOR_INSTANCE`.

HTTP ошибки Telegram логируются без URL с token. Real alerts в этом этапе разработки не отправляются на production.

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
# -> dist/laptop-monitor-<VERSION>.tar.gz
```

### На VPS

```
/opt/laptop-monitor/
  VERSION, *.py, parsers/, stores/, integrations/, scripts/, deploy/
  .env, data/, logs/
```

```bash
./deploy/compose.sh build
python -m scripts.migrate_db --db data/laptop_monitor.db
./deploy/compose.sh up -d control-bot
./deploy/compose.sh run --rm pipeline python run_pipeline.py
```

Полная cutover-последовательность: [`deploy/README.md`](deploy/README.md).

---

## Production instance

| Instance | Role |
|----------|------|
| `vps-prod` | единственный production |
| `local-dev` / Windows | development only |

---

## Что не входит в этот этап (до cutover)

- Подключение к VPS / SCP / перенос DB
- enable systemd / tag `v0.2.0`
- Import workflow в production n8n
- Real Telegram alerts на production chat
- Обход CAPTCHA / enable DNS / XCOM / Technopark без стабильного source
