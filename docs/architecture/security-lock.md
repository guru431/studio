# Вход, сессии, блокировка, журнал безопасности

## Вход и сессии

- `auth` — вход по логину и паролю, сессии на сервере (cookie `__Host-studio_sid`, 30 дней), CSRF (заголовок
  `x-studio-csrf` у изменяющих запросов), пауза после неудач, признак «заперто»; всё `/api/*` — через `auth.guard`
  (HTTP-middleware; анонимные входы — CLAUDE.md «Инварианты»).
- Защита входа от перебора — паузы (на пару «логин + адрес» и на адрес); растущая задержка ответа на неверный пароль
  параллельные попытки не тормозит — на неё не полагаться. Из LAN запрос на публичный адрес приходит с роутера
  (`config.hairpin_ip`): для него пауза — только на пару «логин + адрес», иначе запирался бы весь дом.
- Сессия: `auth.create_session(user_id, device=None)` пишет `sessions.created`, `sessions.device` (`'phone'` |
  `'computer'` по `auth.device_class(ua)`; строка UA не хранится) и `sessions.active` (шаг схемы 34). `screenlock._seen`
  обновляет `active`. `auth.STALE_SEC` = `lock_policy.DEFAULT_TIMEOUT` + `lock_policy.HEARTBEAT_GRACE`.
- Шаг схемы 37 (`db.DEVICES_37`): `session_devices.remember` при входе и `POST /api/lock/alive` сохраняет
  краткие сведения браузера, ОС, доступную модель и последний IP из `auth.client_ip`; исходный User-Agent
  не хранится. Неизвестные сведения — NULL, старые сессии дополняются при следующей отметке своего устройства.
  Имя владельца — отдельный столбец `label`, автоматическое обновление его не меняет. Эти сведения не дают
  прав доступа: браузер может скрыть/подменить их (Windows 10/11 не различаются, сокращённый Android UA
  не раскрывает модель и точную версию ОС). Сетевые запросы для определения модели или геолокации не делаются.
- `Clear-Site-Data: "cache"` (`auth.CLEAR_CACHE`) — у `/logout`, `DELETE /api/history` и аварийного выхода: HTTP-кэш
  (медиа — `private, max-age=86400`) стирается, кэш оболочки offline и localStorage остаются (проверено Chromium;
  Safari — нет).

## Блокировка (`screenlock`, `lock_policy`)

- Блокировка на всех устройствах (не только на телефоне): схема 24 — `users.screen_lock_sections` (`["*"]` или
  любые из восьми видимых разделов, без dictation), `screen_lock_timeout` (0–86400 с; 0 — при возвращении),
  `computer_pin_timeout`. Таймер активности с допуском `lock_policy.HEARTBEAT_GRACE` (30) с — больше интервала
  отметок 15 с плюс срок ответа 8 с; `alive.active=false` не продлевает доступ, истёкший доступ отметка не открывает.
- Частичная защита закрывает API выбранных разделов, связанные media и повторное использование файлов, общие выгрузки
  и изменение защиты. Скан принадлежит и своему разделу: `lock_policy.scan_sections` (ocr + chat при `origin=chat`,
  translate при ссылке `translations.scan_id`) и `lock_policy.hidden_scans` (подзапрос для лент) — в `media_sections`
  (исходный PDF, PNG страниц), `lock_policy.blocks` для `/api/ocr/<id>*`, ленте `/api/ocr` (`db.page` с
  `extra`/`extra_args`), `search.find`, `jobs`. Изменяющие `/api/me/ntfy*`, `/api/me/push`, `/api/me/exa-key`, а также
  `/api/me/sessions*`, `/api/me/emergency`, `/api/me/security-events`, `DELETE /api/me/password-reset` закрывает любая
  частичная защита (GET ntfy/push открыты). Реестр маршрутов — `tests/test_lock_policy.py::ROUTES`: новый маршрут без
  классификации роняет тест.
