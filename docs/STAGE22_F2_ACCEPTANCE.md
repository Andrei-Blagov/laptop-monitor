# Этап 2.2-F2 — расписание и изолированная приёмка

**Независимая приёмка после передачи Work:** [STAGE22_WORK_HANDOFF.md](STAGE22_WORK_HANDOFF.md). Исходный отчёт ниже описывает проверенный Cursor candidate `a286477`, а не автоматически приёмку любого последующего HEAD. Текущие результаты CI и исправления приведены в новом отчёте.

Новый рейтинг, очередь и сканер не создавались. Ежедневный запуск, кнопка, personal и BUY ставят события в очередь E. Worker по-прежнему вызывает `evaluate_catalog_reuse` и `execute_catalog_plan`.

Флаг `STAGE22_MARKET_TELEGRAM` по умолчанию выключен. Таймеры на VPS этим пакетом не устанавливались и не включались. `VERSION` остаётся 0.10.2.

## Кто пишет курс

`FX_OBSERVATION_PATH` (`data/fx_thb.json`) создаёт и обновляет `thailand.fx.refresh_fx_observation`. Его вызывает `python -m thailand.worker`, когда флаг включён и в очереди есть работа. Внутри используется существующий `fetch_cbr_thb_rate`, второго клиента ЦБ нет. Дата публикации ЦБ `ДД.ММ.ГГГГ` записывается как ISO, иначе D не считает курс пригодным.

Неудачный запрос оставляет прежний файл. Пустой или старый курс не даёт текущую выгоду. Пробуждение пустой очереди ЦБ не вызывает.

## Точки запуска

Файлы лежат в репозитории и не включены:

- `laptop-monitor-market-daily.timer` — `*-*-* 08:00:00 Europe/Moscow`, команда `python -m thailand.market_schedule enqueue-daily`. До 08:00 событие лежит в `deferred/`. При выключенном флаге команда ничего не ставит.
- `laptop-monitor-market-retry.timer` — `*-*-* *:0/15:00`, команда `python -m thailand.worker --once`. Это не path-юнит и каталог `deferred/` не смотрит. Пока `not_before` в будущем, пробуждение не вызывает магазины и не крутит цикл.
- Существующий `laptop-monitor-thailand.path` по-прежнему смотрит только `pending/`.
- `python -m personal_top enqueue` при включённом флаге ставит `personal_top3` в ту же очередь и не запускает отдельный тайский сбор. При выключенном флаге остаётся прежняя job v1. Текст unit-файла personal TOP-3 не менялся.

## Объединение доставок

Один проход по-прежнему обслуживает все события. Одинаковые представление и результат отправляются один раз каждому получателю: daily, кнопка и BUY не теряются и все помечаются доставленными. Разные получатели получают сообщение каждый. Другое представление, например personal TOP-3, не подменяется общим.

Повтор доказанной тайской конфигурации в разных разделах не шлётся отдельной карточкой. Идентичность задаётся структурированным variant ID ядра D, а не текстом или совпавшим SKU. Разные конфигурации с одинаковой надписью SKU сохраняются раздельно. Строка выгоды входит в карточку пары.

## Сообщение одного запроса

Прогон 2026-10-15, временный каталог, MockTransport. Daily, кнопка и BUY, один проход, магазины повторно не вызывались. Отправлено **1** сообщение на всех троих, не 9 и не 27. Все три события сохранены.

Россия ↔ Таиланд. ASUS ROG Strix SKU-MATCH17. Подтверждено: та же конфигурация и полный артикул. Цена TH: 80000 ฿ (200000.0 ₽). Цена RU: 250000 ₽. Магазины jib и regard. Разрешение 2560x1600. Неизвестные расходы не равны нулю: customs, delivery, exchange_spread, travel. Курс: 2.5 ₽ за 1 ฿, CBR. Другая конфигурация той же модели указана в этой же карточке, не второй. Подтверждённая разница: Таиланд дешевле России, 50000.0 ₽, 20.0%, заметное преимущество.

Полный отказ магазинов, все HTTP 503: **1** сообщение «Обновление не удалось.», не «подходящих моделей нет». В сохранённом результате есть `failed`, причины и покрытие. Прежний снимок и его даты не переписываются. Успешный пустой поиск остаётся фразой «По сохранённому результату показывать нечего».

Устаревшая котировка и 429 на повторной проверке JIB не содержат «Таиланд дешевле». Состояние: «Эта пара не текущая». Повторное пробуждение магазины не вызывает.

## Готовность первого запуска

