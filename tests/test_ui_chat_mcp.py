"""MCP в диалоге на странице (chat-mcp.js, спека 2026-11-21-studio-source-foundation-design §4 и §7a п. 2): окно
`#chat-mcp-dialog` — общий сервер включён по умолчанию, инструменты с метками, инструмент «с подтверждением»
включён, как остальные (вызов ждёт «Запустить» в чате); политика инструментов общего MCP в окне «Доступ» панели администратора."""
import json

import httpx
import pytest

import auth
import db
from test_ui_speech_read import answer_chat
from test_mcp import TOKEN as MCP_KEY
from test_mcp_dialog import HOST, server, tool
from test_ui_browser import (ANSWERED, API, NO_OVERFLOW, READY, add_gateway, browser, go, is_open, live, open_new_chat,  # noqa: F401
                             problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


def test_mcp_document_has_download_under_answer_and_at_the_inline_path(browser, live):
    login = "mcp-doc-download"
    name = "План по отделам.docx"
    cid = answer_chat(live, live.add_user(login), f"Готово: `out/{name}`")
    with db.connect(live.db_path) as conn:
        conn.execute("UPDATE messages SET tools = ? WHERE chat_id = ? AND role = 'assistant'", (json.dumps([
            {"kind": "mcp", "tool": "create_docx", "server": "MCP", "sid": 1, "stamp": "test",
             "text": f"Готово: out/{name}",
             "files": [{"name": name, "path": f"out/{name}", "mime": "application/octet-stream"}]},
        ], ensure_ascii=False), cid))
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"chat/{cid}"),
        ("wait", "document.querySelector('.msg.assistant .md a[download]')"),
        ("eval", "links", """[...document.querySelectorAll('.msg.assistant a[download]')]
          .map(a => [a.getAttribute('href'), a.download])""")])
    assert not result.get("failed"), result
    inline, button = result["values"]["links"]
    assert inline == button and inline[0].startswith(f"/api/chats/{cid}/messages/") and inline[1] == name

RULES = "Отвечай кратко и только по делу."
TOOLS = [tool("lookup", title="Поиск по базе"),
         tool("web_search", annotations={"openWorldHint": True}),
         tool("video_translate", _meta={"studio/confirm": True})]
TOGGLE = "document.querySelector('#mcp-toggle')"
DIALOG = "#chat-mcp-dialog"


def admin_client(live) -> httpx.Client:
    """httpx-сессия администратора `owner` с CSRF."""
    client = httpx.Client(base_url=live.url, headers={"Origin": live.url})
    assert client.post("/login", data={"login": "owner", "password": "password-123"}).status_code == 303
    for cookie in client.cookies.jar:            # Secure cookie на тестовом HTTP — только явно
        cookie.secure = False
    client.headers[auth.CSRF_HEADER] = client.get("/api/me").json()["csrf"]
    return client


def shared_server(live, name: str, path: str) -> int:
    server(live, path, tools=TOOLS, instructions=RULES)
    client = admin_client(live)
    try:
        response = client.post("/api/admin/shared/mcp", json={"name": name, "url": HOST + path, "token": MCP_KEY})
    finally:
        client.close()
    assert response.status_code == 201, response.text
    return response.json()["id"]


def row(sid: int) -> str:
    return f"document.querySelector('{DIALOG} .chat-mcp-item[data-sid=\"{sid}\"]')"


def tool_switch(sid: int, name: str) -> str:
    return f"{row(sid)}.querySelector('.chat-mcp-tool[data-tool={name}]')"


def open_dialog(sid: int) -> list:
    return [("click", "#mcp-toggle"), ("wait", f"{is_open(DIALOG)} && {row(sid)}"
                                               " && !document.querySelector('#chat-mcp-save').disabled")]


def state(sid: int) -> tuple:
    """Переключатели окна: сервер, lookup, web_search, video_translate."""
    switches = ", ".join(f"{tool_switch(sid, n)}.checked" for n in ("lookup", "web_search", "video_translate"))
    return ("eval", "state", f"[{row(sid)}.querySelector('.chat-mcp-server').checked, {switches}]")


def chip_matches_view() -> str:
    return f"""(async () => {{ const m = {API};
      const chat = (await m.api('GET', '/api/chats/' + location.hash.split('/')[2])).chat;
      const n = chat.mcp_ids.length;
      return [{TOGGLE}.textContent === (n ? 'MCP (' + n + ')' : 'MCP'), chat]; }})()"""


def reload_chat() -> list:
    return [("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"
                     f" && document.querySelector('#messages .msg') && {TOGGLE} && !{TOGGLE}.hidden")]


