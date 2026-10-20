# studio — студия картинок и роликов

Личные тесты владельца: страница **https://llm.example.com/studio/** (только LAN и туннели) и два
пакетных скрипта — для Windows и для Linux. Всё это — клиенты API картинок и видео LLM-шлюза srvllm
(`http://10.77.1.15:4000/slot`); модели, карта и очередь живут там, в проекте `llm_routers`.
Не для бота и не для чужих глаз: фильтров контента нет (решение владельца), только вымышленные
взрослые, реальные люди — только с согласия.

Выделен из `llm_routers` 2026-10-19 (страница была службой `srvllm-studio-web` на srvllm,
скрипты — `srvllm/client/`); с того же дня страница работает на debian.

| Что | Где |
|---|---|
| Страница | [app/](app/) — FastAPI `server.py`, `index.html`, `login.html`; служба `studio-web` на debian |
| Скрипты | [client/studio-batch.ps1](client/studio-batch.ps1) — картинки, [client/studio-video.ps1](client/studio-video.ps1) — ролики; для Linux — те же на Python: [studio-batch.py](client/studio-batch.py), [studio-video.py](client/studio-video.py) |
| Выкладка | [deploy/](deploy/) — `deploy.sh` с workstation, `install.sh` на хосте, юнит, блок Apache |
| Правила для агентов | [CLAUDE.md](CLAUDE.md), [AGENTS.md](AGENTS.md) |

## Вход

Токен — личный ключ шлюза `owner-images` (vault `GATEWAY_KEY_OWNER_IMAGES`), тот же, что у
скриптов и приложений на телефоне; страница проверяет его вызовом `GET /slot/models`. Сессия —
30 дней, переживает перезапуск службы; пять неверных токенов с адреса за 15 минут — пауза на
15 минут. Ссылка на студию с другого сайта сначала показывает вход (cookie SameSite=Strict при
таком переходе не уходит); при живой сессии страница входа сама переходит в студию.

## Картинки на странице

Модель — Lustify (по умолчанию) или FLUX; размеры портрет 832×1216, квадрат 1024×1024, альбом
1216×832; seed. Пока шлюз ждёт карту и считает, страница пишет «жду карту или генерирую» с
прошедшим временем. Одно задание за раз.

- **Отменить** запрос к шлюзу не обрывает: картинка всё равно досчитывается. Задание переходит в
  «отменяю», потом страница выбрасывает картинку, сама освобождает карту модели задания
  (`/slot/images/release`; пока карта занята — повторяет до минуты) и пишет итог.
- **Освободить карту** — release Lustify, FLUX и gemma-abl с итогом по каждой: «освобождена»,
  «занята генерацией» (карта считает чью-то картинку или идут ролики) или ошибка.
- **Исходное фото** — PNG/JPEG/WebP до 10 МиБ и 16 Мп, обрезается по центру под формат; у
  Lustify «Сила изменения» 0.05–1 (на странице 0.25, в API и скрипте 0.65).
- **Изменить** у готовой картинки — отдельная инструкция той моделью, что выбрана на странице;
  «Изменить только выделенную область» — кисть: пиксели вне выделения берутся из исходника.
- **Описать** — локальная gemma-abl пишет подробный английский промпт по фото; его можно
  править и дополнить, затем сгенерировать новое изображение по тексту (предел — 24000 знаков).

Картинки и фото живут только в памяти службы до выдачи и в браузере; промпты, ключ и картинки в
журнал не пишутся.

## Видео на странице

Раздел «Видео»: картинка с устройства или «Оживить» у картинки из ленты, промпт (пусто —
придумает gemma-abl), длительность 1–30 с, качество, дорисовка до 32 кадров/с, seed. Список
роликов — общая очередь шлюза (видны и задания скрипта): статус словами, «Отменить», плеер и
«Скачать». Ролик страница берёт у шлюза целиком и сама отвечает на диапазоны `Range` (без них
Safari на iPhone не играет), держа в памяти до 3 роликов не дольше 10 минут.

## Скрипты для Windows

Ходят прямо в шлюз (`-Url`, по умолчанию `http://10.77.1.15:4000/slot`), страница им не нужна.
В репозитории ключа в скриптах нет: один раз собрать локальную копию с вписанным ключом —

    powershell -ExecutionPolicy Bypass -File client\studio-batch.ps1 -MakeLocal
    powershell -ExecutionPolicy Bypass -File client\studio-video.ps1 -MakeLocal

Ключ берётся строкой `GATEWAY_KEY_OWNER_IMAGES=` из `C:\work\projects\local-config\secrets\vault.env`
(другой путь — `-VaultPath`); рядом появится `*.local.ps1` (в `.gitignore`), её можно переносить
на другие компьютеры как есть. После ротации ключа — `-MakeLocal` заново.

**Картинки** — `studio-batch.local.ps1 -PromptsFile prompts.txt [-Model lustify|flux]
[-Size portrait|square|landscape] [-Count 3] [-Seed N] [-ImagePath D:\photo.jpg] [-Strength 0.65]
[-OutDir D:\out] [-KeepCard]`: промпты — строками файла, картинки и `manifest.tsv` (seed и модель
каждой) — в папку; в конце и по Ctrl+C карта освобождается, кроме `-KeepCard`.

**Ролики** — `studio-video.local.ps1 -Folder D:\pics [-Prompt "…"] [-Seconds 5]
[-Quality fast|standard|high] [-NoSmooth] [-Count 2] [-Seed N] [-Redo] [-KeepCard]`: картинки
`.jpg .jpeg .png .webp` в самой папке; промпт — `<имя>.txt` рядом, иначе `-Prompt`, иначе его
придумает шлюз, а скрипт сохранит в `<имя>.auto.txt`. Ролики — `<имя>_<N>s_s<seed>.mp4`, журнал —
`video-manifest.tsv`: повторный запуск пропускает сделанное, `-Redo` — заново. Ctrl+C снимает свои
задания и освобождает карту.

## Скрипты для Linux и macOS

`client/studio-batch.py` и `client/studio-video.py` — те же скрипты на Python 3.8+ без сторонних
пакетов: то же поведение, те же имена файлов и журналы (на Linux — UTF-8 без BOM). Параметры те
же, в стиле Linux: `-PromptsFile` → `--prompts-file`, `-NoSmooth` → `--no-smooth`, `-MakeLocal` →
`--make-local`; полный список — `--help`.

Локальную копию с ключом проще собрать на workstation, где лежит vault, и перенести на Linux:

    py client\studio-batch.py --make-local
    py client\studio-video.py --make-local

Рядом появятся `*.local.py` (в `.gitignore`). На Linux без vault — `--vault-path /путь/vault.env`;
там копия получает права 0700. Запуск —

    python3 studio-batch.local.py --prompts-file prompts.txt --model flux --count 3 --out-dir ~/out
    python3 studio-video.local.py --folder ~/pics --seconds 8 --quality high

Ключ разовым `--token` на Linux виден другим пользователям в списке процессов — лучше копия.

Контракт API картинок и видео, отказы шлюза и что делать — руководство srvllm в `llm_routers`
(`srvllm/LLM srvllm - Manual.md`, разделы «API картинок для приложений», «Видео по картинке»,
«Когда что-то не работает»).
