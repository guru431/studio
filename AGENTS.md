# AGENTS.md — studio

Полные правила — [CLAUDE.md](CLAUDE.md), что умеет страница и скрипты — [README.md](README.md).

Страница `https://llm.example.com/studio/` (FastAPI, `app/server.py`) и PowerShell-скрипты
`client/studio-batch.ps1`, `client/studio-video.ps1` (и их Linux-версии `*.py`) — клиенты API
картинок и видео шлюза srvllm
(`http://10.77.1.15:4000/slot`, ключ владельца `owner-images`). Модели, карта и маршруты шлюза —
проект `llm_routers`; отсюда выделено 2026-10-19, старая история — там.

## Что нельзя ломать

- Промпты, ключи, картинки и имена файлов — не в журнал и не на диск (только память и браузер).
- Анонимны только `/studio/health`, форма входа, `POST /studio/login` и `/logout`; всё
  `/studio/api/*` — через сессию (middleware `guard`).
- Служба слушает только unix-сокет `/run/studio/web.sock` за Apache debian и потому верит
  последнему `X-Forwarded-For` — TCP-порт не открывать.
- Секреты — только vault и `/etc/studio/studio.env` на хосте; скрипты в репозитории без ключа,
  копии с ключом `*.local.ps1` и `*.local.py` — в `.gitignore`. Репозиторий приватный.
- `client/*.ps1` — UTF-8 с BOM и CRLF; `client/*.py` — UTF-8 без BOM и LF (shebang), только stdlib.
  Поведение двух версий одинаковое — правка идёт в обе.

## Команды

- Тесты: `.venv/Scripts/python -m pytest -q` (быстрый набор), `-m integration` (браузер,
  PowerShell), `-m manual` (живая приёмка с `GATEWAY_KEY_OWNER_IMAGES`, генерирует на карте).
- Выкладка: `bash deploy/deploy.sh` — закоммиченный HEAD на debian (`ssh -p 22222 deploy@10.77.1.10`,
  код `/opt/services/studio`, служба `studio-web`).
