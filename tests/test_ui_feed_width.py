"""«Ширина ленты» и сворачиваемое поле ввода (п. 41 спеки 2026-11-25-studio-skills-status-local-models): настройка
устройства в «Оформлении» (`html[data-feed]`, токены `--feed-max` и `--composer-max`, ранний theme.js), поле растёт
до половины окна, кнопка `.field-fold` сворачивает его до 6em."""
import pytest

from test_ui_browser import ANSWERED, READY, add_gateway, browser, go, live, open_new_chat, problems, signin  # noqa: F401
from test_ui_chat_carryover import pause

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

#: max-width ленты «Чата» и ленты Open WebUI — пробными узлами с их классами
WIDTHS = """(() => { const probe = (cls) => { const n = document.createElement('div'); n.className = cls;
  document.body.append(n); const w = getComputedStyle(n).maxWidth; n.remove(); return w; };
  return [probe('feed'), probe('owui-feed-list')]; })()"""


def pick(value):
    return ("eval", f"pick-{value}", f"(() => {{ document.querySelector('#appearance-feed-{value}').click();"
                                     " return document.documentElement.dataset.feed || null; })()")


FOLD = """(() => { const b = document.querySelector('#composer .field-fold'); const f = document.querySelector('#chat-input');
  const before = [b.hidden, b.getAttribute('aria-label')]; b.click();
  const em = parseFloat(getComputedStyle(f).fontSize);
  return [...before, b.getAttribute('aria-pressed'), f.getBoundingClientRect().height <= 6 * em + 1,
          b.getAttribute('aria-label')]; })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_feed_width_and_field_fold(browser, live, width):
    login = f"feed-width-{width}"
    live.add_user(login)
    long_text = "\n".join(f"строка {i}" for i in range(60))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        *go("settings/appearance"), ("wait", "document.querySelector('#appearance-feed-normal')"),
        ("eval", "label", "document.querySelector('#appearance-feed-choice legend').textContent"),
        ("eval", "w0", WIDTHS), ("eval", "s0", "localStorage.getItem('studio-feed-width')"),
        pick("wide"), ("eval", "w1", WIDTHS), ("eval", "s1", "localStorage.getItem('studio-feed-width')"),
        pick("full"), ("eval", "w2", WIDTHS), ("eval", "s2", "localStorage.getItem('studio-feed-width')"),
        pick("wide"),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("eval", "after", "[document.documentElement.dataset.feed, document.querySelector('#appearance-feed-wide')?.checked]"),
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
        *open_new_chat(mobile=width < 760),
        ("eval", "fill", f"""(() => {{ const f = document.querySelector('#chat-input'); f.value = {long_text!r};
          f.dispatchEvent(new Event('input', {{ bubbles: true }})); return true; }})()"""),
        pause(100),
        ("eval", "grown", """(() => { const h = document.querySelector('#chat-input').getBoundingClientRect().height;
          return [h > innerHeight * .25, h <= innerHeight * .5 + 1]; })()"""),
        ("eval", "fold", FOLD),
        ("eval", "chat_fits", "document.documentElement.scrollWidth <= innerWidth"),
        ("press", "Enter") if width >= 760 else ("click", "#send"), ("wait", ANSWERED),
        ("eval", "sent", """[document.querySelector('#chat-input').value,
          document.querySelector('#composer .field-fold').hidden,
          document.querySelector('#composer .field-box').classList.contains('field-folded')]""")],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["label"] == "Ширина ленты", v
    assert [v["w0"][0], v["w1"][0], v["w2"][0]] == ["800px", "1100px", "none"], v
    assert v["w1"][1] == "1100px", v                         # .owui-feed-list берёт тот же токен
    assert [v["s0"], v["s1"], v["s2"]] == [None, "wide", "full"], v
    assert v["after"] == ["wide", True] and v["fits"] is True and v["chat_fits"] is True, v   # theme.js до отрисовки
    assert v["grown"] == [True, True], v                     # 60 строк: выше 25 % окна, не выше 50 %
    assert v["fold"] == [False, "Свернуть поле", "true", True, "Развернуть поле"], v
    assert v["sent"] == ["", True, False], v                 # после отправки поле пусто, кнопка скрыта, не свёрнуто
    assert not problems(r), problems(r)