- Закрытые разделы запроса — ContextVar `lock_policy.RESTRICTED` (`auth.guard`), и задача, созданная запросом,
  наследует его. Раннеры очередей на пользователя (`ocr._runner`, `translate._runner`, `transcripts._runner`,
  `speeches._runner`) живут дольше запроса и читают свои же файлы: в начале они сбрасывают его в пустое множество,
  иначе скан из «Чата» при закрытых «Сканах» падал бы «файл документа удалён». Ход чата (`StreamTask`) — часть
  своего запроса и защиту запроса сохраняет.
- Поиск/задачи фильтруются, code SSE проверяет именно code; закрытие потоков при lock/open — только при защите code
  или всего приложения. Изменение включённой защиты — с паролем. Новый путь, который запертая сессия должна делать, —
  только в `auth.SCREEN_LOCK_PATHS`.
- Отпирание паролем и passkey библиотекой `webauthn`, ключи учётки — таблица `passkeys`.
- PIN учётки — спека 2026-10-28-studio-lock-pin, шаг схемы 17: 4–8 цифр ASCII (`PIN_RE`), хеш `users.pin_hash`
  scrypt, `PUT /api/me/pin` — с паролем учётки, `DELETE` — без; `POST /api/lock/pin` — счёт `users.pin_fails` один на
  учётку, попытка занимает место атомарным `UPDATE … RETURNING` до scrypt, верный — только при том же хеше после
  scrypt; после `PIN_FAILS` (10) — 403 `pin_exhausted`, обнуляют только верный PIN и отпирание паролем (не passkey,
  не новый вход и не смена PIN); пауза пароля `("pw", uid)` PIN не касается; `GET /api/lock` — `pin`, `pin_left`.
- Страница: шторка, экран блокировки, отметки и помощники passkey — `lock.js` (ставит `api.js` `hooks.locked`: 423
  ждёт отпирания и повторяется; цифровая панель PIN — по `pin`/`pin_left` `GET /api/lock`, набранные цифры только в
  памяти модуля; ответ не пришёл — перечитывает возвращение, таймер и «Открыть»).

`lock-start.js` запускает блокировку до модулей оболочки. HTML `/` получает снимок состояния своей сессии
из `screenlock.snapshot`, без отдельного запроса перед PIN; ответ `no-store`, JSON экранирован в атрибуте
и удаляется из DOM после чтения. При возврате известная панель PIN принимает цифры сразу; отправка
ждёт запирания и свежего состояния, которое не стирает введённое. Запросы состояния объединяются.
Старт без снимка использует ту же область `security`, что HTML и 423 при загрузке профиля.
Состояние и запирание ограничены 8 с. Сервер сохраняет проверку области, CSRF и счётчик попыток.
Ранний старт и задержку сети проверяют `tests/test_ui_webkit.py` и `tests/test_ui_browser.py`.
При возврате до истечения локального срока шторка снимается сразу, отметка `/api/lock/alive` идёт в фоне
и при серверном отказе показывает блокировку. Возврат не продлевает срок; истёкший срок и таймер 0
сразу требуют отпирания. Неизменившаяся история «Агентов» сохраняет DOM, раскрытые шаги, выделение
и ранее загруженные сообщения; изменённая история и временные узлы хода заменяются свежим снимком.
Проверки возврата — `tests/test_ui_resume.py` и `tests/test_ui_webkit.py`.

## Сессии учётки, аварийный выход, журнал безопасности (`account`, `security_log`)

- `GET /api/me/sessions` — сессии учётки (время входа и активности, тип, `browser`, `os`, `model`, `label`, `ip`;
  признак — `security_log.handle`, sha256 16 hex, не дайджест). `PATCH /api/me/sessions/<признак> {label}` —
  своё имя устройства до 80 символов, без управляющих символов; пустая строка возвращает автоматическое
  название. Можно назвать текущую и другие собственные живые сессии. Защита — обычные guard, CSRF и блокировка;
  чужая или истёкшая сессия — 404. `DELETE /api/me/sessions/<признак>` — завершить (текущая — 409 `current`): строка
  удаляется, push/ntfy уходят каскадом, `bridge.forget(..., close_computers=True)` — доступ теряется сразу.
- `POST /api/me/emergency {password, agents}` — все сессии, все ссылки «Поделиться», push/ntfy (и шаблон
  `ntfy_settings`), по выбору — отзыв компьютеров (DELETE agents, `bridge.record('revoke')`, `bridge.drop`).
