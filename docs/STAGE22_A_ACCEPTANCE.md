# Приёмка пакета 2.2-A — 14 октября 2026

**Итог:** пакет A готов к интеграции как документация согласованных интерфейсов и синтетические policy fixtures. Алгоритмы matching/cache/queue этапа 2.2 этим PR не реализованы и не приняты.

**Ветка:** feature/stage-2-2-contracts-work. **Draft PR:** [№3](https://github.com/Andrei-Blagov/laptop-monitor/pull/3). Исходный HEAD при приёмке: ac7e6e63e256193fad18c17c70a3f2532937b813. База: v0.10.2 / ff4d79889dbf78f78623fdac69734412c4bc42af.

## Проверки

| Проверка | Исходный пакет | После исправлений |
|---|---:|---:|
| unittest пакета A | 8 passed | 26 passed |
| Полный unittest discover | 940 passed | 958 passed, 0 failures/errors/skips |
| compileall | — | Успешно; 154 Python-файла |
| git diff --check | — | Успешно |
| Чувствительность к искажению предпосылок | — | 5/5 ошибочных вариантов отклонены |

Команды: `python -m unittest discover -s tests -p test_stage22_contract_fixtures.py -v`, затем `python -m unittest discover -s tests -v`, затем `python -m compileall -q .`. Отдельно проверены изменение MPN в R1, устранение конфликта разрешения в C3, другая generation в M2, устаревшее наличие в F1 и потеря адресата Q1: каждый вариант приводит к отказу соответствующей проверки.

Среда: Python 3.12.14, отдельное окружение с зависимостями requirements.txt (включая точные pins). HTTP-магазинов, ЦБ и Telegram не выполнялись; сетевые сокеты запрещены внешним test harness, реальные Telegram/n8n credentials из окружения тестов удалены. Счётчик попыток реальных сетевых обращений: 0. Строки о Telegram/магазинах в unittest output относятся к mocks и временным синтетическим базам. Browser binaries и production-сессии не использовались.

## Исправленные проблемы

1. Проверки охватывали лишь часть 16 сценариев; некоторые ожидания проверялись без проверки их входных предпосылок. Добавлены отдельные проверки всех 16 кейсов, согласованные policy constants и граничные случаи. Региональный пример получил явное family/generation evidence; coalescing содержит три разных синтетических адресата.
2. Не было отдельного времени широкого discovery: incremental update мог трактоваться как новый полный каталог. Введён contract `discovery_checked_at`; detail refresh не продлевает 26h. Проверяются отдельно discovery, цена и наличие.
3. Обязательный ru_snapshot_id противоречил TH-only fallback. Разрешён null при отсутствии свежего RU; pairs пусты, country verdict отсутствует, coverage ограничено. Свежий пустой RU snapshot сохраняет настоящий ID.
4. «Неактивный источник/partial» мог запрещать свежую подтверждённую пару вопреки согласованной policy. Разделены partial scan coverage и непроверенное предложение; известная свежая пара остаётся сравнимой с оговоркой покрытия.
5. Уточнены ID связей/indexes, nullable match_class при incomplete/conflict, derived unmatched status, machine bands, 180m RU TTL, 6h/26h/36h включительные границы, UTC/future guards, календарная дата FX Europe/Moscow и non-finite money.
6. Добавлена перепроверка свежести перед повторной доставкой: истёкший verdict нельзя повторять как текущий. Исходный анализ помечен архивом; приоритет — согласованные решения и уточнённые контракты, Work A/D и Cursor B/C/E/F.
7. Явно разграничены упрощённые fixture examples и runtime payloads. `verified=true` в синтетических данных не заменяет собственные timestamps, seller/variant и field provenance в будущих serializers. VRAM-описание не вводит новый обязательный hard gate для отсутствующего поля относительно v0.10.2.

## Ограничения и передача

26 тестов A проверяют согласованность данных и policy examples. Они **не доказывают**, что новая очередь действительно объединяет события, scraper выполняет reuse или новый matching работает. Эти implementation acceptance tests относятся к B/C/D/E/F; полная матрица исходного анализа содержит 56 сценариев. Исходные 932 unittest регрессии также прошли.

Изменены только документы пакета A и его fixtures/tests. Исполняемые production-модули, VERSION, main, теги, таймеры и сервисы не изменены. Новая ветка/PR не создавались; package D не начат; merge/deploy не выполнялись.

Следующий координационный шаг — интеграция принятых контрактов в рабочие пакеты B/C по отдельной постановке. Приёмка A не является разрешением на merge, release или production deployment.
