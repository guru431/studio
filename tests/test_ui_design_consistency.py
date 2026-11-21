"""Единый вид разделов, образец — «Агенты»: шапка в одну строку с целым заголовком, выборы и кнопки ряда
управления того же размера, что выбор «Агентов» на той же ширине, без видимых системных выборов и без
горизонтальной прокрутки; поля на iPhone не мельче 16 px (иначе iOS увеличивает страницу при фокусе).

Разделы кроме «Агентов» приводит к этому виду следующая задача: что у них ещё не так (`TODO`) — xfail(strict=True),
уже верное сторожится сразу. WebKit и Chromium в одном процессе Playwright не открыть дважды: iPhone — тот же
`engine` с WebKit, а не фикстура `iphone` (у неё свой Playwright)."""
import pytest

from fake_bridge import pid
from test_mcp import TOKEN, URL
from test_ui_browser import (API, CODE_CHAT_A, add_gateway, add_translator, code_free_machine, code_online,
    code_session, live)  # noqa: F401
from test_ui_design_tokens import cookie_for, engine, shell  # noqa: F401
from test_ui_section_limits import GOOD, serve

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

SECTIONS = ["code", "chat", "photo", "video", "speech", "transcribe", "ocr", "translate"]
WIDTHS, LANGS = [320, 390, 1280], ["ru", "en"]
CHROMIUM = pytest.mark.parametrize("engine", ["chromium"], indirect=True)
EVERY = {f"{width}-{lang}" for width in WIDTHS for lang in LANGS}
PHONES = {f"{width}-{lang}" for width in (320, 390) for lang in LANGS}
#: Что ещё не по образцу «Агентов» (замер 2026-11-21) — тест → раздел → «ширина-язык». Задача 3 убирает строки по мере
#: правки разделов; strict: исправленное, но не убранное отсюда, — провал.
TODO = {
    "head": {"photo": {"320-ru", "320-en", "390-en"}, "video": {"320-ru", "320-en"}},
    "controls": {section: EVERY for section in ("photo", "video", "speech")},
    "scroll": {},
}

#: Признак готовности раздела: его поле или кнопка и выборы, которые раздел дорисовывает после загрузки.
READY = {
    "code": "document.querySelector('#code-chat')?.dataset.loaded === '1'",
    "chat": "document.querySelector('#chat-input')",
    "photo": "document.querySelector('#photo-prompt') && document.querySelector('#section-mcp-tool option[value$=\":image_generate\"]')",
    "video": "document.querySelector('#video-prompt') && document.querySelector('#section-mcp-tool option[value$=\":video_create\"]')",
    "speech": "document.querySelector('#speech-text')",
    "transcribe": "document.querySelector('#transcribe-upload')",
    "ocr": "document.querySelector('#ocr-upload') && document.querySelectorAll('#ocr-model option').length > 1",
    "translate": "document.querySelector('#tl-input') && document.querySelectorAll('#tl-model option').length === 2",
}

#: Замер открытого раздела: шапка, эталонная кнопка выбора, кнопки ряда управления, видимые системные выборы.
#: Видимое — с размерами, не `visibility: hidden` и не прозрачное (select под кнопкой `.pick` прозрачен).
#: Инструменты проекта «Агентов» (Git, «Файлы», «Чат») на телефоне переносятся отдельным рядом под шапку — это ряд
#: инструментов, как `.split-toolbar` у разделов, и в высоту шапки он не входит.
MEASURE = """() => {
  const view = document.querySelector('#view');
  const shown = e => { if (!e.getClientRects().length) return false;
    const s = getComputedStyle(e); return s.visibility !== 'hidden' && parseFloat(s.opacity) > 0; };
  const box = e => ({ name: e.id || e.className, font: parseFloat(getComputedStyle(e).fontSize),
    height: e.getBoundingClientRect().height });
  const head = [...view.querySelectorAll('.head, .code-head')].find(shown);
  const h1 = head && head.querySelector('h1');
  const ref = [...view.querySelectorAll('.pick-trigger')].find(shown);
  const headHeight = () => {
    let height = head.getBoundingClientRect().height;
    const line = head.querySelector(':scope > .code-location'), tools = head.querySelector(':scope > .code-project-tools');
    if (line && tools && shown(tools) && tools.getBoundingClientRect().top >= line.getBoundingClientRect().bottom)
      height -= tools.getBoundingClientRect().bottom - line.getBoundingClientRect().bottom;
    return height;
  };
  return {
    head: head ? { height: headHeight(), title: h1 ? [h1.textContent, h1.scrollWidth, h1.clientWidth] : null } : null,
    ref: ref ? box(ref) : null,
    controls: [...view.querySelectorAll('.composer :is(.pick-trigger,.ctl-chip,.chip), '
      + '.split-toolbar :is(.pick-trigger,.ctl-chip,.ctl-primary)')].filter(shown).map(box),
    selects: [...view.querySelectorAll('select:not(.pick select)')].filter(shown).map(e => e.id || e.className),
    scroll: [document.documentElement.scrollWidth, innerWidth],
  };
}"""

