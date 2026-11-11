# AGENTS.md — studio

Полные правила — [CLAUDE.md](CLAUDE.md), что умеют приложение и скрипты — [README.md](README.md),
спека — [docs/superpowers/specs/2026-10-19-studio-pwa-design.md](docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Приложение `https://studio.example.com` (FastAPI `app/main.py`, фронтенд без сборки `app/static/`):
учётки, история, чат · фото · видео. Скрипты `client/studio-batch.ps1`, `client/studio-video.ps1`
(и их Linux-версии `*.py`) ходят прямо в шлюз srvllm (`http://10.77.1.15:4000/slot`, ключ
владельца `owner-images`). Модели, карта и маршруты шлюза — проект `llm_routers`; отсюда выделено
2026-10-19, старая история — там.

`origin` приватный, но основная ветка после очистки публикуется на GitHub: в отслеживаемые файлы — только
примеры адресов, имён и путей. Реальные параметры установки — в игнорируемом `deploy/local-profile.sh`;
правила — [docs/publication.md](docs/publication.md). Устройство по областям — [docs/architecture/](docs/architecture/README.md).

## Что нельзя ломать

- История — в базе (`/var/lib/studio/studio.db`) и на шаре (`/mnt/storage/SharedData/studio`), в
  журнал — ничего: промпты, ключи, пароли, тексты, картинки и имена файлов не пишутся.
- Анонимны `/health`, `/login`, `/logout`, `/robots.txt`, оболочка PWA и два класса входов со своей защитой:
  публичная ссылка `/share` и `/share/unlock|check|file` (`chat_shares.py`: секреты только телом JSON ≤ 4096 байт,
  чужой `Origin` — 403, PIN с паузами и общим пределом, read-токен без прав учётки) и `auth.OPEN_API`:
  `/api/agent/enroll` (логин и пароль с паузами `/login`) и `/api/agent/package` (Bearer-ключ действующего
  устройства активной учётки). Остальное `/api/*` — через сессию и CSRF (`auth.guard`); WebSocket
  `/api/agent/link` проверяет ключ сам. Пользователь видит своё и явно выданные проекты «Агентов»
  (`project_access.py`, [совместная работа](docs/architecture/project-collaboration.md)). Запертой сессии (блокировка на всех
  устройствах, `app/lock_policy.py`) — 423 на API защищённых разделов, кроме `auth.SCREEN_LOCK_PATHS`.
- В Docker служба слушает только unix-сокет `/run/studio/web.sock`; nginx принимает запросы
  только от Apache debian и сохраняет его `X-Forwarded-For`. Прямого TCP-входа в приложение нет.
  Устройство и откат — [deploy-docker](docs/architecture/deploy-docker.md).
- Секреты — только vault и `/etc/studio/studio.env` (`STUDIO_DATA_KEY`) на хосте, на хост — потоком
  stdin; скрипты в репозитории без ключа, копии с ключом `*.local.ps1` и `*.local.py` — в
  `.gitignore`. Основная ветка публикуется после очистки — секретов и реальных адресов в Git нет.
- `client/*.ps1` — UTF-8 с BOM и CRLF; `client/*.py` — UTF-8 без BOM и LF (shebang), только stdlib.
  Поведение двух версий одинаковое — правка идёт в обе. `deploy/*` — LF.

## Команды

- Windows-мост Claude Code, Codex и OpenCode — компонент [agent/](agent/README.md), его правила — [agent/CLAUDE.md](agent/CLAUDE.md).
  Наборы тестов `main` и `agent` объявлены вместе в `local-config/cron/project-registry.json::studio.tests`.
- Тесты: `.venv/Scripts/python -m pytest -q` (быстрый набор, восемь процессов pytest-xdist; один
  тест без параллели — `-n0`), `-m integration` (браузер, PowerShell — одним процессом по очереди),
  `-m manual` (живая приёмка `tests/test_studio_upload_live.py` после выкладки:
  `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD`, `GATEWAY_KEY_OWNER_IMAGES` из vault через `grep`,
  генерирует на карте; команда — в CLAUDE.md).
- Выкладка: `bash deploy/deploy.sh [--new-secret] [--create-admin]`, профиль `STUDIO_RUNTIME=docker` —
  закоммиченный HEAD на Docker-хост, [compose](deploy/docker/compose.yaml), `studio-docker.service`
  и `studio-backup.timer`; Apache остаётся HTTPS-входом. Прежний systemd-профиль сохранён для отката.
  Работающий мост из выполняемого им задания не перезапускать и не убивать: дождаться завершения заданий и выхода в трее. Закрытие Студии на телефоне задания не останавливает.
