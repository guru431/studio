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
  Формат и хранение прежние: согласованная SQLite backup, 7 последних на шаре. Проверка восстановления
  — `manage.py verify-backup`: целостность, схема, расшифровка и наличие медиа.

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
Выкладка с грязным деревом приложения/моста запрещена. Повторная выкладка использует кэш слоёв Docker
(кроме случаев ниже).

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
база не откатывается. Для ручного возврата сначала переключают `current` на `previous`, затем запускают
Compose с `up -d --no-build --wait`; совместимость схемы описана в [deploy-host](deploy-host.md).

Для возвращения **на прежний хост** нельзя просто запустить оставленную старую базу:
после переключения в Docker могли появиться новые записи. Отключить автозапуск
`studio-docker-start.service` и `studio-backup.timer` через `systemctl disable --now`,
затем остановить `studio-docker.service`,
снять согласованную копию актуальной базы и перенести её через SSH на прежний локальный диск, сохранив
оба исходных файла и назначив владельца прежней службы. Проверить её текущим ключом, затем запустить
прежний `studio-web`, восстановить сохранённый Unix-socket vhost, выполнить `apache2ctl -t` и reload,
проверить HTTPS и мосты; после этого включить старый timer. Медиа остаются на общей шаре.
