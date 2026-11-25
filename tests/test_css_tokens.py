"""Дизайн-токены студии без браузера: один источник тем, все var() объявлены, наведение — только у мыши,
нет повторных селекторов в одном блоке. Цвета и геометрия в браузере — test_ui_design_tokens.py."""
import json
import re

import account_profile as profile

from conftest import APP

STATIC = APP / "static"
CSS = {name: (STATIC / "css" / name).read_text(encoding="utf-8")
       for name in ("app.css", "share.css", "code-desktop.css")}
#: переменные, которые ставит не CSS, а разметка или JS (style.setProperty)
FROM_JS = {"--font-scale", "--ui-scale", "--tabs-h", "--context-percent", "--ratio", "--tree-depth"}


def strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def rules(text):
    """[(цепочка @-правил, номер блока, селектор, {свойства})] — по фигурным скобкам; @keyframes пропущены.
    Номер блока различает два отдельных `@media (max-width: 760px)` с одним условием."""
    text = strip_comments(text)
    out, stack, start, opened = [], [], 0, 0
    for i, ch in enumerate(text):
        if ch == "{":
            prelude = " ".join(text[start:i].split())
            opened += 1
            stack.append((prelude, opened))
            if not prelude.startswith("@") and not any(p.startswith("@keyframes") for p, _ in stack[:-1]):
                body = text[i + 1:text.index("}", i)]
                props = {d.split(":")[0].strip() for d in body.split(";") if ":" in d}
                out.append((tuple(p for p, _ in stack[:-1]), stack[-2][1] if len(stack) > 1 else 0, prelude, props))
            start = i + 1
        elif ch == "}":
            stack.pop()
            start = i + 1
        elif ch == ";":
            start = i + 1
    return out


def test_every_used_variable_is_declared():
    declared = set(FROM_JS)
    for text in CSS.values():
        declared |= set(re.findall(r"(--[\w-]+)\s*:", strip_comments(text)))
    missing = {}
    for name, text in CSS.items():
        # var(--x, запасное) допустим и без объявления: запасное значение и есть умолчание
        for used in re.findall(r"var\((--[\w-]+)\s*\)", strip_comments(text)):
            if used not in declared:
                missing.setdefault(name, set()).add(used)
    assert not missing, missing


def test_theme_values_have_one_source():
    """Тёмные значения — только в light-dark(): ни ветки prefers-color-scheme, ни второй копии палитры."""
    css = strip_comments(CSS["app.css"])
    assert "prefers-color-scheme" not in css
    assert css.count("#1b1b1b") == 1 and css.count("#ececec") == 1 and css.count("#fbfaf7") == 1
    dark = re.search(r'\[data-theme="dark"\]\s*\{([^}]*)\}', css)
    assert dark and dark.group(1).split(":")[0].strip() == "color-scheme" and dark.group(1).count(":") == 1
    assert re.search(r":root\s*\{[^}]*color-scheme:\s*light dark", css)


def test_hover_styles_are_only_for_a_mouse():
    """На iPhone :hover «залипает» после касания: наведение — внутри @media (hover: hover)."""
    bare = [selector for name in CSS for chain, _, selector, _ in rules(CSS[name])
            if ":hover" in selector and not any("hover: hover" in p or "hover:hover" in p for p in chain)]
    assert not bare, bare


def test_no_rule_overrides_its_own_selector_in_one_block():
    """Тот же селектор в том же блоке задаёт то же свойство второй раз — поведение зависит от порядка правил."""
    for name, text in CSS.items():
        seen, twice = {}, []
        for _, block, selectors, props in rules(text):
            for selector in split_list(selectors):
                again = seen.get((block, selector), set()) & props
                if again:
                    twice.append((selector, sorted(again)))
                seen.setdefault((block, selector), set()).update(props)
        assert not twice, (name, twice)


