# Laptop Monitor

Мониторинг цен на игровые ноутбуки (RTX 5070 Ti / RTX 5080 Laptop) с multi-store adapter architecture, матчингом моделей, ranking, Telegram alerts и remote control.

**Version:** `0.2.0`  
**Production target:** Linux VPS (Docker + systemd)  
**Windows:** только development / testing

---

## Что умеет (v0.2.0)

| Область | Описание |
|---------|----------|
| **Multi-store** | Единый `StoreAdapter` registry (`stores/`). Enabled: Regard, ANDPRO. DNS/Citilink зарегистрированы, но **disabled** (anti-bot) |
| **Independent snapshots** | Успешный store сохраняется независимо; failed store не помечает каталог unavailable |
| **Matching** | L1 exact SKU + L2 strong ID (MPN / alt PN) + config compatibility; N магазинов |
| **CROSS_STORE** | Только между **fresh** stores текущего run (≥2 успешных) |
| **Ranking** | Explainable `priority_score` (`deal_ranking.py`), TOP N, сортировка Telegram |
| **Telegram control** | Long-polling bot: Run / TOP / Status / Version; admin allowlist |
| **Pipeline** | `run_pipeline.py`: lock, `store_runs`, `app_version`, `instance_id`, ops alerts |
| **Deploy** | Docker image, compose, systemd timer (2h), daily SQLite backup |

---

## Архитектура

```
stores/registry
  Regard ──┐
  ANDPRO ──┼──► collect (per-store) ──► store_runs + products (success only)
  DNS* ────┤
  Citilink*┘              │
                          ▼
                   identity sync
                          │
                          ▼
              match_products (N-store clusters)
                          │
                          ▼
         alerts (local + fresh-only CROSS_STORE)
                          │
                          ▼
         deal ranking → Telegram (priority DESC)
                          │
              control_bot (polling, admin only)
```

\* DNS / Citilink adapters present but `enabled=False` until a stable public source exists.

**Правила snapshot:**

| Исход store | Поведение |
|-------------|-----------|
| SUCCESS | цены + history + local alerts |
| FAILED | snapshot не трогаем; ошибка в `store_runs` |

**Pipeline status:**

- `SUCCESS` — все enabled stores OK
- `PARTIAL` — ≥1 OK и ≥1 failed (local alerts работают; cross-store только среди fresh)
- `FAILED` — ни один store не собран / критический сбой

---

## Stores

| Store | Status | Notes |
|-------|--------|-------|
| Regard | **enabled** | HTML catalog parser |
| ANDPRO | **enabled** | HTML catalog parser |
| DNS | disabled | Live: 401 challenge / API 403 |
| Citilink | disabled | Live: 429 JS challenge |
| OnlineTrade / M.Video / Eldorado / XCOM | not connected | unstable / protected in probes |

Добавление магазина: новый файл в `stores/`, реализовать `StoreAdapter`, зарегистрировать в `stores/registry.py`, fixtures + tests. Без обхода CAPTCHA/anti-bot.

---

## Ranking

Модуль `deal_ranking.py`. Веса в `config.py` (`RANK_*`).

Учитывает: цена vs target / бюджет ~200k ₽, GPU (5080 / 5070 Ti), RAM, SSD, диагональ, cross-store saving, historical low.

Каждый deal показывает score + reasons («Почему: …»).

TOP: кнопка Telegram / `format_top_deals_message` (по умолчанию 10).

---

## Telegram

### Price alerts
Группировка по matched model (delivery-layer). Сортировка групп по `priority_score` DESC. `alert_events` в DB не сливаются.

### Control bot (`control_bot.py`)
Long polling, **без** публичного HTTP-порта.

Кнопки: `Запустить проверку` | `Топ предложений` | `Статус` | `Версия`

Env:

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ADMIN_CHAT_ID=   # allowlist, comma-separated; fallback = CHAT_ID
LAPTOP_MONITOR_INSTANCE=vps-prod
```

Не-admin не видит кнопки управления и не запускает pipeline. Только predefined actions (без shell).

### Ops notifications
При CLI/scheduled `PARTIAL`/`FAILED` — короткое admin-сообщение (не путать с price alerts).

---

## Versioning

Единый source of truth: файл `VERSION` → `version.get_version()`.

```bash
python run_pipeline.py --version
# Laptop Monitor 0.2.0
```

Пишется в logs, `pipeline_runs.app_version`, Telegram status, Docker label.

---

## Быстрый старт (dev)

```bash
git clone https://github.com/Andrei-Blagov/laptop-monitor.git
cd laptop-monitor
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# Linux:   source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить токены
```

```bash
python run_pipeline.py --version
python run_pipeline.py --status
# Полный pipeline (локально, не VPS production):
# python run_pipeline.py
```

Tests (temp DB):

```bash
python -m unittest discover -s tests
```

Windows helpers (dev only): `scripts/windows/` — **не** для production, Task Scheduler не устанавливается.

---

## Production = VPS

| Instance | Role |
|----------|------|
| `vps-prod` | единственный production monitor |
| `local-dev` / Windows | development / testing only |

После cutover Windows **не** должен слать Telegram alerts (иначе duplicate из двух SQLite).

Подробности: [`deploy/README.md`](deploy/README.md).

### Docker

```
deploy/
  Dockerfile
  docker-compose.yml
  systemd/          # timer 2h, control-bot, daily backup
  README.md
```

Volumes: `data/` (SQLite + backups), `logs/`. `.env` только на хосте.

Control bot: `docker compose up -d control-bot`  
Pipeline: systemd timer → `docker compose run --rm pipeline`

### Backup

```bash
python scripts/backup_db.py
```

SQLite Online Backup API + `PRAGMA integrity_check`, retention 30 дней, каталог `data/backups/` (не в git).

---

## Migration Windows → VPS (подготовка)

1. Backup Windows DB (SQLite backup API) + SHA256
2. Transfer на VPS + verify SHA256
3. Backup на VPS
4. Schema migration (`init_db`)
5. `integrity_check`
6. Read-only `--status`
7. First **manual** VPS run
8. Verify Telegram
9. Enable systemd timer + control-bot
10. Disable Windows production monitoring

**Этот репозиторий готов к cutover; сам transfer / enable пока не выполняется.**

---

## Структура

```
stores/           # adapters + registry
parsers/          # Regard/ANDPRO HTML parsers
deal_ranking.py
control_bot.py
admin_notify.py
run_pipeline.py
scripts/backup_db.py
scripts/windows/  # local PowerShell helpers
deploy/           # Docker + systemd
VERSION
```

---

## Что не входит в этот этап

- Реальный deploy на VPS / enable systemd
- Перенос production DB
- Обход CAPTCHA / anti-bot
- Агрессивный fuzzy matching
- Marketplaces (Ozon/WB) как priority
- Изменения n8n / Caddy / других контейнеров на VPS
