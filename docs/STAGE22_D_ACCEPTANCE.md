# Приёмка 2.2-D — 14 октября 2026

**Итог:** чистое ядро рейтинга и межстранового сравнения реализовано, offline-приёмка пройдена. Готово к интеграции с будущим C; production behavior пока не подключён к этим модулям.

База B: b3d585e6cd8cad277f8b04f32e1ae08a6138aaf0. Контракты A: 6362237a520260e10446f3ad3ca4bcc36f1d4b06. Ветка feature/stage-2-2-comparison-work. [Draft PR №5](https://github.com/Andrei-Blagov/laptop-monitor/pull/5), base feature/stage-2-2-ru-snapshot. PR не объединён.

## Алгоритмы

- Typed immutable snapshot/evidence adapters из данных в памяти; собственные timestamps и binding к source/external_id обязательны. Фактический B формат поддержан без изменения exporter. Future C contract и полный синтетический JSON описаны в STAGE22_D_INTERFACE.md.
- Полный independent TH eligible list, TOP-5 по international value score/confidence/цене и отдельный полный cheapest list. All offers, отказные причины и каналы сохраняются. Группировка требует доказанной одинаковой комплектации и общего полного ID всех членов; конфликтный или неполный вариант не соединяет разные модели.
- Одна full RU×TH evidence matrix без ограничения TOP-10/12; EXACT, EQUIVALENT, SAME_FAMILY, ALTERNATIVE. Подтверждённая family без generation остаётся incomplete. Региональный suffix не удаляется для EXACT. Шесть основных параметров и известные VRAM/TGP/Hz/panel conflicts проверяются; дополнительная коммерческая разница показывается отдельно.
- Цена конкретного offer, Decimal THB→RUB, только подтверждённые расходы с evidence_ref, единые bands 5/10%. Выбранная итоговая пара использует минимальную сопоставимую RU цену; нет искусственно увеличенной экономии через дорогой RU listing. Unknown costs отдельно, итог только known_costs_only. Current purchase proof невозможен для конфликта, неполных данных, старого наличия/цены и негодного FX.
- Полный unmatched TH list за пределами TOP-5, краткий value top-3 по настраиваемым score≥75 и GPU targets. Причины ограничены реально обследованным покрытием; никакого нового BUY или утверждения глобального отсутствия модели в РФ.
- Общий TOP и личный TOP-3 — membership опубликованного B snapshot. Все пользовательские views выбирают уже рассчитанные pair rows без повторной арифметики; series profile не превращается в точный SKU.

## Проверки

| Проверка | Результат |
|---|---:|
| D unittest, включая A fixture premises и реальный B export | 67 passed |
| Полный существующий unittest + D | 1 039 passed; 0 failures/errors/skips |
| Отдельный test_release_packaging | 15 passed |
| D packaging test | 4 новых модуля в bundle; их first-party import closure полностью упакован; verify_bundle успешен |
| compileall | Успешно, 161 Python-файл |
| git diff --check | Успешно |
| Static dependency boundary | 4/4 новых модулей без imports HTTP/SQLite/pipeline/scanner/storage/worker/Telegram |
| Реальные сетевые попытки во время тестов | 0 |

Среда: Python 3.12.14, отдельный venv с requirements.txt. Outbound sockets заблокированы внешним test harness; реальные Telegram/n8n credentials из окружения тестов удалены. Mock API и tempfile SQLite/JSON в existing regression tests не обращаются к production. Магазины, ЦБ, Telegram и браузеры не запускались.

Команды: `python -m unittest discover -s tests -p test_stage22_comparison.py -v`, затем `python -m unittest discover -s tests -v`, `python -m compileall -q .`, `python -m unittest discover -s tests -p test_release_packaging.py -v`, `git diff --cached --check`.

Проверены RU #17 из 25 вне TOP, независимость TH TOP, все core conflicts при одном SKU, региональная эквивалентность, CPU-class ≠ precise model, разные бренды, -wpro, Lenovo series, неизвестные/будущие или чужие spec evidence, дополнительные hardware conflicts, группа нескольких цен, отсутствие transitive/unknown bridges, стабильность ID при смене цены, Decimal budget 330 000 и тонкая дробь при низкой caller precision, quote TTL обеих осей, старое stock evidence при новом timestamp, FX nominal/датировка/ошибки, расходы по offer, неизвестные категории, unmatched за TOP-5, единая арифметика views и JSON serialization.

В настоящем синтетическом B export неизвестные captured_at остаются incomplete. После добавления **реальных для синтетического источника** field capture timestamps в fixture cache exporter B выдаёт доказательства, которые D подтверждает. Никакие времена production/cache автоматически не продлевались.

## Ограничения интеграции

C/E/F ещё не реализованы и в этом пакете не изменялись. D не выполняет сбор, refresh/reuse, очередь, Telegram или запуски systemd. Данные C должны соответствовать описанному per-offer evidence API; raw старый scan/готовые purchase_eligible flags не обходят проверки.

B не экспортирует отдельные brand/family/generation. Явный бренд собственной карточки поддержан; региональная EQUIVALENT требует дополнительного подтверждённого identity_evidence от интегратора. Unknown spec timestamp — ограничение исходных данных, не разрешение датировать его временем свежей цены. GPU/CPU marketing labels без точной модели не подтверждают равенство.

Spec confirmations относятся к наблюдённой конфигурации; отдельного времени жизни неизменных specs контракт не задаёт. Текущие цены/наличие и FX проверяются отдельно. Старый broad discovery отражается как ограниченное покрытие даже при свежем известном offer. Матрица полная, её размер O(RU×TH); масштабирование/caching относится к интеграции, сеть сама не запускается.

Российские ranking/BUY, market_snapshot.py, run_pipeline.py, russian_deals.py, TH scanner/storage/worker, очереди, Telegram, systemd, main, VERSION, теги и production не изменены. Изменения D — только новые чистые модули, tests/fixtures и документация. Следующий шаг — review и отдельная постановка интеграции C; merge/deploy/release не разрешены этой приёмкой.