@pytest.mark.parametrize("width", [320, 1280])
def test_dialog_mcp_servers_and_tools_are_chosen_and_kept(browser, live, width):
    login, name = f"mcp-dialog-{width}", f"Общие инструменты {width}"
    sid = shared_server(live, name, f"/mcp-dialog-{width}")
    live.add_user(login)
    mobile = width == 320
    pick = lambda n: ("eval", "toggle", f"{tool_switch(sid, n)}.click()")
    steps = [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"
                 f" && {TOGGLE} && !{TOGGLE}.hidden && {TOGGLE}.textContent !== 'MCP'"),
        ("eval", "draftChip", f"""(async () => {{ const m = {API};
          const shared = (await m.api('GET', '/api/mcp')).items
            .filter(s => s.shared && s.enabled && s.dialog_default !== false).length;
          return {TOGGLE}.textContent === 'MCP (' + shared + ')'; }})()"""),
        # новый диалог: общий сервер и все его инструменты, и «с подтверждением», включены по умолчанию
        *open_dialog(sid), state(sid),
        ("eval", "labels", f"""[...{row(sid)}.querySelectorAll('.chat-mcp-tool-row')].map(li => [li.dataset.tool,
          [...li.querySelectorAll('.tag')].map(t => t.textContent),
          li.querySelector('.chat-mcp-tool-text')?.textContent])"""),
        ("eval", "names", f"[{row(sid)}.querySelector('.chat-mcp-tool-row[data-tool=lookup] .chat-mcp-tool-name').textContent,"
                          f" {row(sid)}.querySelector('.chat-mcp-tool-row[data-tool=lookup] code')?.textContent]"),
        ("eval", "rules", f"""(() => {{ const d = {row(sid)}.querySelector('details.chat-mcp-rules');
          return [d.open, d.querySelector('summary').textContent, d.querySelector('.chat-mcp-instructions').textContent]; }})()"""),
        ("eval", "openTools", f"{row(sid)}.querySelector('details.chat-mcp-tools').open = true"),
        pick("lookup"),
        ("eval", "count", f"{row(sid)}.querySelector('.chat-mcp-count').textContent"),
        # «Обновить список» перечитывает каталог, несохранённые переключатели остаются
        ("click", "#chat-mcp-refresh"),
        ("wait", f"!document.querySelector('#chat-mcp-refresh').disabled && {row(sid)}"),
        ("eval", "afterRefresh", state(sid)[2]),
        ("eval", "dialogFits", f"document.querySelector('{DIALOG}').scrollWidth <= innerWidth && {NO_OVERFLOW}"),
        ("click", "#chat-mcp-save"), ("wait", f"!{is_open(DIALOG)}"),
        # черновик уходит в диалог после его создания
        ("type", "#chat-input", "Привет"), ("press", "Enter"), ("wait", ANSWERED),
        ("eval", "created", chip_matches_view()),
        *reload_chat(), *open_dialog(sid), ("eval", "kept", state(sid)[2]),
        # выключить общий сервер в открытом диалоге — `mcp_off`
        ("eval", "serverOff", f"{row(sid)}.querySelector('.chat-mcp-server').click()"),
        ("eval", "toolsLocked", f"{tool_switch(sid, 'video_translate')}.disabled"),
        ("click", "#chat-mcp-save"), ("wait", f"!{is_open(DIALOG)}"),
        ("eval", "switchedOff", chip_matches_view()),
        *reload_chat(), *open_dialog(sid), ("eval", "keptOff", state(sid)[2]),
        ("eval", "composerFits", NO_OVERFLOW)]
    result = browser.drive(live.url + "/", steps, width=width, mobile=mobile)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["draftChip"] is True
    assert values["state"] == [True, True, True, True]
    assert values["labels"] == [["lookup", [], "desc lookup"], ["web_search", ["интернет"], "desc web_search"],
                                ["video_translate", ["с подтверждением"], "desc video_translate"]]
    assert values["names"] == ["Поиск по базе", "lookup"]
    assert values["rules"] == [False, "Правила сервера", RULES]
    assert values["count"] == "Инструменты: 2 из 3"
    assert values["afterRefresh"] == [True, False, True, True]
    assert values["dialogFits"] is True and values["composerFits"] is True
    matches, chat = values["created"]
    assert matches and sid in chat["mcp_ids"] and sid not in chat["mcp_off"]
    assert chat["mcp_tools_off"] == {str(sid): ["lookup"]}
    assert chat["mcp_tools_on"] == {}
    assert values["kept"] == [True, False, True, True]
    assert values["toolsLocked"] is True
    matches, chat = values["switchedOff"]
    assert matches and sid not in chat["mcp_ids"] and chat["mcp_off"] == [sid]
    assert chat["mcp_tools_on"] == {}
    assert values["keptOff"] == [False, False, True, True]
    # первый ход: «с подтверждением» предложен; выключенный и веб-инструмент без «Интернета» — нет
    offered = [t["function"]["description"].split(". ", 1)[0] for r in live.gw.chat_requests
               for t in r.get("tools", []) if t["function"]["name"].startswith(f"mcp_{sid}_")]
    assert offered == [f"{name}: video_translate"]
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_default_off_shared_server_is_switched_on_in_the_dialog(browser, live, width):
    """Решение владельца 2026-11-22: общий MCP без «В диалоге по умолчанию» в новом диалоге не отмечен, с пометкой
    «включается в диалоге вручную» и не в счёте чипа; отметка включает его в этом диалоге. Его список длиннее
    `mcp_client.MAX_TOOLS` — под ответом заметка, а плашка ответа без ошибки."""
    login, name, path = f"mcp-manual-{width}", f"Вручную {width}", f"/mcp-manual-{width}"
    server(live, path, tools=[tool(f"t{n}") for n in range(129)])
    client, sid = admin_client(live), None
    try:
        response = client.post("/api/admin/shared/mcp", json={"name": name, "url": HOST + path, "token": MCP_KEY,
                                                              "dialog_default": False})
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
        live.add_user(login)
        mobile = width == 320
        badge = "document.querySelector('#messages .msg.assistant .web-badge')"
        steps = [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"
                     f" && {TOGGLE} && !{TOGGLE}.hidden"),
            ("eval", "draftChip", f"""(async () => {{ const m = {API};
              const shared = (await m.api('GET', '/api/mcp')).items
                .filter(s => s.shared && s.enabled && s.dialog_default !== false).length;
              return {TOGGLE}.textContent === (shared ? 'MCP (' + shared + ')' : 'MCP'); }})()"""),
            *open_dialog(sid),
            ("eval", "before", f"[{row(sid)}.querySelector('.chat-mcp-server').checked,"
                               f" {row(sid)}.querySelector('.chat-mcp-manual')?.textContent]"),
            ("eval", "serverOn", f"{row(sid)}.querySelector('.chat-mcp-server').click()"),
            ("click", "#chat-mcp-save"), ("wait", f"!{is_open(DIALOG)}"),
            ("type", "#chat-input", "Привет"), ("press", "Enter"), ("wait", ANSWERED),
            ("eval", "created", chip_matches_view()),
            ("wait", f"{badge} && !{badge}.hidden && !{badge}.classList.contains('busy')"),
            ("eval", "badge", f"[{badge}.textContent, {badge}.classList.contains('bad')]"),
            *reload_chat(), *open_dialog(sid), ("eval", "kept", f"{row(sid)}.querySelector('.chat-mcp-server').checked"),
            ("eval", "dialogFits", f"document.querySelector('{DIALOG}').scrollWidth <= innerWidth && {NO_OVERFLOW}")]
        result = browser.drive(live.url + "/", steps, width=width, mobile=mobile)
    finally:
        if sid is not None:                      # не мешать счёту общих серверов в соседних тестах
            client.delete(f"/api/admin/shared/mcp/{sid}")
        client.close()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["draftChip"] is True
    assert values["before"] == [False, "включается в диалоге вручную"]
    matches, chat = values["created"]
    assert matches and chat["mcp_ids"][0] == sid
    text, bad = values["badge"]
    assert not bad and "ошибк" not in text and "заметк" in text, text
    assert values["kept"] is True and values["dialogFits"] is True
    offered = [t["function"]["description"].split(". ", 1)[0] for t in live.gw.chat_requests[-1].get("tools", [])
               if t["function"]["name"].startswith(f"mcp_{sid}_")]
    assert len(offered) == 128 and offered[0] == f"{name}: t0"
    assert not problems(result), result.get("log")