def split_list(selectors):
    """Список селекторов по запятым верхнего уровня (запятые внутри :is() и :has() — не разделители)."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(selectors):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and not depth:
            out.append(selectors[start:i].strip())
            start = i + 1
    return out + [selectors[start:].strip()]


#: Ряды управления и шапки разделов: размер шрифта — только токеном (--ctl-*, --title-font), не числом в разделе.
CONTROL_BLOCKS = (".pick-trigger", ".ctl-chip", ".ctl-primary", ".composer .chip", ".head h1")


def test_control_rows_take_font_size_only_from_tokens():
    css = strip_comments(CSS["app.css"])
    found, raw = set(), []
    for prelude, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        hit = set(split_list(" ".join(prelude.split()))) & set(CONTROL_BLOCKS)
        found |= hit
        sizes = [d.split(":", 1)[1].strip() for d in body.split(";") if d.split(":")[0].strip() == "font-size"]
        raw += [(sorted(hit), size) for size in sizes if hit and not size.startswith("var(--")]
    assert found == set(CONTROL_BLOCKS) and not raw, (set(CONTROL_BLOCKS) - found, raw)


def test_focus_ring_is_not_hidden_and_reaches_upload_labels():
    css = strip_comments(CSS["app.css"])
    assert ".icon-btn:focus-within" not in css
    assert re.search(r"label:has\(> input\.sr:focus-visible\)", css)


def test_unused_and_colliding_classes_are_gone():
    css = strip_comments(CSS["app.css"])
    assert ".grid2" not in css
    # имя файла у вложения чата «Агентов» и в дереве «Файлов» — разные классы
    assert "class: 'code-attachment-name'" in (STATIC / "js" / "code-chat.js").read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^\.code-file-name\b", css)) == 1
    assert "!important" not in strip_comments(CSS["code-desktop.css"])


def test_stop_buttons_look_the_same_in_every_section():
    js = STATIC / "js"
    chat = (js / "chat.js").read_text(encoding="utf-8")
    code = (js / "code-chat.js").read_text(encoding="utf-8")
    assert re.search(r"id: 'stop', class: '[^']*\bcomposer-stop\b", chat)
    assert re.search(r"id: 'code-stop', class: '[^']*\bcomposer-stop\b", code)


def test_accent_list_is_one_contract_for_server_page_css_and_early_theme():
    """Акцент учётки: profile.ACCENTS = appearance.js ACCENTS; у каждого, кроме умолчания, — правило [data-accent] с
    --accent/--accent-soft/--primary в light-dark() и строка раннего theme.js; умолчание — те же токены у :root."""
    js = (STATIC / "js" / "appearance.js").read_text(encoding="utf-8")
    listed = re.findall(r"\['(\w+)', tr\(", js[js.index("export const ACCENTS"):js.index("export const THEME_NAMES")])
    assert listed == list(profile.ACCENTS)
    css = strip_comments(CSS["app.css"])
    early = (STATIC / "theme.js").read_text(encoding="utf-8")
    for accent in profile.ACCENTS:
        rule = re.search(r'(?:^|\n)([^{}\n]*\[data-accent="%s"\][^{}]*)\{([^}]*)\}' % accent, css)
        assert rule, accent
        props = dict(d.split(":", 1) for d in (x.strip() for x in rule.group(2).split(";")) if d)
        assert {"--accent", "--accent-soft", "--primary"} <= set(props), (accent, props)
        assert all(props[k].strip().startswith("light-dark(") for k in ("--accent", "--accent-soft", "--primary"))
        if accent == profile.ACCENTS[0]:
            assert ":root" in rule.group(1) and f"'{accent}'" not in early
        else:
            assert f"accent === '{accent}'" in early, accent


def test_feed_width_is_one_contract_for_css_page_and_early_theme():
    """«Ширина ленты»: appearance.js FEED_WIDTHS = значения раннего theme.js = селекторы [data-feed] app.css; у каждого —
    оба токена --feed-max и --composer-max, умолчание — у :root; лента и поле берут токены."""
    js = (STATIC / "js" / "appearance.js").read_text(encoding="utf-8")
    widths = re.search(r"export const FEED_WIDTHS = \[([^\]]*)\]", js).group(1)
    listed = re.findall(r"'(\w+)'", widths)
    assert listed == ["wide", "full"]
    early = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert re.findall(r"feed === '(\w+)'", early) == listed
    css = strip_comments(CSS["app.css"])
    assert re.findall(r'\[data-feed="(\w+)"\]\s*\{', css) == listed
    for value in [None, *listed]:
        selector = r":root\s*" if value is None else r':root\[data-feed="%s"\]\s*' % value
        bodies = re.findall(r"(?:^|\n)%s\{([^}]*)\}" % selector, css)
        assert any("--feed-max" in b and "--composer-max" in b for b in bodies), value
    for sel, token in ((".feed", "--feed-max"), (".owui-feed-list", "--feed-max"), (".composer", "--composer-max")):
        body = re.search(r"(?:^|\n)%s\s*\{([^}]*)\}" % re.escape(sel), css).group(1)
        assert f"max-width: var({token})" in body, sel


def test_density_keeps_the_touch_target():
    """«Компактно» уменьшает высоты и просветы, но не цель касания --h-touch."""
    rule = re.search(r'\[data-density="compact"\]\s*\{([^}]*)\}', strip_comments(CSS["app.css"]))
    assert rule and "--h-md" in rule.group(1) and "--space-6" in rule.group(1) and "--h-touch" not in rule.group(1)


def test_share_page_and_manifest_follow_the_theme():
    share = (STATIC / "share.html").read_text(encoding="utf-8")
    assert share.count('name="theme-color"') == 2
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    # одна заставка на обе темы: не тёмная и не светлая
    assert manifest["background_color"] == manifest["theme_color"] not in ("#1b1b1b", "#fbfaf7")
