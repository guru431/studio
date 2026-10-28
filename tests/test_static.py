"""Оболочка приложения (app/static, app/main.py): адреса только свои, PWA ставится, служебный
работник не кэширует API и медиа, у страниц — CSP и nosniff."""
import json
import re

from conftest import APP, make_user, signin

STATIC = APP / "static"


#: ссылки, по которым человек уходит сам (новая вкладка, без Referer), — не загрузка чужого ресурса
LINKS = ("https://dashboard.exa.ai/api-keys",)


def test_static_has_no_external_urls():
    for p in STATIC.rglob("*"):
        if p.suffix in {".html", ".js", ".css", ".webmanifest"} and "vendor" not in p.parts:
            text = p.read_text(encoding="utf-8")
            for link in LINKS:
                text = text.replace(f"'{link}'", "''")
            assert not re.search(r"https?://(?!localhost)", text), p


def test_static_has_no_lan_addresses():
    """/static открыт всем в интернете: адреса LAN (шлюз и прочее) в примерах — не свои (B1-M6)."""
    for p in STATIC.rglob("*"):
        if p.suffix in {".html", ".js", ".css", ".webmanifest"} and "vendor" not in p.parts:
            assert not re.search(r"\b192\.168\.\d+\.\d+", p.read_text(encoding="utf-8")), p


def test_vendor_files_name_package_and_version():
    banners = {"marked.min.js": "marked 18.1.1", "purify.min.js": "dompurify 3.4.17",
               "highlight.min.js": "highlight.js 11.12.0", "hl-paper.css": "highlight.js 11.12.0",
               "hl-graphite.css": "highlight.js 11.12.0"}
    for name, banner in banners.items():
        first = (STATIC / "vendor" / name).read_text(encoding="utf-8").splitlines()[0]
        assert banner in first, name


def test_manifest_makes_an_installable_app():
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] == m["short_name"] == "Студия"
    assert m["start_url"] == "/" and m["scope"] == "/" and m["display"] == "standalone" and m["lang"] == "ru"
    assert m["background_color"] == m["theme_color"] == "#1b1b1b"
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in m["icons"]:
        assert icon["src"].startswith("/static/icons/")
        assert (STATIC / icon["src"].removeprefix("/static/")).is_file(), icon["src"]


def test_manifest_icons_are_png_only():
    """SVG `sizes: any` iOS берёт первой и не читает — рисует букву на theme_color."""
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["icons"] and all(i["type"] == "image/png" and i["src"].endswith(".png") for i in m["icons"])


def test_icons_have_the_sizes_they_claim():
    from PIL import Image
    for name, size in {"icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512,
                       "apple-touch-icon.png": 180}.items():
        with Image.open(STATIC / "icons" / name) as im:
            assert im.size == (size, size), name
    for name in ("apple-touch-icon.png", "icon-maskable-512.png"):
        with Image.open(STATIC / "icons" / name) as im:
            assert im.mode == "RGB", name                   # без прозрачности: iOS залил бы её чёрным
    with Image.open(STATIC / "icons" / "favicon.ico") as im:
        assert {(16, 16), (32, 32), (48, 48)} <= set(im.info["sizes"])


def test_icon_is_the_bubble_with_a_spark():
    from PIL import Image
    near = lambda a, b: all(abs(x - y) <= 30 for x, y in zip(a, b))
    with Image.open(STATIC / "icons" / "apple-touch-icon.png") as im:
        assert near(im.getpixel((6, 6)), (0xe2, 0x82, 0x5c))       # градиент у левого верхнего угла
        assert near(im.getpixel((60, 60)), (255, 255, 255))         # облачко
        assert near(im.getpixel((90, 86)), (0xc9, 0x64, 0x42))      # центр искры
    with Image.open(STATIC / "icons" / "icon-512.png") as im:
        assert im.getpixel((2, 2))[3] == 0                          # скруглённый угол прозрачен
    svg = (STATIC / "icons" / "icon.svg").read_text(encoding="utf-8")
    assert "#e2825c" in svg and "A84 84 0 0 1 428 192" in svg and "C266 222 286 236 340 246" in svg


