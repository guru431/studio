# AGENTS.md — studio

Полные правила — [CLAUDE.md](CLAUDE.md), что умеют приложение и скрипты — [README.md](README.md),
спека — [docs/superpowers/specs/2026-10-19-studio-pwa-design.md](docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Приложение `https://studio.example.com` (FastAPI `app/main.py`, фронтенд без сборки `app/static/`):
учётки, история, чат · фото · видео. Скрипты `client/studio-batch.ps1`, `client/studio-video.ps1`
(и их Linux-версии `*.py`) ходят прямо в шлюз srvllm (`http://10.77.1.15:4000/slot`, ключ
владельца `owner-images`). Модели, карта и маршруты шлюза — проект `llm_routers`; отсюда выделено
2026-10-19, старая история — там.

Основная ветка и GitHub используют общую очищенную историю. Реальные параметры
установки — в игнорируемом `deploy/local-profile.sh`; правила — [docs/publication.md](docs/publication.md).

## Что нельзя ломать

- История — в базе (`/var/lib/studio/studio.db`) и на шаре (`/mnt/storage/SharedData/studio`), в
  журнал — ничего: промпты, ключи, пароли, тексты, картинки и имена файлов не пишутся.
- Анонимны `/health`, `/login`, `/logout`, `/robots.txt`, PIN-вход `/share` и оболочка PWA;
  `/share/*` — отдельные PIN/read-токены без доступа к учётке (`chat_shares.py`). Всё `/api/*` —
  через сессию и CSRF (`auth.guard`). Пользователь видит только своё. Запертой сессии телефона
  (блокировка, `app/screenlock.py`) — 423 на всё, кроме `auth.SCREEN_LOCK_PATHS`.
- Служба слушает только unix-сокет `/run/studio/web.sock` за Apache debian
  ([deploy/apache-vhost.conf](deploy/apache-vhost.conf)) и потому верит последнему
  `X-Forwarded-For` — TCP-порт не открывать.
- Секреты — только vault и `/etc/studio/studio.env` (`STUDIO_DATA_KEY`) на хосте, на хост — потоком
  stdin; скрипты в репозитории без ключа, копии с ключом `*.local.ps1` и `*.local.py` — в
  `.gitignore`. Репозиторий приватный.
- `client/*.ps1` — UTF-8 с BOM и CRLF; `client/*.py` — UTF-8 без BOM и LF (shebang), только stdlib.
  Поведение двух версий одинаковое — правка идёт в обе. `deploy/*` — LF.

## Команды

- Windows-мост Claude Code, Codex и OpenCode — компонент [agent/](agent/README.md), его правила — [agent/CLAUDE.md](agent/CLAUDE.md).
  Наборы тестов `main` и `agent` объявлены вместе в `local-config/cron/project-registry.json::studio.tests`.
- Тесты: `.venv/Scripts/python -m pytest -q` (быстрый набор, четыре процесса pytest-xdist; один
  тест без параллели — `-n0`), `-m integration` (браузер, PowerShell — одним процессом по очереди),
  `-m manual` (живая приёмка `tests/test_studio_upload_live.py` после выкладки:
  `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD`, `GATEWAY_KEY_OWNER_IMAGES` из vault через `grep`,
  генерирует на карте; команда — в CLAUDE.md).
- Выкладка: `bash deploy/deploy.sh [--new-secret] [--create-admin]` — закоммиченный HEAD на debian
  (`ssh -p 22222 deploy@10.77.1.10`, код `/opt/services/studio`, юниты
  [studio-web.service](deploy/studio-web.service), [studio-backup.timer](deploy/studio-backup.timer)).
  Работающий мост из выполняемого им задания не перезапускать и не убивать: дождаться завершения заданий и выхода в трее. Закрытие Студии на телефоне задания не останавливает.
