# Устройство студии по областям

Правила и инварианты — [CLAUDE.md](../../CLAUDE.md); здесь — как устроено **сейчас**, по областям.
Не журнал версий: что когда появилось — `git log`; совместимость с мостом — по capability `hello.*`
(таблица версий — [agent-bridge.md](agent-bridge.md#capability-и-версии-моста)). Названия интерфейса —
[глоссарий](../glossary.md).

| Файл | Область |
|---|---|
| [shell.md](shell.md) | оболочка PWA, статика, навигация, тема и масштаб, часовой пояс, барьер создания истории |
| [security-lock.md](security-lock.md) | вход, сессии, CSRF, блокировка и PIN, passkey, журнал безопасности, аварийный выход |
| [connections.md](connections.md) | подключения, разделы, список разрешённых адресов, проверка подключения, s06 |
| [email-accounts.md](email-accounts.md) | обязательная почта, приглашения, коды входа, восстановление пароля и настройки SMTP |
| [chat.md](chat.md) | чат: поток, доставка хода, рассуждение, «Интернет», MCP, ожидание карты, Cleaner |
| [mcp-contracts.md](mcp-contracts.md) | контракты Studio для любых MCP-серверов: пометки инструментов, «Мои файлы», файлы пользователя, состояние источника, руководства и «Справка» |
| [media.md](media.md) | фото, видео, файлы на шаре, очередь удаления, архивы, выгрузка истории, хранилище |
| [speech.md](speech.md) | голосовой ввод, расшифровка, голоса, озвучка |
| [ocr-translate.md](ocr-translate.md) | «Сканы» (распознавание) и «Перевод» |
| [search.md](search.md) | поиск по истории (FTS5) |
| [notifications.md](notifications.md) | «В работе», Web Push, ntfy |
| [sharing-export.md](sharing-export.md) | ссылки «Поделиться» и экспорт PDF/Markdown |
| [agents.md](agents.md) | «Агенты» на сервере: компьютеры, канал моста, API `/api/code/*`, рабочее место |
| [agents-workspace.md](agents-workspace.md) | «Агенты» на странице: чат, файлы, Git, панель проектов, возможности по capability |
| [project-collaboration.md](project-collaboration.md) | совместная работа над проектом «Агентов»: участники, права, отзыв доступа |
| [open-webui.md](open-webui.md) | «Open WebUI»: учётки Open WebUI, токены сессии, дерево и чат, поток socket.io → SSE, пауза ответа |
| [agent-bridge.md](agent-bridge.md) | Мост Windows/macOS `agent/`: модули, решения, пределы, capability → версия |
| [agent-macos.md](agent-macos.md) | Установка macOS, Keychain, LaunchAgent, процессы и системные проверки |
| [deploy-docker.md](deploy-docker.md) | Docker Compose, защищённый вход через Apache, переезд, копия базы и откат |
| [deploy-host.md](deploy-host.md) | прежний нативный хост debian и откат по шагам схемы (подробно) |
| [testing.md](testing.md) | наборы тестов, среда, живая приёмка, медленная сеть |

Ссылки вида `` `модуль.символ` `` на `app/` (и на `agent/studio_agent/` в agent-bridge.md), пары
«`модуль.КОНСТАНТА` (значение)» и относительные ссылки сверяет `tests/test_docs.py`.
