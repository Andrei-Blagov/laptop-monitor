# Laptop Monitor — передача Work и независимая приёмка F2

Дата передачи: 15.10.2026. Репозиторий `Andrei-Blagov/laptop-monitor`.
Рабочая ветка `feature/stage-2-2-runtime-acceptance`, существующий Draft PR №12.
Исходный HEAD: `a286477d3b0c74d0d2aeefdca9fed24e0a37bd15`.
[Решение владельца](https://github.com/Andrei-Blagov/laptop-monitor/pull/12#issuecomment-6194956542).

## Действующий порядок

ChatGPT Work самостоятельно выполняет разработку, адресные регрессии, итоговые
проверки, коммиты, GitHub и подготовку развёртывания. Cursor больше не участвует.
Координационный чат принимает продуктовые решения и итоговую приёмку. Новая
ветка разговора не создаёт новую Git-ветку. Пакеты A–F2 повторно не реализуются.

Сначала сверять remote HEAD, локальный HEAD и `git status`. Не перезаписывать
чужие изменения. Исправления собирать по связанным дефектам; после изменения
гонять адресные тесты, на окончательном HEAD — полный проход. Записывать отдельно
доказанные результаты и недоступные проверки. Release/merge/deploy, изменение
VERSION, main, тегов, production-флага и таймеров требуют отдельного решения.
Никаких живых магазинов, ЦБ или Telegram в текущей приёмке.

## Проверенная среда и доступ

Локально: Linux, Python 3.12.14, git; зависимости установлены из
`requirements.txt` в отдельный venv. До установки исходный Python не содержал
httpx. Docker/Podman и Docker socket отсутствуют. Создание собственного сетевого
namespace недоступно (`unshare: Operation not permitted`). Это не препятствует
офлайн-проверкам: Python audit hook запрещает IP-соединения и DNS, включая
дочерние Python-процессы с унаследованным PYTHONPATH.

GitHub: чтение PR/комментариев/ветки, запись blobs/tree/commit и обновление
существующей ветки проверены. Прямая отправка `git push` из shell не имеет
учётных данных; публикация выполнена подключённым GitHub API с проверкой
expected HEAD и сравнением удалённого дерева с локальным `git write-tree`.
Секретные значения не читались и не сохранялись. Новую ветку/PR не создавали.

GitHub Actions реально работает. До передачи workflow/run отсутствовали.
Первый прогон исправлений `2d5201440bf01326bec9ed5f90528d0562e0d502`:
[run 38757918564](https://github.com/Andrei-Blagov/laptop-monitor/actions/runs/38757918564),
оба job — success: offline и containers. Окончательный HEAD проверяется тем же
workflow повторно; источник итогового результата — Checks PR №12 и артефакты
`laptop-offline-<SHA>`, `laptop-containers-<SHA>` соответствующего SHA.
Финальный workflow явно checkout-ит PR head SHA и проверяет его равенство:
не подменяет приёмку candidate проверкой синтетического PR merge commit.
Первый прогон использовал стандартный checkout PR merge; он подтверждает
работоспособность CI, но не заменяет окончательный проход точного HEAD.

Workflow BLAGOVA_SWEETS просмотрен только как ориентир разделения проверки и
SSH-операций. Его код, workflow и секреты не изменялись и не переносились.

## Воспроизводимые команды

Из чистого checkout этой ветки, Python 3.12, без `.env` и реальных токенов:

```bash
python3.12 -m venv /tmp/laptop-monitor-check
/tmp/laptop-monitor-check/bin/python -m pip install -r requirements.txt
/tmp/laptop-monitor-check/bin/python -m scripts.check_offline --tests tests.test_f2_handoff tests.test_stage22_runtime tests.test_market_events tests.test_market_telegram tests.test_systemd_units
/tmp/laptop-monitor-check/bin/python -m scripts.check_offline
```

Последняя команда выполняет полный unittest, отдельные 15 packaging-проверок,
compileall по отслеживаемым Python-файлам, `git diff --check HEAD`, разбор восьми
F2 systemd units, сборку bundle и release_verify bundle/извлечённого дерева с
импортами. Логи и JSON с HEAD, dirty-state, Python, командами и exit codes —
`dist/checks/`. Сеть для тестов блокирует `tests/offline_guard/sitecustomize.py`;
переменные proxy и внешние Telegram/n8n credentials удаляются из окружения
тестового процесса. Тесты используют свои фиктивные значения и MockTransport.
Установка зависимостей и сборка образа требуют сети, сами runtime-прогоны — нет.

Контейнерная приёмка на машине с Docker (VERSION не меняется):

```bash
python -m scripts.build_deploy_bundle
version=$(cat VERSION)
mkdir -p dist/context
tar -xzf "dist/laptop-monitor-${version}.tar.gz" -C dist/context
docker build --build-arg "APP_VERSION=${version}" -t "laptop-monitor-ci:${version}" -f "dist/context/laptop-monitor-${version}/deploy/Dockerfile" "dist/context/laptop-monitor-${version}"
python -m scripts.check_containers --image "laptop-monitor-ci:${version}"
```

Образ собирается из чистого bundle. Проверяются его manifest, VERSION и импорты.
Контейнеры приёмки имеют `--network none`, отдельные PID namespaces, отдельный
одноразовый общий volume и marker `f2-container-test`; host `.env` и рабочие
тома не монтируются. Проверяются живые scan/delivery leases, присоединение
второго события без второго scan/send, SIGKILL владельца, возврат обоих событий,
один общий результат/доставка и пустое повторное пробуждение. Отдельная фаза
проверяет живого владельца доставки и восстановление доставки после SIGKILL.
Volume и контейнеры удаляются; отчёт хранит HEAD и ID образа. Без Docker команда
возвращает код 2 и `unavailable`, не PASS.

`check_systemd` использует временный корень с оригинальными unit-файлами и
заглушками Docker/boot targets/compose executable. Это проверка синтаксиса и
зависимостей, не запуск служб и не доказательство серверных прав. Нативная
проверка без временного корня здесь закономерно видит отсутствующие docker.service
и `/opt/laptop-monitor/deploy/compose.sh`; это ограничение среды.

## CI без production-доступа

`.github/workflows/checks.yml` запускается на pull_request, push рабочей ветки
и workflow_dispatch. Workflow публикует только артефакты проверок; прав записи
нет (`contents: read`), checkout не сохраняет credentials, environment/secrets,
SSH, deploy, release/tag и вызовов production нет. Контейнерный job начинается
только после успешного offline job. Для обычных проверок владелец не должен
добавлять ни один production secret. Создание и запуск через push уже доказаны;
ручной workflow_dispatch через API этой среды отдельно не проверялся.

## Найденные дефекты и исправления

1. Карточки объединялись по тексту и regex `SKU-*`: обычный артикул вроде
   `G615LR-S5348` повторялся в разных разделах, а разные тайские конфигурации с
   одинаковой надписью SKU скрывались. Теперь ключ — структурированный variant ID
   D, с безопасным fallback к offer ID. Ядро D не менялось.
2. После успешной отправки группы и архивирования первого события авария могла
   оставить остальные без его ACK и повторить доставку. Добавлен атомарный
   журнал `receipts/<hash(result_id, view)>.json`, записываемый после каждого
   подтверждённого фрагмента до следующего send/архивирования. Получатели входят
   в part IDs, представления имеют разные журналы; события сохраняются отдельно.
3. После fork дочерний процесс наследовал Python-счётчик и ошибочно считал lease
   своим. Теперь ребёнок закрывает унаследованные копии дескрипторов без LOCK_UN,
   очищает локальное владение и действительно проверяет kernel lock родителя.
4. При обратном порядке освобождения вложенных lease терялся держатель
   дескриптора. Теперь дескриптор и refcount хранятся вместе до последнего
   release; путь нормализован, повторный acquire одного объекта идемпотентен.
5. Пустой stream мог терять сведения о неполном покрытии. Они сохраняются в
   сообщении. Длинные ошибки/coverage разбиваются на части с резервом для
   актуальной строки свежести. Части и датированные исторические состояния
   остаются различимыми при повторе.
6. Один тест BUY неявно зависел от локальных Telegram credentials. Адресат теперь
   задан явно фиктивным значением, без необходимости production `.env` в CI.
7. Runbook отката не перекрывал все входы очереди и не учитывал delivery/delivering.
   Процедура уточнена: приостановить producers, дождаться владельцев, завершить
   очередь по not_before и только затем переключить флаг/восстановить старый путь.

Новые регрессии сначала воспроизвели пять ошибок старого candidate. После
исправления: 1176 полных Linux unittest без ошибок/пропусков; packaging 15;
compileall, diff, release_verify bundle и дерева — PASS. Восемь unit-файлов
прошли изолированный systemd-analyze verify. Первый CI также подтвердил сборку
образа и настоящий межконтейнерный scan/recovery. Расширенная финальная фаза
delivery проверяется CI того HEAD, который передаётся координационному чату.

## Матрица F2 и границы доказательств

| Область | Воспроизводимая проверка | Граница |
|---|---|---|
| Межпроцессный scan, подписчики, восстановление | test_market_events, test_f2_handoff | Linux процессы, включая fork/spawn |
| Межконтейнерный scan и delivery | check_containers, CI containers | Настоящие containers/volume, синтетические события, network none |
| Частичный отказ/503/429/Retry-After | test_stage22_runtime, test_thai_catalog_refresh/reuse, test_market_events | MockTransport, без магазинов |
| Сохранение last-good/дат | test_stage22_runtime, test_thai_catalog | Файлы временных fixtures |
| Общая доставка daily/manual/BUY; адресаты/представления | test_market_events, test_market_telegram | Подставной sender/HTTP transport |
| Перезапуск после частичной доставки/архивирования | test_market_telegram, test_f2_handoff | ACK сохранён до следующей части |
| Сообщения, freshness, FX, реальные SKU | test_market_telegram, test_f2_handoff, test_stage22_runtime | Тексты, а не реальные Telegram API |
| 08:00 Москва, deferred/retry без цикла | test_stage22_runtime, test_market_events, check_systemd | Синтетические часы и подготовленные units |
| Флаг on/off, старый personal путь | test_stage22_runtime, test_market_telegram | Только тестовое окружение, без production |
| Включение/откат на VPS | Runbook F2 reviewed | **Не выполнены на сервере** |

## Доступ к Laptop Monitor VPS и действия владельца

SSH-клиент здесь есть, но для Laptop Monitor нет настроенного подключения:
нет проектного host/port/user, SSH identity/agent/config и проверенного known_hosts.
Сетевой/SSH-доступ к этому VPS не подтверждён. Наличие доступа BLAGOVA_SWEETS
ничего о Laptop Monitor не доказывает. Неизвестно, настроены ли отдельные
серверные secrets в GitHub: их значения и чужие credentials не запрашивались.

Для будущего серверного этапа владелец настраивает **отдельный** GitHub Environment
`laptop-monitor-production`, required reviewers и доступ только для согласованной
ветки/релиза. В его интерфейсе хранить:

- `LAPTOP_MONITOR_SSH_HOST`, `LAPTOP_MONITOR_SSH_PORT`, `LAPTOP_MONITOR_SSH_USER`;
- `LAPTOP_MONITOR_SSH_PRIVATE_KEY` — отдельную identity, не ключ другого проекта;
- `LAPTOP_MONITOR_SSH_KNOWN_HOSTS` — запись с отпечатком, проверенным владельцем
  по независимому каналу, не результат слепого ssh-keyscan во время подключения;
- variable `LAPTOP_MONITOR_DEPLOY_DIR=/opt/laptop-monitor` после подтверждения пути.

Секретные значения не отправлять в чат. Нужен ограниченный системный пользователь
для этого проекта; доступ к Docker фактически даёт root-права и отдельно
согласуется владельцем. Существующие секреты/окружение другого проекта не копировать.

В этой поставке SSH workflow и deploy не добавлены: тестовый CI не ссылается на
это Environment. Следующий согласованный шаг — отдельный ручной read-only audit
под его protection: BatchMode, IdentitiesOnly, StrictHostKeyChecking=yes,
проверка identity, `/opt/laptop-monitor`, версии, Docker/Compose и статусов
только относящихся к проекту служб. Не читать `.env`/полное окружение контейнеров,
не печатать ключи, не менять службы/флаг/таймеры. Deploy/включение — после новой
постановки и отдельного решения владельца. Офлайн-проверки не зависят от этого.

## Оставшиеся ограничения

Процесс может умереть после успешного Telegram HTTP и до записи ACK в receipts;
эта последняя часть может повториться. True exactly-once без серверного
idempotency key Telegram не заявляется. Журнал закрывает дополнительные окна
частичного архивирования, но не это внешнее окно. ACK-файлы не следует удалять,
пока на результат ссылаются события; автоматическая очистка receipts здесь не
добавлена. Проверка восстановления относится к смерти процессов/контейнеров,
не к отключению питания или повреждению тома.

Ни живой первый запуск, ни установка/enable/rollback на VPS не проводились.
TimeoutStartSec=300 остаётся прежним. Требования, рейтинг/BUY России и ядро D
не менялись. Main, VERSION=0.10.2, production-флаг, теги и работающие таймеры
не изменялись; PR не объединён. Готовность — к координационной приёмке F2 по
доказательствам окончательного HEAD, без автоматического разрешения production.
