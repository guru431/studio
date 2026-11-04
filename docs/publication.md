# Публикация и локальная установка

Основная ветка содержит полную очищенную историю разработки. После проверки
приватности один и тот же коммит отправляется в основной репозиторий и GitHub;
pull-зеркало получает его с основного сервера. Отдельный публичный снимок не нужен.
Старые приватные Git-bundle остаются только в локальном служебном каталоге и
никогда не отправляются в публичные ветки или теги.

Из всех коммитов исключены локальные настройки помощников, секретные файлы и
эксплуатационные записи. Адреса, имена узлов и пути в документации и шаблонах
заменены примерами. Это относится также к сообщениям и метаданным коммитов.
Перед каждым публичным push обязательны проверки всего исходящего диапазона:
имён файлов, форматов ключей, персонального denylist и значений секретов.
Правила `.sanitize-patterns` и `.github-push-deny` остаются локальными.

## Профиль собственной установки

`deploy/deploy.sh` читает игнорируемый `deploy/local-profile.sh`; другой файл
можно задать через `STUDIO_DEPLOY_PROFILE`. Это доверенный локальный shell-файл.
Значения ключей и паролей туда не помещаются: они по-прежнему поступают из vault
или секретного файла сервера через stdin.

Профиль задаёт `STUDIO_SSH`, `STUDIO_SSH_PORT`, `STUDIO_SSH_KEY`, `STUDIO_VAULT`,
`STUDIO_URL`, `STUDIO_REMOTE_ROOT`, `STUDIO_MEDIA_DIR` и `STUDIO_HAIRPIN_IP`.
`STUDIO_CERT_DIR` выбирает каталог сертификата Apache, `STUDIO_APACHE_PORT` —
порт его vhost и метку порта в именах журналов.
При необходимости `STUDIO_PYTHON` выбирает интерпретатор локальной среды.
Шаблон профиля:

```bash
STUDIO_SSH=${STUDIO_SSH:-deploy@server.example.net}
STUDIO_SSH_PORT=${STUDIO_SSH_PORT:-22}
STUDIO_SSH_KEY=${STUDIO_SSH_KEY:-$HOME/.ssh/studio_deploy_key}
STUDIO_VAULT=${STUDIO_VAULT:-$HOME/.config/studio/vault.env}
STUDIO_URL=${STUDIO_URL:-https://studio.example.com}
STUDIO_REMOTE_ROOT=${STUDIO_REMOTE_ROOT:-/opt/services/studio}
STUDIO_MEDIA_DIR=${STUDIO_MEDIA_DIR:-/mnt/storage/SharedData/studio}
STUDIO_HAIRPIN_IP=${STUDIO_HAIRPIN_IP:-198.51.100.1}
STUDIO_CERT_DIR=${STUDIO_CERT_DIR:-/etc/letsencrypt/live/example.net}
STUDIO_APACHE_PORT=${STUDIO_APACHE_PORT:-8443}
```

Перед обращением к серверу профиль проверяется. Поток `git archive` проходит
через `deploy/render-profile.py`: подстановка затрагивает только установщик,
Apache и два systemd-юнита. ZIP моста получает адрес этой установки как сервер
по умолчанию перед сборкой EXE. Файлы в Git остаются прежними.

Git Bash сохраняет `STUDIO_REMOTE_ROOT`, `STUDIO_MEDIA_DIR` и `STUDIO_CERT_DIR`
без преобразования в пути Windows при запуске нативного Python. Преобразование
локальных аргументов и заданные вызывающим процессом исключения MSYS сохраняются.
Параметр `STUDIO_HAIRPIN_IP` передаётся службе через окружение для обработки
входа через собственный маршрутизатор. Каталоги данных и SQLite не перемещаются.
