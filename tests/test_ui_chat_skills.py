"""Навыки и части ответа на странице «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 1, 2, 7, 17):
меню «/» и чип `#chat-skill` над полем, свёрнутое «Навык: имя» в ленте, правка сообщения с навыком, шаг части под
ответом и кнопка `#chat-parts` в строке состояния; 320 px по-английски и 1280 px по-русски."""
import json

import pytest

from test_ui_browser import (ANSWERED, NO_OVERFLOW, READY, add_gateway, browser, go, live,  # noqa: F401
                             open_new_chat, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

TEXT = "Ты — редактор. Сократи текст вдвое."
SKILLS = f"""(async () => {{ const m = await import('/static/js/api.js');
  await m.api('POST', '/api/skills', {{ name: 'Сократить', text: {TEXT!r}, scope: 'chat' }});
  await m.api('POST', '/api/skills', {{ name: 'Частями', text: 'Дели задачу.', scope: 'chat', parts: true }});
  return true; }})()"""
INPUT = "document.querySelector('#chat-input')"
CHIP = "document.querySelector('#chat-skill')"
OPTION = "document.querySelector('#slash-menu:not([hidden]) .slash-option')"
USER = "[...document.querySelectorAll('#messages .msg.user')].at(-1)"
EDIT = f"({USER}.querySelector('.msg-edit').click(), true)"
CHAT = "location.hash.split('/')[2]"
# меню «/» открыто: оно, его строки и страница — в пределах экрана
MENU_FITS = f"""(() => {{ const inside = (n) => {{ const r = n.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }};
  const menu = document.querySelector('#slash-menu');
  return {NO_OVERFLOW} && inside(menu) && [...menu.querySelectorAll('.slash-option')].every(inside); }})()"""
TOOLS_PART = "document.querySelector('#chat-tools-menu .web-part')"
CLEAR = f"(() => {{ {INPUT}.value = ''; {INPUT}.dispatchEvent(new Event('input', {{ bubbles: true }})); return true; }})()"


def pick(word):
    """«/слово», меню со строкой, Enter — чип над полем."""
    return [("type", "#chat-input", f"/{word}"), ("wait", OPTION), ("eval", "menu", MENU_FITS), ("press", "Enter"),
            ("wait", f"!{CHIP}.hidden")]


@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])
def test_skill_chip_message_edit_and_parts(browser, live, width, lang):
    login = f"chat-skills-{width}"
    live.add_user(login)
    mobile = width == 320
    init = f"localStorage.setItem('studio-language', '{lang}')"
    skill = "Skill: " if lang == "en" else "Навык: "
    gw = live.gw
    gw.chat_script = [["Короче."]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), ("eval", "skills", SKILLS),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            *pick("сокр"),
            ("eval", "chip", f"[{CHIP}.textContent, {INPUT}.value]"),
            ("type", "#chat-input", "вот текст"), ("press", "Enter"),
            # сообщение с сервера: в свёрнутом «Навык: имя» — текст навыка
            ("wait", f"{ANSWERED} && {USER}?.querySelector('.msg-skill-text')"),
            ("eval", "bubble", f"{USER}.querySelector('.bubble').textContent"),
            ("eval", "head", f"{USER}.querySelector('.msg-skill summary').textContent"),
            ("eval", "skillText", f"{USER}.querySelector('.msg-skill').textContent"),
            ("eval", "chipGone", f"{CHIP}.hidden && document.querySelector('#skill-chip').hidden"),
            ("eval", "fits", NO_OVERFLOW), ("eval", "cid", CHAT)],
            width=width, mobile=mobile, init=init)
        assert not r.get("failed"), r
        v = r["values"]
        assert v["menu"] is True, v
        assert v["chip"] == [skill + "Сократить", ""], v
        assert gw.chat_requests[-1]["messages"][-1]["content"] == TEXT + "\n\nвот текст"
        assert v["bubble"] == "вот текст" and v["head"] == skill + "Сократить", v
        assert v["skillText"] == skill + "Сократить" + TEXT, v
        assert v["chipGone"] is True and v["fits"] is True, v
        assert not problems(r), r.get("log")

        # «Изменить»: в поле — только то, что дописал человек, чип на месте; отправка — с прежним текстом навыка
        cid = v["cid"]
        gw.chat_script = [["Иначе."], ["Без навыка."]]
        r = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
            ("wait", f"{ANSWERED} && {USER}?.querySelector('.msg-edit')"),
            ("eval", "edit", EDIT),
            ("wait", f"{INPUT}.value === 'вот текст'"),
            ("eval", "editing", f"[{CHIP}.hidden, {CHIP}.textContent]"),
            ("eval", "clear", CLEAR), ("type", "#chat-input", "иначе"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {USER}?.querySelector('.bubble')?.textContent === 'иначе' && {USER}.dataset.id"),
            ("eval", "kept", f"{USER}.querySelector('.msg-skill summary')?.textContent"),
            # крестик чипа при правке — сообщение без навыка
            ("eval", "edit", EDIT),
            ("wait", f"{INPUT}.value === 'иначе' && !{CHIP}.hidden"),
            ("click", "#skill-chip button"),
            ("eval", "clear", CLEAR), ("type", "#chat-input", "совсем без навыка"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {USER}?.querySelector('.bubble')?.textContent === 'совсем без навыка' && {USER}.dataset.id"),
            ("eval", "dropped", f"!{USER}.querySelector('.msg-skill') && {CHIP}.hidden")],
            width=width, mobile=mobile, init=init)
        assert not r.get("failed"), r
        v = r["values"]
        assert v["editing"] == [False, skill + "Сократить"], v
        assert v["kept"] == skill + "Сократить", v
        assert gw.chat_requests[-2]["messages"][-1]["content"] == TEXT + "\n\nиначе"
        assert gw.chat_requests[-1]["messages"][-1]["content"] == "совсем без навыка"
        assert v["dropped"] is True, v
        assert not problems(r), r.get("log")

        # части — следующим ходом того же диалога: навык вызван в середине диалога
        gw.chat_script = [{"tool_calls": [("run_part", json.dumps({"title": "Сбор", "task": "Собери"}))]},
                          ["Итог части"], ["Общий ответ"]]
        sent = len(gw.chat_requests)
        part = "[...document.querySelectorAll('#messages .msg.assistant')].at(-1)?.querySelector('.web-part')"
        # пока запрос части задержан: в окне «Шаги текущего ответа» — идущая часть со своим названием
        gw.chat_hold = lambda: len(gw.chat_requests) == sent + 2
        try:
            r = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
                ("wait", ANSWERED), *pick("частями"),
                ("type", "#chat-input", "Большая задача"), ("press", "Enter"),
                ("wait", "!document.querySelector('#chat-tools').hidden"),
                ("click", "#chat-tools"), ("wait", f"{TOOLS_PART}"),
                ("eval", "toolsPart", f"[{TOOLS_PART}.querySelector('.part-title').textContent, {TOOLS_PART}.querySelector('.part-state').textContent]")],
                width=width, mobile=mobile, init=init)
        finally:
            gw.chat_hold = False
        assert not r.get("failed"), r
        assert r["values"]["toolsPart"] == ["Сбор", "running" if lang == "en" else "идёт"], r["values"]
        r = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
            ("wait", f"{ANSWERED} && {part} && !document.querySelector('#chat-parts').hidden"),
            ("eval", "part", f"[{part}.querySelector('.part-title').textContent, {part}.querySelector('.part-state').textContent]"),
            ("eval", "partText", f"{part}.querySelector('.part-text').textContent"),
            ("eval", "partsButton", "document.querySelector('#chat-parts .chat-parts-count').textContent"),
            ("eval", "fits", NO_OVERFLOW)],
            width=width, mobile=mobile, init=init)
        assert not r.get("failed"), r
        v = r["values"]
        assert len(gw.chat_requests) - sent == 3                         # ход, часть, итог хода
        part_request = gw.chat_requests[sent + 1]
        assert [m["role"] for m in part_request["messages"]] == ["system", "user"]
        assert v["part"] == ["Сбор", "ready" if lang == "en" else "готово"], v
        assert v["partText"] == "Итог части", v
        assert v["partsButton"] == ("1 of 1" if lang == "en" else "1 из 1"), v
        assert v["fits"] is True, v
        assert not problems(r), r.get("log")
    finally:
        gw.chat_script = []
