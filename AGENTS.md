# AGENTS.md — studio

Полные правила — [CLAUDE.md](CLAUDE.md), что умеют приложение и скрипты — [README.md](README.md),
спека — [docs/superpowers/specs/2026-10-19-studio-pwa-design.md](docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Приложение `https://studio.example.com` (FastAPI `app/main.py`, фронтенд без сборки `app/static/`):
учётки, история, чат · фото · видео. Скрипты `client/studio-batch.ps1`, `client/studio-video.ps1`
(и их Linux-версии `*.py`) ходят прямо в шлюз srvllm (`http://10.77.1.15:4000/slot`, ключ
владельца `owner-images`). Модели, карта и маршруты шлюза — проект `llm_routers`; отсюда выделено
2026-10-19, старая история — там.

## Что нельзя ломать

- История — в базе (`/var/lib/studio/studio.db`) и на шаре (`/mnt/storage/SharedData/studio`), в
  журнал — ничего: промпты, ключи, пароли, тексты, картинки и имена файлов не пишутся.
- Анонимны только `/health`, `/login`, `/logout` и оболочка PWA; всё `/api/*` — через сессию и
  CSRF (`auth.guard`). Пользователь видит только своё.
- Служба слушает только unix-сокет `/run/studio/web.sock` за Apache debian
  ([deploy/apache-vhost.conf](deploy/apache-vhost.conf)) и потому верит последнему
  `X-Forwarded-For` — TCP-порт не открывать.
- Секреты — только vault и `/etc/studio/studio.env` (`STUDIO_DATA_KEY`) на хосте, на хост — потоком
  stdin; скрипты в репозитории без ключа, копии с ключом `*.local.ps1` и `*.local.py` — в
  `.gitignore`. Репозиторий приватный.
- `client/*.ps1` — UTF-8 с BOM и CRLF; `client/*.py` — UTF-8 без BOM и LF (shebang), только stdlib.
  Поведение двух версий одинаковое — правка идёт в обе. `deploy/*` — LF.

## Команды

- Тесты: `.venv/Scripts/python -m pytest -q` (быстрый набор), `-m integration` (браузер,
  PowerShell), `-m manual` (живая приёмка с `GATEWAY_KEY_OWNER_IMAGES`, генерирует на карте).
- Выкладка: `bash deploy/deploy.sh [--new-secret] [--create-admin]` — закоммиченный HEAD на debian
  (`ssh -p 22222 deploy@10.77.1.10`, код `/opt/services/studio`, юниты
  [studio-web.service](deploy/studio-web.service), [studio-backup.timer](deploy/studio-backup.timer)).