#: Поля, которые iOS увеличивает при фокусе: видимые текстовые поля и скрытый за кнопкой выбор `.pick select`.
FIELD_FONTS = """() => [...document.querySelectorAll('#view :is(input, textarea, .pick select)')]
  .filter(e => e.getClientRects().length && !['checkbox', 'radio', 'range', 'file', 'hidden', 'color'].includes(e.type))
  .map(e => [e.id || e.className, parseFloat(getComputedStyle(e).fontSize)])"""


def route(section, aid):
    return f"#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}" if section == "code" else f"#/{section}"


def prepare(page):
    """Подключения для выбора модели в «Сканах» и «Переводе» и MCP с инструментами разделов."""
    page.evaluate(add_gateway()[2])
    page.evaluate(add_translator()[2])
    page.evaluate(f"""async () => {{ const m = {API};
      await m.api('POST', '/api/mcp', {{ name: 'Spark', url: '{URL}', token: '{TOKEN}' }}); await m.loadMe(); }}""")


def visit(page, section, aid):
    page.evaluate("hash => { location.hash = hash; }", route(section, aid))
    page.wait_for_function(f"() => !!({READY[section]})")      # функция, а не строка: CSP студии запрещает eval
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


@pytest.fixture(scope="module")
def studio(live):
    """Учётка с компьютером «Агентов» на связи, двумя подключениями и MCP; замеры кэшируются по (ширина, язык)."""
    serve(live.gw, GOOD)
    cookie = cookie_for(live, "design-consistency")
    aid, channel = code_online(live, "design-consistency", code_free_machine({CODE_CHAT_A: code_session("Единый вид")}))
    state = {"cookie": cookie, "aid": aid, "prepared": False, "cache": {}}
    yield state
    channel.stop()


@pytest.fixture
def measured(engine, live, studio):
    def measure(width, lang):
        key = (width, lang)
        if key not in studio["cache"]:
            context, page = shell(engine, live, studio["cookie"], width=width, height=852, touch=width < 760,
                                  route="#/chat", init=f"localStorage.setItem('studio-language', '{lang}')")
            with context:
                if not studio["prepared"]:
                    prepare(page)
                    studio["prepared"] = True
                result = {}
                for section in SECTIONS:
                    visit(page, section, studio["aid"])
                    result[section] = page.evaluate(MEASURE)
            studio["cache"][key] = result
        return studio["cache"][key]
    return measure


def grid(check):
    """Раздел × ширина × язык; несделанное из TODO — xfail(strict=True) до задачи 3."""
    todo = pytest.mark.xfail(strict=True, reason="задача 3: раздел ещё не по образцу «Агентов»")
    return pytest.mark.parametrize("section,width,lang", [
        pytest.param(section, width, lang, id=f"{section}-{width}-{lang}",
                     marks=[todo] if f"{width}-{lang}" in TODO[check].get(section, ()) else [])
        for section in SECTIONS for width in WIDTHS for lang in LANGS])


@CHROMIUM
@grid("head")
def test_head_is_one_line_and_title_is_whole(measured, section, width, lang):
    head = measured(width, lang)[section]["head"]
    assert head, section
    title = head["title"]
    assert head["height"] <= 56 and (title is None or title[1] <= title[2]), head


@CHROMIUM
@grid("controls")
def test_composer_controls_match_agents(measured, section, width, lang):
    page = measured(width, lang)
    ref = page["code"]["ref"]
    assert ref, page["code"]
    data = page[section]
    for control in data["controls"]:
        assert control["font"] == ref["font"] and abs(control["height"] - ref["height"]) <= 1, (control, ref)
    assert data["selects"] == [], data["selects"]


@CHROMIUM
@grid("scroll")
def test_no_horizontal_scroll(measured, section, width, lang):
    scroll, inner = measured(width, lang)[section]["scroll"]
    assert scroll <= inner, (scroll, inner)


@pytest.mark.parametrize("engine", ["webkit"], indirect=True)
def test_fields_do_not_zoom_on_iphone(engine, live, studio):
    context, page = shell(engine, live, studio["cookie"], width=390, height=844, touch=True, device_scale_factor=3,
                          route="#/chat")
    with context:
        if not studio["prepared"]:
            prepare(page)
            studio["prepared"] = True
        small, seen = {}, {}
        for section in SECTIONS:
            visit(page, section, studio["aid"])
            fonts = page.evaluate(FIELD_FONTS)
            seen[section] = len(fonts)
            small.update({f"{section}:{name}": size for name, size in fonts if size < 16})
    assert seen["code"] and seen["chat"] and not small, (small, seen)
