# Laptop Monitor

Мониторинг цен на игровые ноутбуки (Regard + ANDPRO) с матчингом одной физической модели между магазинами, генерацией alert-событий и доставкой в Telegram.

**Статус:** collection → matching → alerts → Telegram delivery (с grouping и rate limits).  
Scheduler / n8n / третий магазин / единый `run_pipeline.py` — пока **не** входят в scope.

---

## Что умеет

| Этап | Описание |
|------|----------|
| **Collection** | Парсинг Regard и ANDPRO (целевые GPU: RTX 5070 Ti / RTX 5080) |
| **Storage** | SQLite: товары, история цен, strong identifiers, alert events, deliveries |
| **Matching** | L1 по SKU + L2 по strong ID (MPN / alternative PN) с проверкой конфигурации |
| **Alerts** | `PRICE_DROP`, `NEW_HISTORICAL_LOW`, `TARGET_PRICE`, `CROSS_STORE_SAVING` |
| **Noise filter** | Исторический минимум только при ≥2% **или** ≥3000 ₽; приоритет PRICE_DROP |
| **Telegram** | Группировка уведомлений по matched model, pacing ~1.1 с, обработка 429 `retry_after` |

---

## Архитектура

```
Regard ──┐
         ├──► SQLite (products, price_history, identifiers)
ANDPRO ──┘              │
                        ▼
                 match_products()
                        │
                        ▼
                   alert engine
                        │
                        ▼
              alert_events (audit, по одному)
                        │
                        ▼
         notification grouping (только delivery layer)
                        │
                        ▼
              Telegram sendMessage + delivery state
```

**Важно:** `alert_events` в БД не сливаются. Grouping происходит только при формировании Telegram-сообщений. Связь delivery ↔ events — через таблицу `notification_delivery_events`.

---

## Требования

- Python 3.11+ (рекомендуется 3.12+)
- Windows / macOS / Linux
- Telegram Bot Token и Chat ID (для реальной доставки)

---

## Быстрый старт

```bash
# 1. Клонировать и окружение
git clone https://github.com/Andrei-Blagov/laptop-monitor.git
cd laptop-monitor

python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt

# 2. Секреты Telegram (не коммитить)
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
```

Заполните `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

> `TELEGRAM_CHAT_ID` должен быть ID **чата/пользователя**, куда бот может писать (не ID другого бота).

---

## Типичный рабочий цикл

Команды запускаются **по отдельности** (единого pipeline пока нет).

### 1. Сбор цен

```bash
python main.py
```

Сохраняет офферы Regard + ANDPRO в `data/laptop_monitor.db`, пишет `data/last_run.json`.

### 2. Синхронизация идентификаторов

```bash
python identity_sync.py
```

Достаёт/нормализует strong identifiers для матчинга.

### 3. Сравнение магазинов (отчёт)

```bash
python compare.py
```

Пишет `data/comparison.json` и сводку matched / unmatched.

### 4. Alert engine

```bash
python monitor.py
python monitor.py --deals   # текущие выгодные офферы без создания alerts
```

Создаёт новые `alert_events` с дедупликацией по `dedupe_key`.

### 5. Telegram delivery

```bash
# Статус очереди (без отправки)
python deliver.py --status

# Предпросмотр с той же grouping logic (без отправки и без мутации DB)
python deliver.py --dry-run

# Тестовое сообщение (проверка токена/чата)
python deliver.py --test

# Реальная отправка unsent alerts
python deliver.py

