# Studio screenshots · Скриншоты Студии

[English overview](../../README.md) · [Русское описание](../../README.ru.md)

These are browser captures of the real application, including its current Russian UI.
All conversations, agent steps, project names and documents are fictional fixtures.
The displayed agent test results are demonstration content, not a live agent run.
No production account, personal history, private file, model quota or working Windows bridge is used.

Это снимки настоящего приложения с его текущим русским интерфейсом.
Переписка, шаги агентов, проекты и документы — вымышленные демонстрационные данные.
Результаты тестов внутри агентского чата также демонстрационные, а не запуск рабочего агента.
Рабочие учётки, личная история, файлы, квота моделей и действующие Windows-мосты не используются.

## Gallery · Галерея

| Screen / Экран | What it shows / Что показано |
| --- | --- |
| [Chat / Чат](chat.png) | Conversation history, a table, code highlighting and the composer / История, таблица, подсветка кода и поле сообщения |
| [Agents / Агенты](agents.png) | Connected computer, project, session tab, tool timeline and agent response / Компьютер, проект, вкладка сессии, шаги и ответ агента |
| [Files / Файлы](files.png) | Project tree and Markdown preview / Дерево проекта и предпросмотр Markdown |
| [Images / Фото](images.png) | Image prompt, model, format, variations and preset controls before generation / Промпт, модель, формат, серия и пресеты до генерации |
| [Translation / Перевод](translation.png) | Source text, language pair, translated result and history / Исходник, языки, перевод и история |
| [Scans / Сканы](ocr.png) | An original demo document beside its recognized headings and table / Демонстрационный документ рядом с распознанными заголовками и таблицей |
| [Appearance / Оформление](appearance.png) | Theme previews, accent, font size, scale and density / Предпросмотр тем, акцент, шрифт, масштаб и плотность |
| [Dark theme / Тёмная тема](agents-dark.png) | The same agent workspace in the dark theme / То же рабочее место агента в тёмной теме |
| [Mobile chat / Чат на телефоне](chat-mobile.png) | Conversation and mobile bottom navigation / Переписка и нижняя навигация телефона |
| [Mobile agents / Агенты на телефоне](agents-mobile.png) | Agent steps, composer and session controls in a narrow viewport / Шаги, поле задания и управление сессией на узком экране |

Desktop captures use **1440 × 1000**; mobile captures use **390 × 844** with touch emulation.
PNG files preserve the captured viewport without retouching or replacing UI elements.
The [manifest](manifest.json) records each image's viewport and theme.

Настольные снимки: **1440 × 1000**; мобильные: **390 × 844**, с эмуляцией касаний.
PNG сохраняют окно браузера без ретуши и подмены элементов интерфейса.
В [манифесте](manifest.json) записаны размер окна и тема каждого снимка.

## Reproduce · Повторить съёмку

Install the repository's development dependencies and Edge/Chromium, then run from the repository root:

Установите зависимости разработки проекта и Edge/Chromium, затем из корня репозитория выполните:

```powershell
.venv\Scripts\python tools/capture_screenshots.py
```

Use `--output <directory>` to write to a different directory; `--help` lists the options.
The script uses the existing test stand, a temporary SQLite database, a fake gateway and a fake agent
connected through the real local WebSocket. Browser requests outside the local stand are blocked.
It checks page readiness, image loading, horizontal overflow and uncaught browser errors before capture.
It does not validate real model quality or production integration.

Параметр `--output <каталог>` меняет папку результата, `--help` показывает справку.
Скрипт использует существующий тестовый стенд, временную SQLite, подставной шлюз и подставного агента,
подключённого настоящим локальным WebSocket. Запросы браузера вне стенда заблокированы.
Перед снимком проверяются готовность страницы, загрузка изображений, горизонтальное переполнение и ошибки JavaScript.
Съёмка не проверяет качество настоящих моделей или рабочую интеграцию.