Код и unit-файлы подготовлены. Первый боевой запуск не выполнялся: флаг выключен, новые таймеры не установлены, production `.env` и рабочие тома не использовались. До команд ниже VPS продолжает старый путь.

## Будущее включение

На сервере, после отдельного развёртывания этого дерева. Эти команды в этом пакете не выполнялись.

Сначала дождаться, пока текущий worker отпустит захват. Не останавливать контейнер посреди сбора: `scan.lease` на общем томе держит и `laptop-monitor-thailand.service`, и `laptop-monitor-market-retry.service`.

```
docker ps --filter name=laptop-monitor --format '{{.Names}} {{.Status}}'
# пусто или контейнер worker уже завершился; active_scan.json нет, либо lease свободен
```

Флаг читается при старте контейнера. Правка `.env` у уже запущенного control-bot сама не применяется. После записи флага контейнер нужно пересоздать и проверить его фактическое окружение:

```
# в .env
STAGE22_MARKET_TELEGRAM=1

cd /opt/laptop-monitor
sudo ./deploy/compose.sh up -d --force-recreate --no-deps control-bot
docker inspect laptop-monitor-control --format '{{range .Config.Env}}{{println .}}{{end}}' | grep STAGE22_MARKET_TELEGRAM
```

Ожидаемая строка: `STAGE22_MARKET_TELEGRAM=1`. Если её нет, бот ещё на старом окружении и кнопки не переведены.

Затем таймеры, не трогая уже отложенные события. Они остаются в `deferred/` и просыпаются одним общим проходом, без второго сбора:

```
sudo cp deploy/systemd/laptop-monitor-market-daily.service /etc/systemd/system/
sudo cp deploy/systemd/laptop-monitor-market-daily.timer /etc/systemd/system/
sudo cp deploy/systemd/laptop-monitor-market-retry.service /etc/systemd/system/
sudo cp deploy/systemd/laptop-monitor-market-retry.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now laptop-monitor-market-daily.timer
sudo systemctl enable --now laptop-monitor-market-retry.timer
```

Старый `laptop-monitor-personal-top.timer` можно оставить включённым. При флаге 1 его команда ставит событие в ту же очередь и не начинает отдельный обход. Каталог `deferred/` в path-юнит не добавлять.

## Откат

Снова не посреди живого сбора. До опустошения очереди приостановить все её входы: market daily, personal timer, российский pipeline timer (BUY) и control-bot (manual). Сохранить их исходное состояние для восстановления; дождаться уже запущенных pipeline/personal/worker. Одной остановки новых таймеров недостаточно: действующие personal/BUY/manual могут снова пополнить очередь. Это будущая процедура после отдельного разрешения, здесь она не выполнялась.

```
sudo systemctl disable --now laptop-monitor-market-daily.timer
sudo systemctl disable --now laptop-monitor-market-retry.timer
sudo systemctl stop laptop-monitor-personal-top.timer laptop-monitor.timer
sudo systemctl stop laptop-monitor-control.service
# дождаться, пока контейнер worker завершится и scan.lease свободен
# проверить pending/, processing/, deferred/, delivery/, delivering/
# при флаге 1 завершить оставшиеся события одним worker после not_before
# не удалять очередь/результаты/receipts и не обходить Retry-After

# в .env
STAGE22_MARKET_TELEGRAM=0

cd /opt/laptop-monitor
sudo ./deploy/compose.sh up -d --force-recreate --no-deps control-bot
docker inspect laptop-monitor-control --format '{{range .Config.Env}}{{println .}}{{end}}' | grep STAGE22_MARKET_TELEGRAM
sudo systemctl daemon-reload
```

Ожидаемая строка после отката: `STAGE22_MARKET_TELEGRAM=0`, либо переменная отсутствует. Только после этого `laptop-monitor-personal-top.timer` снова ставит job v1 и является единственным ежедневным тайским обходом. `laptop-monitor-thailand.path` не удаляется.

Вернуть исходные состояния control service, pipeline timer и personal timer только после проверки флага и пустой рыночной очереди. Если очередь не удалось безопасно завершить, остановиться до переключения флага; сроки Retry-After не сокращать.

## Ограничения

Авария между успешным HTTP и записью части на диск по-прежнему может повторить одну часть. Это окно не закрыто. Повтор доставки после ошибки ждёт `not_before` (30 минут) и ближайшее 15-минутное пробуждение; немедленного цикла нет. Тяжёлый проход по-прежнему ограничен `TimeoutStartSec=300` существующего worker. Серверная установка и выпуск в этот пакет не входят.
