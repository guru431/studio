# Docker Compose: установка, перенос и восстановление

Основной запуск с 2026-11-11 — два контейнера из одного образа: `web` (FastAPI, Unix-сокет) и
`ingress` (nginx). Образ включает Python 3.13, зависимости из `requirements.txt` с закреплёнными
транзитивными пакетами `constraints.txt` (`pip install -r requirements.txt -c constraints.txt`), ffmpeg, Chromium,
закреплённый yt-dlp, общий `md2pdf.py` с проверкой SHA-256 и EXE/ZIP Windows-моста из того же HEAD.
Работающие Windows-мосты при выкладке не перезапускаются: их каналы переподключаются сами.

## Вход и ограничения

Публичный HTTPS-адрес и сертификаты остаются на Apache прежнего хоста. Его vhost строится из того же
[шаблона](../../deploy/apache-vhost.conf), что и нативная установка: сохраняются отдельные пределы
SSE, видео, WebSocket и загрузки расшифровок. Подстановка `STUDIO_BACKEND_URL` заменяет только upstream.
Выкладка проверяет `apache2ctl -t`, выполняет reload и проверяет публичный `/health`.

На Docker-хосте публикуется только nginx на конкретном IPv4 и порту (по умолчанию 8151).
В nginx разрешён **один адрес Apache**, остальные получают 403; nginx сохраняет последний
`X-Forwarded-For` от Apache без добавления собственного адреса. Приложение TCP не слушает.
Проверять ограничение надо с другой машины, в том числе с поддельным `X-Forwarded-For`.
nginx не пишет адреса запросов; uvicorn работает с `--no-access-log`.

