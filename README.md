# Laptop Monitor

Мониторинг цен на игровые ноутбуки (RTX 5070 Ti / RTX 5080 Laptop) с multi-store adapter architecture, матчингом моделей, ranking, Telegram alerts и remote control.

**Version:** читается из файла `VERSION` (сейчас `0.2.0`) — единственный source of truth.
**Production target:** Linux VPS через **clean release bundle** + Docker + systemd
**Windows:** только development / testing

---

## Что умеет (v0.2.0)

| Область | Описание |
|---------|----------|
| **Multi-store** | `StoreAdapter` registry. Enabled: Regard, ANDPRO. DNS/Citilink registered, **disabled** |
| **Independent snapshots** | Успешный store сохраняется отдельно; failed не делает каталог unavailable |
| **Fresh TOP** | TOP / current deals только по stores с последней попыткой `ok` и age <= `STORE_FRESHNESS_MAX_MINUTES` (180) |
| **Matching** | L1 SKU + L2 strong ID + config conflict; N магазинов |
| **CROSS_STORE** | Только между fresh stores текущего pipeline run |
| **Ranking** | Explainable `priority_score`, Telegram sort DESC |
| **Control bot** | Long polling; Run / TOP / Status / Version; admin allowlist; **не** мигрирует schema |
| **Migration** | Явная команда `python -m scripts.migrate_db` |
| **Deploy** | Clean tar.gz bundle -> `/opt/laptop-monitor` (без `.git` / tests) |

---

## Version (single source of truth)

Файл:

```
VERSION
```

Docker / compose **не** содержат захардкоженный номер:

```bash
./deploy/compose.sh build
./deploy/compose.sh run --rm pipeline python run_pipeline.py --version
```

`deploy/compose.sh` читает `VERSION`, экспортирует `LAPTOP_MONITOR_VERSION`, передаёт `APP_VERSION` в Docker build args.

---

## Stores

| Store | Status | Notes |
|-------|--------|-------|
| Regard | enabled | HTML catalog |
| ANDPRO | enabled | HTML catalog |
| DNS | disabled | Live: HTTP 401 challenge / API 403 |
| Citilink | disabled | Live: HTTP 429 JS challenge |

DNS/Citilink **не** блокируют v0.2.0 cutover. Включение — отдельный этап (официальный feed/API), без обхода anti-bot.

---

## Fresh TOP semantics

Store **fresh**, если:

1. самая последняя запись в `store_runs` имеет `status=ok`;
2. `finished_at` не старше `STORE_FRESHNESS_MAX_MINUTES` (по умолчанию 180 мин).

Если последняя попытка **failed** — store stale для TOP (даже при молодом старом success).

Stale данные остаются в DB/history, не помечаются unavailable, но **не** участвуют в TOP и в отображаемом cross-store saving.

Нет fresh stores -> «Нет свежих данных. Запустите проверку.»

---

## Database migration

```bash
python -m scripts.migrate_db --db data/laptop_monitor.db
```

Только schema / `init_db` + `PRAGMA integrity_check`.
Не собирает магазины, не создаёт alerts, не шлёт Telegram.

Control bot при старте **проверяет** schema и завершается с ошибкой, если migrate ещё не выполнен.

---

## Telegram control

Кнопки: Запустить проверку | Топ | Статус | Версия

Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_CHAT_ID`, `LAPTOP_MONITOR_INSTANCE`.

HTTP ошибки Telegram логируются без URL с token.

---

## Dev quick start

```bash
python -m venv .venv
# activate
pip install -r requirements.txt
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
# -> dist/laptop-monitor-<VERSION>.tar.gz.sha256
```

### На VPS

```
/opt/laptop-monitor/
  VERSION, *.py, parsers/, stores/, scripts/, deploy/
  .env, data/, logs/
```

Без `.git`, tests, Windows scripts.

```bash
./deploy/compose.sh build
python -m scripts.migrate_db --db data/laptop_monitor.db
./deploy/compose.sh up -d control-bot
./deploy/compose.sh run --rm pipeline python run_pipeline.py
```

Полная cutover-последовательность: [`deploy/README.md`](deploy/README.md).

Backup:

```bash
python -m scripts.backup_db
# или
./deploy/compose.sh run --rm backup
```

---

## Production instance

| Instance | Role |
|----------|------|
| `vps-prod` | единственный production |
| `local-dev` / Windows | development only |

---

## Что не входит в pre-cutover hardening

- Подключение к VPS / SCP / перенос DB
- enable systemd / tag `v0.2.0`
- Обход CAPTCHA / включение DNS/Citilink
- Изменения n8n / Caddy / других контейнеров
