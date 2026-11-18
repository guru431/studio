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

Для экспорта чатов в PDF выкладка также доставляет общий `md2pdf.py`: по умолчанию
`~/.claude/bin/md2pdf.py`, иной источник задаётся `STUDIO_MD2PDF_SOURCE`. Его SHA-256 должен
совпадать с закоммиченным `deploy/md2pdf.sha256`; проверка идёт до первого SSH и до подмены кода
на хосте. Доставленная копия — `deploy/md2pdf.py`, без изменений относительно общего инструмента.
`install.sh` проверяет Chromium, ставит его при отсутствии и сохраняет прежний код до проверок.

Профиль задаёт `STUDIO_SSH`, `STUDIO_SSH_PORT`, `STUDIO_SSH_KEY`, `STUDIO_VAULT`,
`STUDIO_URL`, `STUDIO_REMOTE_ROOT`, `STUDIO_MEDIA_DIR` и `STUDIO_HAIRPIN_IP`.
`STUDIO_CERT_DIR` выбирает каталог сертификата Apache, `STUDIO_APACHE_PORT` —
порт его vhost и метку порта в именах журналов.

Для автономного установщика macOS профиль также задаёт `STUDIO_MACOS_BUILD_SSH`,
`STUDIO_MACOS_BUILD_PORT`, `STUDIO_MACOS_BUILD_KEY` и `STUDIO_MACOS_BUILD_ROOT` — абсолютный корень
проекта Studio на Mac. Например, `builder@macos.example.net`, порт `22`, ключ SSH и
`/Users/builder/projects/studio`. Нужны macOS, штатные `pkgbuild`/`productbuild` и системный Python 3.9+.
Сборщик передаёт только ZIP того же HEAD, скрипт и lock runtime; работает в уникальной `.tmp/`
на Mac, проверяет автономную установку, забирает PKG и убирает свою папку. Сервер получает ZIP,
EXE и PKG одной ревизии. Без Mac сборка останавливается до изменения сервера.

Для выпуска Developer ID укажите в игнорируемом `deploy/local-profile.sh` SHA-1 сертификата
**Developer ID Application** в `STUDIO_MACOS_SIGNING_IDENTITY`. Сертификат и его закрытый ключ
должны быть доступны `codesign` в Keychain сборочной учётной записи. Для подписи самого PKG
служит `STUDIO_MACOS_INSTALLER_IDENTITY` (SHA-1 **Developer ID Installer** той же команды).
После одобрения Apple задайте `STUDIO_MACOS_CAPTURE_PROFILE` — абсолютный путь **на сборочном Mac**
к Developer ID provisioning profile для `com.studio.agent` с правом
`com.apple.developer.persistent-content-capture`. Профиль встраивается в распространяемое приложение.
Настройте зарегистрированный App ID и запросите право в своей команде разработчика.
[Persistent Content Capture](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.persistent-content-capture).

При заданной подписи сборка сверяет сертификат, команду, App ID, срок профиля и разрешённое право.
Ошибка останавливает выпуск. Закрытые ключи остаются в Keychain. Сборка проверяет обе подписи Universal2,
запускает встроенный runtime на архитектуре сборщика и проверяет .app после распаковки конечного PKG.
Нотаризация в Apple является отдельной операцией.
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
через `deploy/render-profile.py`: подстановка затрагивает установщик,
Apache, systemd-юниты и настройки Docker/nginx. ZIP моста получает адрес этой установки как сервер
по умолчанию перед сборкой EXE. Файлы в Git остаются прежними.

Git Bash сохраняет `STUDIO_REMOTE_ROOT`, `STUDIO_MEDIA_DIR` и `STUDIO_CERT_DIR`
без преобразования в пути Windows при запуске нативного Python. Преобразование
локальных аргументов и заданные вызывающим процессом исключения MSYS сохраняются.
Параметр `STUDIO_HAIRPIN_IP` передаётся службе через окружение для обработки
входа через собственный маршрутизатор. Профиль Docker дополнительно задаёт доверенный адрес Apache,
адрес публикации nginx, upstream и SSH-параметры отдельного прокси-хоста:
[deploy-docker](architecture/deploy-docker.md). При переносе SQLite остаётся на локальном диске,
каталог медиа сохраняется на прежней шаре.
