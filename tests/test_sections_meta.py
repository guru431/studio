"""Реестр разделов страницы (app/static/js/sections-meta.js) — один источник имён, значков и порядка: сверка с
именами сервера, push, страницей входа, manifest и словарём docs/glossary.md, без браузера."""
import json
import re

from conftest import APP

import push
import sections

STATIC = APP / "static"
JS = STATIC / "js"
ORDER = ["chat", "photo", "video", "speech", "transcribe", "ocr", "translate", "code"]


def registry():
    text = (JS / "sections-meta.js").read_text(encoding="utf-8")
    rows = re.findall(r'''\{ id: '(\w+)', name: tr\(["']([^"']+)["']\), group: (\d)''', text)
    return [(sid, name, int(group)) for sid, name, group in rows]


def test_rail_order_is_grouped_and_names_match_the_server():
    rows = registry()
    assert [sid for sid, _, _ in rows] == ORDER
    assert [group for *_, group in rows] == [1, 2, 2, 3, 3, 4, 4, 5]
    names = {sid: name for sid, name, _ in rows}
    for sid, name in sections.NAMES.items():
        if sid in names:
            assert names[sid] == name, sid
    assert names["speech"] == "Озвучка" and names["ocr"] == "Сканы" and names["code"] == push.TITLE == "Агенты"


def test_every_section_has_an_icon_and_no_module_keeps_its_own_table():
    icons = (JS / "icons.js").read_text(encoding="utf-8")
    for sid in ORDER:
        assert re.search(rf"^  {sid}: '", icons, re.M), sid
    # значки разделов — только в icons.js; рельс строится из реестра, а не размечен в index.html
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'class="tab"' not in index and "data-section=\"chat\"" not in index
    for path in JS.glob("*.js"):
        if path.name == "icons.js":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^(export )?const ICONS = ", text, re.M), path.name


def test_login_returns_only_to_screens_the_router_knows():
    """auth.PAGE_SECTIONS (куда вернуть после входа) — те же экраны, что LOADERS роутера app.js."""
    import auth
    loaders = re.findall(r"^  (\w+): \(\) => import\(", (JS / "app.js").read_text(encoding="utf-8"), re.M)
    assert loaders and tuple(loaders) == auth.PAGE_SECTIONS


def test_svg_markup_lives_only_in_icons_js():
    """Ни строк `<svg`, ни путей рисунка вне icons.js: копии расходились с набором (размер «Стопа», пути «Повторить»
    и скрепки). Значки кнопок index.html ставят app.js и lock.js; сторонние библиотеки (vendor/) — не наши."""
    markup = re.compile(r"<svg|viewBox|<path |<rect |<circle ")
    for path in [*JS.glob("*.js"), *STATIC.glob("*.html")]:
        if path.name == "icons.js":
            continue
        found = markup.search(path.read_text(encoding="utf-8"))
        assert found is None, (path.name, found and found.group(0))
    # символ-кнопка «⋯» — значок more и слова в aria-label
    assert "button('⋯'" not in (JS / "settings.js").read_text(encoding="utf-8")


def test_old_names_are_gone_from_the_interface():
    """«Речь» стала «Озвучкой», раздел #/ocr везде «Сканы», «Панель» администратора — «Администрирование»."""
    for path in [*JS.glob("*.js"), *STATIC.glob("*.html")]:
        text = path.read_text(encoding="utf-8")
        for old in ("'Речь'", "«Речь»", ">Речь<", "'Распознавание'", "«Распознавание»", "'Панель'"):
            assert old not in text, (path.name, old)


def test_login_and_manifest_describe_all_eight_sections():
    names = [name.lower() for _, name, _ in registry()]
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    login = (STATIC / "login.html").read_text(encoding="utf-8")
    for name in names:
        assert name in manifest["description"].lower(), name
        assert name in login.lower(), name


def test_glossary_names_the_sections_like_the_registry():
    glossary = (APP.parent / "docs" / "glossary.md").read_text(encoding="utf-8")
    for sid, name, _ in registry():
        row = re.search(rf"^\| `{sid}`[^|]*\| \*{{0,2}}([^|*]+?)\*{{0,2}}(?: \([^)]*\))? \|", glossary, re.M)
        assert row and row.group(1) == name, (sid, row and row.group(1))