# Одноразово: пометить уже существующие events как skipped (baseline)
python deliver.py --baseline-existing
```

**Рекомендация перед первой реальной отправкой:** всегда смотреть `--status` и `--dry-run`.

---

## Типы alert events

| Тип | Когда |
|-----|--------|
| `PRICE_DROP` | Падение цены ≥ `PRICE_DROP_PERCENT` (по умолчанию 5%) |
| `NEW_HISTORICAL_LOW` | Новый минимум истории, если drop ≥ 2% **или** ≥ 3000 ₽; не дублируется отдельным сообщением, если уже есть `PRICE_DROP` с `is_historical_low` |
| `TARGET_PRICE` | Цена ≤ порога для GPU (`RTX 5070 Ti` → 230 000 ₽, `RTX 5080` → 300 000 ₽) |
| `CROSS_STORE_SAVING` | Разница между магазинами ≥ 10 000 ₽ на matched model |

Пороги настраиваются в `config.py`.

---

## Matching моделей

1. **L1 — SKU:** нормализованный артикул совпадает между магазинами.
2. **L2 — strong ID:** `manufacturer_part_number` / `alternative_part_number` / связанные идентификаторы.
3. **Config check:** при strong-ID матче сверяются GPU/CPU/RAM/SSD и т.п.; конфликт конфигурации → не match.

Группировка Telegram использует **matched model key** из comparison (`matched_identifier` / `normalized_sku`), а не «сырой» текст названия.

---

## Telegram: grouping, pacing, 429

### Grouping (delivery layer)

Если в unsent batch для одной matched model есть, например:

- `PRICE_DROP` Regard  
- `PRICE_DROP` ANDPRO  
- `CROSS_STORE_SAVING`  

→ **одно** сообщение `PRICE UPDATE` (с блоками магазинов, note «Новый исторический минимум», cross-store блоком и URL).

Правила:

- `CROSS_STORE` без `PRICE_DROP` той же модели → отдельное сообщение.
- `TARGET_PRICE` не теряется: либо секция в PRICE UPDATE той же модели, либо standalone.
- `alert_events` в БД остаются отдельными; связь через `notification_delivery_events`.

### Pacing

Между последовательными `sendMessage` в один chat — минимум ~**1.1 с** (`TELEGRAM_MIN_SEND_INTERVAL_SECONDS`). Sleep после последнего сообщения не делается.

### Flood control (429)

Если Telegram вернул `error_code=429` и `parameters.retry_after`:

1. ждём **именно** `retry_after`;
2. повторяем отправку;
3. каждая попытка увеличивает `attempts` в delivery state;
4. при превышении `MAX_DELIVERY_ATTEMPTS` (5) → `failed`;
5. token в логи не попадает.

### Delivery statuses

| status | смысл |
|--------|--------|
| `pending` | создан, ещё не успешно отправлен |
| `sent` | успешно; `provider_message_id` сохранён |
| `failed` | ошибка, можно retry пока `attempts < MAX` |
| `skipped` | baseline / сознательно не слать |

---

## Схема SQLite (ключевые таблицы)

| Таблица | Назначение |
|--------|------------|
| `products` | Текущие офферы по магазинам |
| `price_history` | История цен / availability |
| `product_identifiers` | Strong IDs для матчинга |
| `alert_events` | Бизнес/audit события (не группируются в БД) |
| `notification_deliveries` | Одна Telegram-попытка/сообщение (+ `provider_message_id`) |
| `notification_delivery_events` | M:N связь delivery ↔ alert_events |

Путь БД по умолчанию: `data/laptop_monitor.db` (в git не коммитится).

---

## Структура проекта

```
laptop-monitor/
├── main.py                 # сбор Regard + ANDPRO → SQLite
├── identity_sync.py        # синхронизация identifiers
├── compare.py              # отчёт матчинга
├── monitor.py              # alert engine
├── deliver.py              # Telegram delivery CLI
├── alerts.py               # создание / дедуп alert_events
├── comparison.py           # match_products
├── notification_groups.py  # grouping для delivery
├── notifications.py        # HTML-форматтеры сообщений
├── telegram_sender.py      # Bot API client + pacing
├── storage.py              # SQLite + миграции
├── config.py               # пороги и Telegram settings
├── models.py
├── enrichment.py
├── product_identity.py
├── target_gpu.py
├── parsers/
│   ├── regard.py
│   └── andpro.py
├── scripts/
│   ├── rebuild_unsent_alerts.py
│   └── cleanup_test_history.py
├── tests/
├── data/                   # локальные артефакты (.gitkeep)
├── .env.example
├── requirements.txt
└── README.md
```

---

## Тесты

Все тесты используют **временную** SQLite БД. Реальных HTTP-запросов в Telegram нет.

```bash
python -m unittest discover -s tests -v
```

Покрыты: парсеры, storage, matching, monitor/alerts, formatters, delivery (pacing, 429, grouping, dry-run, migration, baseline).

---

## Безопасность

- Секреты только в `.env` / переменных окружения (см. `.env.example`).
- `.env`, `.venv/`, `data/*.db` в `.gitignore`.
- В логах ошибок Telegram **не** печатается bot token.
- Не коммитьте дампы чатов и production DB.

---

## Что пока не сделано (намеренно)

- Единый `run_pipeline.py`
- Scheduler / cron / Windows Task Scheduler
- n8n
- Третий магазин
- Публичный веб-UI

---

## Troubleshooting

| Проблема | Что проверить |
|----------|----------------|
| `bot can't initiate conversation` / Forbidden | Напишите боту `/start`, проверьте `TELEGRAM_CHAT_ID` |
| `chat not found` | Неверный chat id (часто путают user id и bot id) |
| Dry-run показывает 0 сообщений | Нет unsent events — смотрите `deliver.py --status` |
| События «пропали» после baseline | `skipped` — так и задумано; новые alerts появятся после следующего `monitor.py` |
| Частые 429 | Pacing 1.1s + `retry_after`; не запускайте несколько `deliver.py` параллельно |

---

## Лицензия

Private project (все права сохранены за автором репозитория), если не указано иное.