WEB_TOGGLE = "document.querySelector('#web-toggle')"


@pytest.mark.parametrize("width", [320, 1280])
def test_internet_without_exa_key_opens_for_mcp_web_tools(browser, live, width):
    """Находка 2026-11-22: без ключа Exa «Интернет» включается, когда набор MCP диалога даёт веб-инструмент; подсказка
    — только веб-инструменты MCP; включённый до создания диалога уходит в него, и ход предлагает веб-инструмент."""
    login = f"mcp-web-{width}"
    sid = shared_server(live, f"Веб {width}", f"/mcp-web-{width}")
    live.add_user(login)
    mobile = width == 320
    try:
        steps = [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"
                     f" && {WEB_TOGGLE} && !{WEB_TOGGLE}.hidden && !{WEB_TOGGLE}.disabled"),
            ("eval", "before", f"[{WEB_TOGGLE}.getAttribute('aria-pressed'), {WEB_TOGGLE}.title,"
                               " document.querySelector('#web-note').textContent]"),
            ("click", "#web-toggle"), ("wait", f"{WEB_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("type", "#chat-input", "Найди новости"), ("press", "Enter"), ("wait", ANSWERED),
            ("eval", "created", chip_matches_view()),
            ("eval", "after", f"[{WEB_TOGGLE}.getAttribute('aria-pressed'), {WEB_TOGGLE}.disabled]"),
            ("eval", "composerFits", NO_OVERFLOW)]
        result = browser.drive(live.url + "/", steps, width=width, mobile=mobile)
    finally:
        client = admin_client(live)
        try:
            client.delete(f"/api/admin/shared/mcp/{sid}")
        finally:
            client.close()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["before"] == ["false", "Интернет: только веб-инструменты MCP — для поиска Studio задайте ключ Exa"
                                         " в Настройках", ""]
    _, chat = values["created"]
    assert chat["web"] is True and chat["web_tools"] == []
    assert values["after"] == ["true", False] and values["composerFits"] is True
    offered = [t["function"]["description"].split(". ", 1)[0] for t in live.gw.chat_requests[-1].get("tools", [])
               if t["function"]["name"].startswith(f"mcp_{sid}_")]
    assert f"Веб {width}: web_search" in offered
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_sets_tool_policy_in_the_access_dialog(browser, live, width):
    name = f"Политика {width}"
    sid = shared_server(live, name, f"/mcp-policy-{width}")
    item = f"#admin-shared-mcp [data-mcp-id='{sid}']"
    tool_row = lambda n: f"#mcp-access-dialog .access-tool[data-tool={n}]"
    open_access = [("wait", f"document.querySelector(\"{item} .mcp-access\")"), ("click", f"{item} .mcp-access"),
                   ("wait", f"document.querySelector('{tool_row('lookup')}') && !document.querySelector('#mcp-access-save').disabled")]
    selects = ("eval", "selects", f"""['lookup', 'video_translate'].map(n => [
      document.querySelector('#mcp-access-dialog .access-tool[data-tool=' + n + '] .mcp-policy-web').value,
      document.querySelector('#mcp-access-dialog .access-tool[data-tool=' + n + '] .mcp-policy-confirm').value])""")
    steps = [*signin(), ("wait", READY), *go("admin"), *open_access,
        ("eval", "defaults", selects[2]),
        ("eval", "options", f"[...document.querySelector('{tool_row('lookup')} .mcp-policy-web').options].map(o => o.textContent)"),
        set_value(f"{tool_row('lookup')} .mcp-policy-web", "yes"),
        set_value(f"{tool_row('video_translate')} .mcp-policy-confirm", "never"),
        ("eval", "dialogFits", "document.querySelector('#mcp-access-dialog').scrollWidth <= innerWidth"),
        ("click", "#mcp-access-save"), ("wait", f"!{is_open('#mcp-access-dialog')}"),
        *open_access, selects,
        ("eval", "policy", f"""(async () => {{ const m = {API};
          return await m.api('GET', '/api/admin/shared/mcp/{sid}/policy'); }})()"""),
        ("eval", "catalog", f"""(async () => {{ const m = {API};
          const s = (await m.api('GET', '/api/mcp/catalog')).items.find(i => i.id === {sid});
          return s.tools.map(t => [t.name, t.open_world, t.confirm]); }})()""")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["defaults"] == [["", ""], ["", ""]]
    assert values["options"] == ["Интернет: как у сервера", "Интернет: да", "Интернет: нет"]
    assert values["dialogFits"] is True
    assert values["selects"] == [["yes", ""], ["", "never"]]
    assert values["policy"] == {"tools": {"lookup": {"open_world": True, "confirm": None},
                                          "video_translate": {"open_world": None, "confirm": "never"}}}
    # политика сильнее подсказок сервера: lookup — веб-инструмент, video_translate — без подтверждения
    assert values["catalog"] == [["lookup", True, False], ["web_search", True, False], ["video_translate", False, False]]
    assert not problems(result), result.get("log")