- `GET /api/me/security-events?before=` — журнал `security_events` (шаг 34; `kind` без CHECK, каскад от учётки).
  `security_log.record(user_id, kind, *, detail, digest, device, ip)`, виды — `security_log.KINDS`: вход, неверный
  пароль известной учётки (`detail` login|bridge через `auth.check_password(source=)`), выход, отпирание и неудача
  (password|pin|passkey), `pin_exhausted`, смена пароля, `session_end`, `emergency`, `pin_set`, `pin_delete`,
  `passkey_add`, `passkey_delete`, `password_reset`, `share_create`, `share_revoke`, `share_pin_fail`,
  `share_locked` (посетитель ссылки — без сессии: `ip=auth.client_ip(request)`, без digest). Адрес — открыто, как
  `agent_events.ip`. Хранение — `security_log.KEEP_SEC` (180 дней), `security_log.KEEP_MAX` (1000) строк на учётку.
- Шумные неудачи `security_log.NOISY` (`login_fail`, `share_pin_fail`) сворачиваются в пределах
  `security_log.COLLAPSE_SEC` (3600) по виду, адресу, подробности и сессии: `count`, `first` (шаг 36), `created` —
  время последней; строка меняется на месте, id прежний (страницы `before=<id>` повтор не теряют); своя квота
  `security_log.NOISY_MAX` (200). `unlock_fail` не сворачивается. API и выгрузка
  (`takeout.SECTIONS` → `security`, `security_events/0001.json`) отдают `count` и `first`.
- «Удалить всю историю» журнал не трогает (след входов: стирание без пароля — чужая сессия заметала бы следы; как
  `agent_events`).
- Сброс пароля администратором: `users.password_reset` + событие `password_reset` в журнал владельца (без адреса
  администратора); `GET /api/me.password_reset` — после входа владелец один раз видит окно «Открыть «Безопасность»»;
  закрытие снимает флаг (`DELETE /api/me/password-reset`), аварийный выход — тоже. Администратор флага не видит.
- Страница — `account-security.js`: `sessionsBlock`, `emergencyBlock`, `securityEventsBlock` в «Настройки → Защита»
  после блокировки.

## Ловушки

- Из LAN запрос на публичный адрес приходит с роутера 10.77.1.1 (петля): для него пауза входа —
  только на пару «логин + адрес» (`config.hairpin_ip`), иначе запирался бы весь дом.
- Блокировка (на любом устройстве) — 423 `screen_locked`, пауза после неудач — 429 `locked`: не путать (и
  имена: `screen_lock*` против `auth.LOCK_SEC` / `locked` / `locked_key`). Новый путь, который
  запертая сессия должна делать, — только в `auth.SCREEN_LOCK_PATHS`.
- Варианты passkey (`/api/lock/challenge`, `/api/passkeys/options`) готовятся до нажатия, а
  `navigator.credentials.get/create` зовётся первым в обработчике нажатия: Safari, если перед ним
  ждали сеть, теряет «нажатие», и Face ID на iPhone не появится (в Chrome всё работает) — сторожит
  `test_open_calls_the_passkey_without_waiting_for_the_network`. Вызов на сервере один на сессию и
  живёт 120 с: варианты старше `lock.FRESH_SEC` готовятся заново заранее (таймер, возвращение), а
  пока идёт вызов устройства, новых не готовить — новый заменил бы подписываемый (403).
- Шторка и экран блокировки — `<dialog>` через `showModal()`: иначе открытое окно просмотра
  (`#viewer`, тоже top layer) осталось бы над ними. Escape их не закрывает (cancel отменён,
  `closedby="none"`, закрытое силой окно открывается снова), а до нового `showModal()` оболочку
  прячет `<html class="shrouded">`.

- id ключа в API — хеш credential id, а не он сам: `DELETE /api/passkeys/<id>` пишет журнал Apache.
- `seen` сессии — только по отметке `POST /api/lock/alive`, а не по любому запросу: опросы разделов
  идут на Android и при погасшем экране.