Оба контейнера работают от uid/gid 10001, с корнем только для чтения, `no-new-privileges`, отключёнными
core dumps, ограничениями памяти и процессов. `web` использует сеть хоста, чтобы запросы к шлюзу и Cleaner
проходили их существующие ограничения адресов; это не создаёт TCP-входа в приложение.
Песочница Chromium включена: web получает только `SYS_CHROOT` и профиль seccomp
[chromium-seccomp.json](../../deploy/docker/chromium-seccomp.json). Это [профиль Moby](https://github.com/moby/profiles/blob/main/seccomp/default.json)
с единственным дополнительным правилом для `clone`, `setns`, `unshare`, как в
[документации Playwright](https://playwright.dev/docs/docker#crawling-and-scraping).
Лицензия исходного профиля — [LICENSE.moby](../../deploy/docker/LICENSE.moby).
`SYS_ADMIN`, privileged и отключение песочницы не требуются. На хосте должны быть разрешены
непривилегированные user namespaces; глобальные настройки ядра и AppArmor выкладка не меняет.

## Данные и запуск после перезагрузки

- SQLite: `/var/lib/studio/studio.db` на локальном диске хоста, bind mount с тем же путём,
  каталог `0700`, владелец 10001:10001. В образ и на CIFS база не помещается.
- Медиа: прежний каталог сетевого хранилища, смонтированный на Docker-хосте; в контейнере `/media`.
  CIFS монтируется с `soft,echo_interval=60` и правами uid/gid 10001. Его mount unit и SMB credentials
  — локальные настройки хоста. Новый пустой каталог вместо шары запрещён: entrypoint проверяет тип
  файловой системы до запуска приложения, installer проверяет ещё настоящую запись от uid 10001.
- Секрет: `/etc/studio/studio.env`, `0600 root:root`; Docker читает `env_file`. При переносе копируется
  **прежний** `STUDIO_DATA_KEY` через защищённый поток. Смена ключа не является частью переноса.
  Необязательный сертификат Cleaner остаётся в `/usr/local/share/studio/`, только для чтения в контейнере.
- Сокет: `/run/studio-docker` хоста → `/run/studio` обоих контейнеров, `0750 10001:10001`.
  `studio-docker.service` создаёт каталог при старте и запускает Compose после монтирования шары.
  При загрузке `studio-docker-start.service` повторяет неудачный запуск через 15 секунд, в том числе
  если systemd пропустил запуск Compose из-за ошибки CIFS. После успешного старта попытки прекращаются.
  Контейнеры перезапускаются Docker; без CIFS web отказывает до записи.
- Копия базы: `studio-backup.timer` в 04:30 → `studio-backup.service` → `manage.py backup` внутри web.
  Согласованные копии SQLite сохраняются на шаре, хранятся 7 последних. Ручной и ночной запуск одной базы
  сериализуются. Снимок пишется рядом с базой (`.studio-YYYYMMDD.db.part`) и уже готовым файлом копируется на
  шару; каждый запуск сначала убирает снимки убитых прежних запусков. Ошибка создания копии отображается
  отдельно от проверки и времени последней копии.
  Проверка восстановления — `manage.py verify-backup`: целостность, схема, расшифровка настроек и наличие медиа. Юнит копии зовёт её
  сразу после удачной копии; краткий итог (`ok`, число проблем, время проверки и копии — без путей и имён)
  ложится в `settings` (`manage.CHECK_KEY`) и виден в «Администрирование» → «Хранилище» строкой «Проверка
  копии». Сбои копирования и проверки служба замечает (`admin.notify_failed_check`, раз в `admin.CHECK_WATCH_SEC` (900) с и сразу
  после старта) и один раз шлёт push и ntfy активным администраторам. Тем же проходом `admin.notify_stale_copy`
  ловит копию, которая не запускалась вовсе (таймер не включили после SQL-обхода, юнит упал до `manage.py`,
  блокировка не взята): последний итог копирования старше `admin.COPY_STALE_SEC` (36 ч) — один push с отметкой
  в той же записи, новый итог её сбрасывает; итога нет вовсе — молчит. Тот же порог панель получает в
  `/api/admin/storage` (`stale_sec`) и красит строку копии. Итог «Проверить копию» из панели
  тоже записывается, но без уведомления. Копия со схемой новее кода (после отката) — пропуск без тревоги
  (раздел «Откат»).

## Выкладка

Пример игнорируемого `deploy/local-profile.sh` (адреса и пути собственной установки не коммитить):

```bash
STUDIO_RUNTIME=docker
STUDIO_SSH=deploy@10.77.1.15
STUDIO_SSH_PORT=22222
STUDIO_SSH_KEY=$HOME/.ssh/studio_deploy_key
STUDIO_REMOTE_ROOT=/opt/services/studio
STUDIO_MEDIA_DIR=/mnt/storage/SharedData/studio
STUDIO_URL=https://studio.example.com
STUDIO_HAIRPIN_IP=10.77.1.1
STUDIO_BIND_IP=10.77.1.15
STUDIO_PROXY_IP=10.77.1.10
STUDIO_BACKEND_URL=http://10.77.1.15:8151
STUDIO_PROXY_SSH=deploy@10.77.1.10
STUDIO_PROXY_SSH_PORT=22222
STUDIO_PROXY_VHOST=/opt/services/studio/deploy/apache-vhost.conf
STUDIO_CERT_DIR=/etc/letsencrypt/live/example.net
STUDIO_APACHE_PORT=8443
```

`bash deploy/deploy.sh --prepare-only` доставляет HEAD, собирает образ и проверяет Compose без запуска
приложения и переключения Apache. Обычный `bash deploy/deploy.sh` собирает образ до смены релиза,
переключает `current`, ожидает здоровье обоих контейнеров и обновляет vhost. Неудачный запуск возвращает
предыдущий образ; база остаётся актуальной. Прежний успешный релиз — `previous`, исходники — `releases/`.
`--new-secret` и `--create-admin` — явные операции, как в прежней установке. Переносу они не нужны.
Выкладка с грязным деревом приложения/моста запрещена. HEAD разрешается один раз: код, мост и тег образа — из
одной ревизии; загрузка — в свою папку `.incoming-<ревизия>-<id>`, вторая одновременная выкладка получает отказ
`flock` ([deploy-host](deploy-host.md#выкладка)). Повторная выкладка использует кэш слоёв Docker
(кроме случаев ниже).

Локальный пакет собирается в `.tmp/deploy-<id>` внутри проекта и удаляется при выходе; `.tmp/` — в
`.gitignore`: ZIP моста с адресом установки не попадёт в `git add` и после оборванной выкладки. На хосте выход
очищает только свою успешно созданную папку загрузки, в том числе при отказе и `--prepare-only`.
Другие `.incoming*` сохраняются независимо от возраста. Остатки оборванных выкладок удаляют отдельно,
проверив точный путь, содержимое, принадлежность операции и отсутствие работающего установщика.

Образ получает тег `studio:<версия Studio>-<полный SHA>` и OCI-метки версии и ревизии. Версия берётся из
`app/release.py` доставленного коммита, поэтому разные ревизии одной версии не перезаписывают друг друга.
После успешной выкладки `install-docker.sh` убирает старое (`prune_releases`): в `releases/` остаются цели
`current` и `previous`, их образы сохраняются для отката. Теги читаются из конфигурации этих релизов;
нечитаемая конфигурация запрещает уборку. Остальные образы Studio удаляются по списку Docker, включая
прежние SHA-теги, служебные теги подготовки и остатки неудачных выкладок без каталога релиза. Принудительного удаления нет:
образы, используемые другими контейнерами, защищает Docker. Каталоги с посторонними именами и ссылки сохраняются.
Затем удаляются образы без тега только с OCI-меткой Studio и кэш сборки старше недели
(`docker builder prune -f --filter until=168h`). Ошибка уборки — предупреждение: выкладка уже здорова.
Для возврата дальше предыдущего релиза нужно выложить нужный коммит заново; `--prepare-only` уборку не запускает.

### Обновления базы и пакетов образа

- База `python:3.13-slim-trixie` (Debian trixie и патч-выпуск Python 3.13): `install-docker.sh` собирает
  с `docker build --pull`, новая база из реестра заменяет кэшированную, и все слои пересобираются.
  Digest не закреплён: обновление базы приходит с ближайшей выкладкой.
- Пакеты Debian (Chromium, ffmpeg, nginx-light, сертификаты): сборка получает `REFRESH` — ISO-неделю
  (`date -u +%G-W%V`) на Docker-хосте. Первая выкладка каждой недели промахивается мимо кэша на слое apt
  и ставит текущие пакеты с исправлениями безопасности; повторные выкладки той же недели берут кэш.
- Таймера пересборки нет: без выкладки образ не обновляется.
- Срочно (CVE в Chromium, ffmpeg, OpenSSL или Python): `bash deploy/deploy.sh --refresh-base` —
  `--pull --no-cache`, все слои заново, затем обычная проверка здоровья и откат при отказе. Версии в
  работающем контейнере: `… exec -T web sh -c 'python -V; dpkg-query -W chromium ffmpeg openssl'`
  (команда compose — ниже).
- Python-пакеты обновляются только сменой закреплений в репозитории: прямые — `requirements.txt`,
  транзитивные — `constraints.txt`. yt-dlp — сменой его закрепления в Dockerfile и выкладкой: YouTube
  ломает старые версии, поэтому при отказах субтитров первым делом поднимают версию yt-dlp.

Из каталога установки на Docker-хосте:

```bash
sudo docker compose --env-file current/deploy/docker/host.conf -f current/deploy/docker/compose.yaml ps
sudo docker compose --env-file current/deploy/docker/host.conf -f current/deploy/docker/compose.yaml logs --tail 30
sudo docker compose --env-file current/deploy/docker/host.conf -f current/deploy/docker/compose.yaml exec -T web python /opt/studio/app/manage.py verify-backup
sudo systemctl start studio-backup.service
sudo systemctl status studio-docker-start.service studio-docker.service studio-backup.timer
```

Учётка с кодами входа без доступа к ящику или при сломанном SMTP (подробно — [email-accounts](email-accounts.md#настройка-почты-и-выпуск)):
сначала согласованная копия базы, затем `recover-email` — значения только через stdin (`printf` — встроенная
команда оболочки, в список процессов не попадает); `STUDIO_ACCOUNT_EMAIL=` вместо или вместе с
`STUDIO_EMAIL_CODES=off` заменяет адрес:

```bash
sudo systemctl start studio-backup.service
printf 'STUDIO_ACCOUNT_LOGIN=%s\nSTUDIO_EMAIL_CODES=off\n' alice | sudo docker compose --env-file current/deploy/docker/host.conf \
  -f current/deploy/docker/compose.yaml exec -T web python /opt/studio/app/manage.py recover-email
```

Приёмка YouTube после выкладки или смены версии yt-dlp — выгрузка субтитров одного публичного ролика внутри `web`
с окружением службы (`web._env`) и аргументами `web.youtube`, без загрузки видео; `/tmp` контейнера — tmpfs.
Штатный код выбирает одну дорожку, скачивает её и разбирает VTT; проверка требует непустого текста.
Ожидается строка `ok` с языком и числом знаков, без содержимого субтитров. Ошибка или превышение
суммарного срока двух вызовов yt-dlp завершает команду ненулевым кодом. `VIDEO` — id любого публичного
ролика с субтитрами; временный каталог удаляется и при отказе:

```bash
VIDEO=jNQXAC9IVRw
sudo docker compose --env-file current/deploy/docker/host.conf -f current/deploy/docker/compose.yaml exec -T web \
  env -i PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 HOME=/nonexistent \
  python - "$VIDEO" <<'PY'
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/opt/studio/app")
import state
import web


async def check():
    with tempfile.TemporaryDirectory(prefix="yt-check-") as folder:
        state.CFG = SimpleNamespace(tmp_dir=Path(folder))
        url = f"https://www.youtube.com/watch?v={sys.argv[1]}"
        result = await asyncio.wait_for(web.youtube(url), 2 * web.YTDLP_TIMEOUT_SEC + 5)
        body = result["text"].partition("\n\n")[2]
        if not body.strip():
            raise RuntimeError("empty subtitles")
        print(f"ok lang={result['lang']} chars={len(body)}")


asyncio.run(check())
PY
```

## Перенос с нативной установки

1. Подключить **тот же** каталог медиа на новом хосте, проверить CIFS и запись от uid 10001. Скопировать
   секрет и сертификат через SSH stdin. Сохранить прежние юниты, vhost, код и базу для отката.
2. Выполнить `--prepare-only`. На отдельной согласованной копии базы проверить `verify-backup`, PDF,
   ffmpeg, пакет моста и доступ к шлюзу, Cleaner и Exa. Не запускать второй web на общей рабочей базе
   или снимке с доступом к общим медиа: фоновые уборки могли бы работать одновременно.
3. Проверить активные серверные генерации и дать им закончиться. Windows-мосты не останавливать.
   Остановить `studio-web` и таймер копии **только на прежнем хосте**; снять финальную SQLite backup.
   Копировать не живую базу с незахваченным WAL, а файл SQLite backup. Сравнить хеши и счётчики таблиц.
4. Положить финальную базу на локальный диск нового хоста с владельцем 10001:10001, выполнить обычную
   выкладку из подготовленного HEAD. Проверить HTTPS, авторизацию, CSRF, медиа и переподключение мостов.
   Отключить автозапуск прежней службы и таймера: двух писателей быть не должно.
5. Проверить новую суточную копию и восстановление, запрет прямого входа, состояние остальных сервисов
   Docker-хоста. Обновить приватный реестр инфраструктуры и вики.

## Откат

При неудачном старте новой версии installer автоматически возвращает прежний контейнерный релиз;
база не откатывается. Значения `host.conf` Compose получает только через `--env-file`, а не окружением
installer: откат поднимает прежний образ с прежними адресом, портом и путями. Для ручного возврата сначала переключают `current` на `previous`, затем запускают
Compose с `up -d --no-build --wait`; совместимость схемы описана в [deploy-host](deploy-host.md), SQL-обходы
таблицы отката ([CLAUDE.md](../../CLAUDE.md#откат-по-шагам-схемы)) выполняют, как описано ниже.

Через шаг схемы ночная копия делается, а её проверка на прежнем коде видит схему новее своей. С выпуска,
где `manage.verify_backup` отдаёт `skipped`, это пропуск без тревоги: код 0, итог «пропущена: база новее кода»
в «Хранилище», push нет. Прежний код до этого выпуска считает такую копию провалом: юнит копии уходит в failed,
администраторы каждую ночь получают «Копия базы не поднимается», пока код не вернётся вперёд.

## Восстановление и SQL-обходы

На Docker-хосте нет пользователя `studio`, CLI `sqlite3` нет и в образе: база принадлежит 10001:10001, а SQL
выполняет Python образа текущего релиза отдельным контейнером без сети, от того же uid. Служба при этом
остановлена — и таймер копии тоже: `studio-backup.service` требует `studio-docker.service` и поднял бы
приложение посреди работы. Из каталога установки:

```bash
cd /opt/services/studio
IMAGE=$(sed -n 's/^STUDIO_IMAGE=//p' current/deploy/docker/host.conf)
# SQL со stdin по одной инструкции; PRAGMA foreign_keys=ON уже включена (каскады); строки SELECT — через «|».
sql() {
  sudo docker run --rm -i --network none -u 10001:10001 -v /var/lib/studio:/var/lib/studio \
    --entrypoint python "$IMAGE" -c '
import sqlite3, sys
c = sqlite3.connect("/var/lib/studio/studio.db", isolation_level=None)
c.execute("PRAGMA foreign_keys=ON")
s = ""
for line in sys.stdin:
    s += line
    if sqlite3.complete_statement(s):
        for row in c.execute(s):
            print(*row, sep="|")
        s = ""
'
}
sudo systemctl stop studio-backup.timer studio-docker.service
sql <<'SQL'
DELETE FROM section_links WHERE section = 'ocr';
SELECT count(*) FROM section_links;
SQL
sudo systemctl start studio-docker.service studio-backup.timer
```

Автокоммит — каждая инструкция отдельно; несколько связанных — между `BEGIN;` и `COMMIT;`.

Восстановление базы из ночной копии (`backup/studio-YYYYMMDD.db` на шаре):

1. Проверить копию, пока служба работает: `… exec -T web python /opt/studio/app/manage.py verify-backup
   /media/backup/studio-YYYYMMDD.db` (путь внутри контейнера; без пути — новейшая). Код 0 — поднимается.
2. `sudo systemctl stop studio-backup.timer studio-docker.service`.
3. Сохранить рабочую базу вместе с журналом: `sudo mkdir -m 0700 /var/lib/studio/before-restore-$(date +%Y%m%d)`
   и перенести туда `studio.db`, `studio.db-wal`, `studio.db-shm` (какие есть). На месте `-wal`/`-shm`
   не должно остаться: чужой журнал SQLite применил бы к копии.
4. `sudo install -o 10001 -g 10001 -m 0600 /mnt/storage/SharedData/studio/backup/studio-YYYYMMDD.db
   /var/lib/studio/studio.db` — копия с владельцем контейнера (каталог остаётся `0700 10001:10001`).
5. Пока служба остановлена, отложить файлы шары новее копии: у них нет строк `media` в восстановленной базе,
   студия их не покажет, а стирание истории и удаление учётки до них не дойдут. Файлы пишутся только новыми
   (`media.write`, `os.replace`), поэтому время изменения — время записи. Отбирать надо до запуска: после
   него новее копии станут и новые загрузки пользователей.

   ```bash
   MEDIA=/mnt/storage/SharedData/studio
   COPY=$MEDIA/backup/studio-YYYYMMDD.db
   LATER=$MEDIA/before-restore-$(date +%Y%m%d)
   cd "$MEDIA" && sudo find u* -type f -newer "$COPY" -exec sh -c \
     'for f; do mkdir -p "$0/${f%/*}" && mv "$f" "$0/$f"; done' "$LATER" {} +
   ```

   Файлы, записанные за минуты между снимком базы и копированием его на шару, могут остаться: строк у них
   нет, но и в отбор по времени копии они не попадают. Отказ от восстановления — вернуть файлы на место
   (`cd "$LATER" && sudo find . -type f -exec sh -c 'for f; do mkdir -p "$0/${f%/*}" && mv "$f" "$0/$f"; done' "$MEDIA" {} +`)
   вместе с сохранённой базой из шага 3.
6. `sudo systemctl start studio-docker.service studio-backup.timer`, проверить `/health` и вход. Файлов,
   удалённых после даты копии, на шаре уже нет — их строки `media` отвечают 404. Токены подключений
   расшифровываются только тем `STUDIO_DATA_KEY`, что был при копии (запасной — vault).
7. Сохранённые папки `before-restore-…` — базы и файлов шары — удалить, когда восстановление подтверждено.

Для возвращения **на прежний хост** нельзя просто запустить оставленную старую базу:
после переключения в Docker могли появиться новые записи. Отключить автозапуск
`studio-docker-start.service` и `studio-backup.timer` через `systemctl disable --now`,
затем остановить `studio-docker.service`,
снять согласованную копию актуальной базы и перенести её через SSH на прежний локальный диск, сохранив
оба исходных файла и назначив владельца прежней службы. Проверить её текущим ключом, затем запустить
прежний `studio-web`, восстановить сохранённый Unix-socket vhost, выполнить `apache2ctl -t` и reload,
проверить HTTPS и мосты; после этого включить старый timer. Медиа остаются на общей шаре.
