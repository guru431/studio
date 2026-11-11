# Тесты

Короткая сводка и правила — [CLAUDE.md](../../CLAUDE.md#тесты); здесь — полный текст.

s06: `.venv/Scripts/python -m pytest -q -n0 tests/test_s06.py tests/test_foundation.py tests/test_connections.py tests/test_sections.py tests/test_speech.py tests/test_ocr.py`;
настройки 320/1280: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_s06.py`.

Cleaner: `.venv/Scripts/python -m pytest -q -n0 tests/test_cleaner.py`; сквозной контракт двух
приложений и браузер 320/1280: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_cleaner_contract.py tests/test_ui_cleaner.py`.
Нужен соседний `cleaner/.venv` или `CLEANER_TEST_REPO`; провайдер синтетический, внешней сети нет.

Контракт — `local-config/cron/project-registry.json::studio.tests`: `main` (корень, среда `.venv`) и `agent`
(`agent/`, своя `.venv`, [подробности](../../agent/CLAUDE.md#тесты)). Быстрый и полный уровни запускают оба набора;
нижеследующие команды относятся к `main`. Windows-зависимости моста не ставятся в среду сервера.

Решение владельца 2026-11-03: подписка Claude остановлена на неопределённый срок — работа и приёмка
идут без Claude, живые пробы Claude возобновляются только после сообщения о восстановлении доступа.
Chrome уже подключён через расширение; дополнительная настройка прямого DevTools MCP не требуется.

    .venv/Scripts/python -m pytest -q                                   # быстрый набор: бюджет 60 с, 4 процесса
    .venv/Scripts/python -m pytest -q -m integration                    # браузер (Edge/Chrome + node 22), PowerShell
    v() { grep "^$1=" C:/work/projects/local-config/secrets/vault.env | cut -d= -f2-; }    # живая приёмка: значения не печатать
    STUDIO_ADMIN_LOGIN=$(v STUDIO_ADMIN_LOGIN) STUDIO_ADMIN_PASSWORD=$(v STUDIO_ADMIN_PASSWORD) \
      GATEWAY_KEY_OWNER_IMAGES=$(v GATEWAY_KEY_OWNER_IMAGES) .venv/Scripts/python -m pytest -q -m manual

Живая приёмка (`tests/test_studio_upload_live.py`, генерирует на карте, до ~1,5 ч) — после
выкладки и `--create-admin`: входит администратором в `STUDIO_LIVE_URL` (по умолчанию
`https://studio.example.com`), делает Lustify, правку кистью (Lustify, вне маски — пиксели исходника),
описание gemma-abl, ролик 1 с, ответ `local-gemma-abl` по PDF и одностраничный скан — в разделе
«Распознавание» и вложением в чате (затем `/pages/ocr-release` — 200 и ответ gemma-abl без ожидания
аренды), «Перевод» на `local-gemma-abl` — текст, одностраничный PDF со слоем и снимок вывески (блоки с рамками,
перевод gemma-abl без ожидания аренды `ocr`, затем `/pages/ocr-release` — 200); удаляет только своё и освобождает карту. Без `STUDIO_ADMIN_*` эта часть пропускается; с `GATEWAY_KEY_OWNER_IMAGES` — ещё шлюз
напрямую (`STUDIO_LIVE_GATEWAY`) и `client/studio-batch.local.ps1`.

**`STUDIO_ADMIN_PASSWORD` в vault — пароль восстановления, не рабочий:** владелец входит своим
(решение 2026-10-20, `local-config/project-knowledge-base.yaml` → `api_keys.studio_admin`), поэтому вход
на проде паролем из vault отвечает 401 — это не сбой выкладки. Не повторять: из LAN неудачи идут
парой «example-user + 10.77.1.1», и пятая за 15 минут запрёт владельцу вход из дома. Для живой приёмки
— спросить владельца: временно вернуть пароль восстановления (`--create-admin` гасит его сессии)
или завести отдельную учётку администратора для приёмки.

Среда: `"/c/Program Files/Python314/python" -m venv .venv`, затем
`.venv/Scripts/python -m pip install -r requirements-dev.txt`. Приложение в быстрых тестах —
фикстура `studio` (`tests/conftest.py`): шлюз подменён `httpx.MockTransport` (`tests/fakes.py`),
часы — `clock.NOW`, паузы — `clock.SLEEP`. Браузерные сценарии — `tests/test_ui_browser.py`
(живая служба `tests/live_app.py`, драйвер `tests/browser.py` + `cdp_driver.mjs`).
Движок Safari проверяется отдельно в `tests/test_ui_webkit.py` (Playwright WebKit, касания, 393×852 и
852×393, также 320 px). Установка движка: `.venv/Scripts/python -m playwright install webkit`;
точечно — `.venv/Scripts/python -m pytest tests/test_ui_webkit.py -q -m integration -n0`.
Замер синтетической истории 100 сообщений/1000 шагов и Markdown на CPU ×4 — отдельно от тестов:
`.venv/Scripts/python tools/benchmark_history.py --output <report.json>`; три тёплых открытия,
393×852, без настоящих переписок, квоты и изменений работающего моста.
Медленная сеть и обрывы — `.venv/Scripts/python tools/benchmark_network.py --output <report.json>`:
изолированная база, синтетические gateway/мост, CDP-профили до 128/64 Кбит/с и 1500 мс;
`--faults-only` — потеря ответа POST, разрыв потока и offline; `--profiles slow_3g edge --loading-only` —
только загрузка. Запускать последовательно. Локальная HTTP-статика без сжатия не воспроизводит gzip Apache;
живая приёмка с авторизацией — отдельная проверка. Результаты: `docs/audits/2026-11-04-network.md`.
Без установленного движка эти проверки пропускаются. В мобильной `.code-panel` обязательно
`align-self: stretch`: WebKit при настольном `flex-start` и `height: auto` сжимает фиксированную
панель до высоты компьютеров, обрезая проекты, несмотря на заданные `top` и `bottom`.
Договорённость юнитов, vhost и скриптов выкладки сверяет `tests/test_deploy.py`. Набор идёт четырьмя процессами
(`-n 4 --dist loadgroup` в `pytest.ini`, pytest-xdist): состояние заданий — модульное, процессы
его не делят; `integration` и `manual` — группой `serial` (хук в `tests/conftest.py`), одним
процессом по очереди. Один тест без параллели — `-n0`. Срок теста (`timeout = 30`, на Windows метод `thread`)
по истечении печатает стеки и роняет процесс (`os._exit`): под xdist — «crashed while running <тест>», а
`pytest_xdist_make_scheduler` (`tests/conftest.py`) не даёт loadgroup повторять упавший тест до предела
перезапусков; под `-n0` итога нет — имя теста в стеках. `faulthandler_timeout = 20` заранее печатает стеки
всех потоков напрямую в stderr до завершения процесса на 30 с: захваченный вывод pytest-timeout под xdist
может потеряться. Тело теста и финализацию фикстуры проверяет `test_suite_timeout.py`. База фикстуры `studio` — копия схемы
`schema_db` (одна на процесс; SCHEMA на новый файл — ~0,2 с на тест), базы тестов — без fsync
(autouse `no_fsync`): на нагруженном ночью диске набор выходил за бюджет 60 с.
Набор моста также печатает ранние стеки и полные имена тестов, включая зависание закрытия asyncio Runner.
Сценарии отмены записи медиа выполняются через portal работающего TestClient. Фикстура `write_gate` в
`tests/test_media.py` удерживает I/O до явного разрешения: автоматический выход по пятисекундному сроку
позволял сохранению завершиться раньше проверки отмены. При ошибке теста затвор открывается до закрытия
TestClient; отдельная проверка в `tests/test_suite_timeout.py` подтверждает завершение executor и на этом пути.
Публикационный гейт сохраняет полный stdout/stderr упавшего быстрого набора в отдельном локальном временном файле;
сводка показывает путь к нему и последние 25 строк. Журнал нужен для разбора и остаётся до ручного удаления.
Это относится и к командам контракта, и к запуску по pytest-конфигу; ошибка записи журнала не снимает отказ тестов.
Ожидание `bridge.TASKS`/`push.TASKS` в тестовых `settle` после `gather` обязательно отдаёт цикл
через `asyncio.sleep(0)`: Python 3.14 завершает `gather` готовых задач синхронно, а callback
`TASKS.discard` ещё стоит в очереди. Без уступки `while TASKS` зацикливается и pytest-timeout
убивает worker; регрессия с уже готовой задачей и последующей работой — `tests/test_task_settle.py`.
Клиентские скрипты (`tests/clients.py`): вывод `[ps]`-вариантов декодируется кодовой страницей консоли
процесса тестов (`clients.ps_encoding`: дочерний powershell пишет в трубу ею, ночной прогон идёт под
65001); проверка — `cmd /c "chcp 65001 >nul && .venv\Scripts\python.exe -m pytest -q -m integration
tests/test_studio_batch_client.py -n0"`. Сценарии, ждущие настоящую паузу `PollSec`, и разбор скриптов
парсером PowerShell — `integration` (секунды).


Замеры 2026-11-09: быстрый набор `main` — 45–76 с на четырёх процессах (бюджет 60 с), набор моста — 14–18 с.
Браузерные сценарии общих ссылок и экспорта идут и в Firefox (Playwright `firefox`), помимо Chromium и WebKit.
WebKit-тестам с `page.route` нужен `service_workers="block"`; движок WebKit должен совпадать с версией пакета
Playwright (иначе WebKit-сценарии тихо пропускаются).
