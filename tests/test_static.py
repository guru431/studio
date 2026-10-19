"""Оболочка приложения (app/static, app/main.py): адреса только свои, PWA ставится, служебный
работник не кэширует API и медиа, у страниц — CSP и nosniff."""
import json
import re

from conftest import APP, make_user, signin

STATIC = APP / "static"


def test_static_has_no_external_urls():
    for p in STATIC.rglob("*"):
        if p.suffix in {".html", ".js", ".css", ".webmanifest"} and "vendor" not in p.parts:
            assert not re.search(r"https?://(?!localhost)", p.read_text(encoding="utf-8")), p


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


def test_icons_have_the_sizes_they_claim():
    from PIL import Image
    for name, size in {"icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512,
                       "apple-touch-icon.png": 180}.items():
        with Image.open(STATIC / "icons" / name) as im:
            assert im.size == (size, size), name


def test_service_worker_never_caches_api_or_media():
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "'/api/'" in sw and "'/static/js/'" in sw
    # в кэш попадает только оболочка при установке (addAll); ответы на лету не кладутся никогда
    assert ".put(" not in sw
    assert "/offline.html" in sw and "studio-shell-" in sw


def test_pages_send_csp_and_nosniff(studio):
    def check(r):
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
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