def test_pages_link_the_same_icon():
    for page in ("index.html", "login.html"):
        text = (STATIC / page).read_text(encoding="utf-8")
        assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/apple-touch-icon.png">' in text, page
    assert '<img class="logo" src="/static/icons/icon.svg" alt="">' in (STATIC / "index.html").read_text(encoding="utf-8")


def test_root_icon_paths_answer_without_login(studio):
    touch = (STATIC / "icons" / "apple-touch-icon.png").read_bytes()
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        r = studio.c.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == touch, path
    r = studio.c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] in ("image/x-icon", "image/vnd.microsoft.icon")


def test_theme_toggle_lives_in_the_profile_menu_not_the_settings():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    menu = index[index.index('<div id="menu"'):]
    menu = menu[:menu.index("</div>\n<main")]
    assert 'id="theme-toggle"' in menu and 'id="theme-toggle"' not in index[:index.index('<div id="menu"')]
    assert menu.index('class="menu-who"') < menu.index('id="theme-toggle"') < menu.index('href="#/settings"')
    assert "themeBlock" not in (STATIC / "js" / "settings.js").read_text(encoding="utf-8")


def test_text_fields_carry_the_mic_through_field_js_and_have_no_seed():
    """Микрофон и «очистить» — только через field.js (спека 2026-10-27-studio-ui-tidy, 4.4): разделы attachMic сами
    не зовут, пустых `mic-note` нет; полей seed у фото, видео и пресетов нет (4.5)."""
    js = STATIC / "js"
    for name in ("chat.js", "photo.js", "editor.js", "video.js", "speech.js", "search.js"):
        text = (js / name).read_text(encoding="utf-8")
        assert "fieldBox(" in text and "attachMic" not in text and "mic-note" not in text, name
    assert "attachMic(" in (js / "field.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert ".mic-note" not in css and ".with-mic" not in css and ".chip-seed" not in css
    for name in ("photo.js", "video.js", "presets.js"):
        text = (js / name).read_text(encoding="utf-8")
        assert not re.search(r"photo-seed|video-seed|chip-seed|chip-vseed|preset-with-seed|SEED_MAX", text), name
    for name in ("chat.js", "photo.js", "video.js", "speech.js"):
        assert "composer-bar" in (js / name).read_text(encoding="utf-8") and "composer-go" in (js / name).read_text(encoding="utf-8"), name


def test_service_worker_never_caches_api_or_media():
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "'/api/'" in sw and "'/static/js/'" in sw
    # в кэш попадает только оболочка при установке (addAll); ответы на лету не кладутся никогда
    assert ".put(" not in sw
    assert "/offline.html" in sw and "studio-shell-" in sw


def test_service_worker_opens_the_job_from_a_notification():
    """Нажатие на уведомление «В работе»: открытое окно — focus и адрес сообщением, иначе новое окно на
    записи; ничего сверх оболочки в кэш не кладётся (спека 2026-10-21-studio-jobs-panel, раздел 7)."""
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "addEventListener('notificationclick'" in sw and "event.notification.close()" in sw
    assert "clients.matchAll" in sw and ".focus()" in sw and "postMessage({ jobs: href })" in sw
    assert "openWindow(`/#/${href}`)" in sw and ".put(" not in sw and "pushManager" not in sw
    jobs = (STATIC / "js" / "jobs.js").read_text(encoding="utf-8")
    # только showNotification регистрации: конструктор на Android — «Illegal constructor»
    code = "\n".join(line for line in jobs.splitlines() if not line.lstrip().startswith(("*", "/*", "//")))
    assert "registration.showNotification(" in code and not re.search(r"new\s+Notification\s*\(", code)


def test_service_worker_shows_a_push_and_opens_it_like_a_job():
    """Push «Claude Code» (спека 2026-10-28-studio-claude-code-design, 6.8): обработчик `push` показывает
    уведомление регистрации из полезной нагрузки (сбой разбора — пустая), адрес — в data.href для того же
    notificationclick; шапка больше не говорит, что push нет."""
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "addEventListener('push'" in sw and "event.data.json()" in sw
    assert "self.registration.showNotification(" in sw and "data: { href:" in sw and "'Студия'" in sw
    assert "push здесь нет" not in sw
    # тот же tag (чат) — снова звук; без tag renotify бросил бы TypeError — только условно
    assert "...(tag ? { renotify: true } : {})" in sw and sw.count("renotify: true") == 1
    settings =(STATIC / "js" / "settings.js").read_text(encoding="utf-8")
    # в localStorage — только флаг устройства, ни ключей, ни адреса подписки
    assert re.findall(r"localStorage\.setItem\(([^,]+),", settings) == ["PUSH_KEY"]
    assert "const PUSH_KEY = 'studio-push';" in settings and "userVisibleOnly: true" in settings


def test_pages_send_csp_and_nosniff(studio):
    def check(r):
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
        # картинка из ответа модели (markdown `![](http://…)`) — <img> с внешним src: DOMPurify (markdown.js)
        # его пропускает, наружу запрос не уходит только из-за img-src
        assert "img-src 'self' blob: data:" in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"

    check(studio.c.get("/login"))
    check(studio.c.get("/offline.html"))
    make_user(studio, "owner")
    signin(studio, "owner")
    check(studio.c.get("/"))


def test_pages_carry_the_static_version(studio):
    import main
    assert re.fullmatch(r"[0-9a-f]{12}", main.STATIC_VERSION)
    page = studio.c.get("/login").text
    assert f"/static/css/app.css?v={main.STATIC_VERSION}" in page and "{{" not in page
    make_user(studio, "owner")
    signin(studio, "owner")
    page = studio.c.get("/").text
    assert f"/static/js/app.js?v={main.STATIC_VERSION}" in page and "{{" not in page
    assert 'rel="manifest"' in page and 'rel="apple-touch-icon"' in page
    assert page.count('name="theme-color"') == 2


def test_shell_files_are_public_and_api_is_not(studio):
    for path in ("/sw.js", "/manifest.webmanifest", "/offline.html", "/static/css/app.css",
                 "/static/js/app.js", "/static/icons/icon-192.png", "/static/vendor/marked.min.js"):
        assert studio.c.get(path).status_code == 200, path
    assert studio.c.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    assert studio.c.get("/static/js/app.js").headers["content-type"].startswith(("text/javascript",
                                                                                 "application/javascript"))
    assert studio.c.get("/api/me").status_code == 401


def test_static_scripts_revalidate_and_versioned_files_are_kept(studio):
    assert studio.c.get("/static/js/app.js").headers["cache-control"] == "no-cache"
    assert "immutable" in studio.c.get("/static/css/app.css?v=abc").headers["cache-control"]


def test_sw_carries_the_static_version(studio):
    import main
    r = studio.c.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert r.text.startswith(f'const VERSION = "{main.STATIC_VERSION}";')
    assert re.match(r'const VERSION = "[0-9a-f]{12}";', r.text)
    assert r.headers["cache-control"] == "no-cache" and r.headers["service-worker-allowed"] == "/"


def test_photo_preview_knows_the_thumbnail_side():
    """srcset превью называет ширину миниатюры по media.THUMB_SIDE — копия в photo.js не расходится."""
    import media
    side, = re.findall(r"const THUMB_SIDE = (\d+);", (STATIC / "js" / "photo.js").read_text(encoding="utf-8"))
    assert int(side) == media.THUMB_SIDE


def test_profile_module_is_gone_and_the_menu_opens_settings():
    assert not (STATIC / "js" / "profile.js").exists()
    assert 'href="#/settings">Настройки</a>' in (STATIC / "index.html").read_text(encoding="utf-8")
