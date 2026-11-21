"""Файлы между чатом и MCP на странице (chat-files.js, спека 2026-11-21-studio-source-foundation-design §5 и §7a п. 1,
3): запрос согласия в ленте старого диалога после включения общего MCP с `studio/chat-files`, «Передать» и «Не
передавать», звук в поле ввода только при таком сервере и в его пределе, файлы результата под ответом."""
from contextlib import closing
import json

import httpx
import pytest

import auth
from fakes import PNG_REAL, TOKEN as GW_TOKEN
from test_chat_web import call
from test_documents import pdf
from test_mcp import TOKEN as MCP_KEY
from test_mcp_dialog import HOST
from test_ui_browser import (ANSWERED, API, CHAT_ID, GATEWAY_URL, NO_OVERFLOW, READY, WEB_TOGGLE, browser,  # noqa: F401
                             is_open, live, open_new_chat, problems, signin)
from test_ui_chat_mcp import DIALOG, admin_client, open_dialog, row, tool_switch

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

LIMIT = 64 << 10
IMAGE = "art_" + "1" * 32
DOC = "art_" + "2" * 32
DOCX = b"PK\x03\x04docx-result-bytes"
LAST = "document.querySelector('#messages .msg.assistant:last-child')"


def files_server(live, path):
    """Мок MCP: `artifact_upload` с пометкой `studio/chat-files` (предел LIMIT) и `make`, чей результат — картинка и
    docx. Итог — загрузки: (имя файла, число запросов к модели к этому мигу)."""
    uploads = []
    upload = {"name": "artifact_upload", "description": "upload", "_meta": {"studio/chat-files": {"max_bytes": LIMIT}},
              "inputSchema": {"type": "object", "properties": {"data_base64": {"type": "string"},
                                                               "mime_type": {"type": "string"}, "name": {"type": "string"}}}}
    tools = [upload, {"name": "make", "description": "make files", "inputSchema": {"type": "object"}}]

    async def handler(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        method, params = payload["method"], payload.get("params", {})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif params.get("name") == "artifact_upload":
            uploads.append((params["arguments"].get("name"), len(live.gw.chat_requests)))
            n = len(uploads)
            result = {"content": [{"type": "text", "text": "ok"}],
                      "structuredContent": {"id": "art_" + f"{n:032x}", "path": f"/data/in/{n}"}}
        else:
            result = {"content": [{"type": "text", "text": "made"}], "structuredContent": {"artifacts": [
                {"id": IMAGE, "mime_type": "image/png", "name": "out.png", "download_path": f"/artifacts/{IMAGE}/content"},
                {"id": DOC, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "name": "report.docx", "download_path": f"/artifacts/{DOC}/content"}]}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def content(data, mime):
        async def reply(request):
            return httpx.Response(200, content=data, headers={"content-length": str(len(data)), "content-type": mime})
        return reply

    live.gw.on[path] = handler
    live.gw.on[f"/artifacts/{IMAGE}/content"] = content(PNG_REAL, "image/png")
    live.gw.on[f"/artifacts/{DOC}/content"] = content(DOCX, "application/octet-stream")
    return uploads


def member_client(live, login) -> httpx.Client:
    """httpx-сессия участника с CSRF и подключением шлюза в разделе «Чат»."""
    client = httpx.Client(base_url=live.url, headers={"Origin": live.url}, timeout=30)
    assert client.post("/login", data={"login": login, "password": "password-123"}).status_code == 303
    for cookie in client.cookies.jar:
        cookie.secure = False
    client.headers[auth.CSRF_HEADER] = client.get("/api/me").json()["csrf"]
    made = client.post("/api/connections", json={"name": "Шлюз", "kind": "srvllm", "base_url": GATEWAY_URL,
                                                 "token": GW_TOKEN, "sections": ["chat"]})
    assert made.status_code == 201, made.text
    return client


def old_dialog(client, name, data, mime) -> str:
    """Диалог с вложением и ответом — до появления MCP с файлами чата."""
    cid = client.post("/api/chats", json={}).json()["id"]
    att = client.post("/api/attachments", files={"file": (name, data, mime)})
    assert att.status_code == 201, att.text
    done = client.post(f"/api/chats/{cid}/turns", json={"text": "Прочитай", "attachment_ids": [att.json()["id"]]})
    assert done.status_code == 200 and "event: done" in done.text, done.text
    return cid


def open_chat(cid) -> list:
    return [("eval", "go", f"location.hash = '#/chat/{cid}'"),
            ("wait", f"location.hash === '#/chat/{cid}' && document.querySelectorAll('#messages .msg').length >= 2"
                     " && !document.querySelector('#send').hidden")]


def say(text) -> list:
    return [("type", "#chat-input", text), ("press", "Enter"), ("wait", ANSWERED)]


def media_file(name, size) -> tuple:
    return ("eval", "attached", f"""(() => {{ const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array({size})], '{name}', {{ type: 'audio/mpeg' }}));
      const input = document.querySelector('#attach'); input.files = dt.files;
      input.dispatchEvent(new Event('change', {{ bubbles: true }})); return input.files.length; }})()""")


def chat_of(cid) -> str:
    return f"(async () => {{ const m = {API}; return await m.api('GET', '/api/chats/{cid}'); }})()"


ACCEPT = "document.querySelector('#attach').accept"


@pytest.mark.parametrize("width", [320, 1280])
def test_consent_media_and_result_files_in_chat(browser, live, width):
    login = f"chat-files-{width}"
    live.add_user(login)
    client = member_client(live, login)
    admin = admin_client(live)
    sid = None
    try:
        with_pdf = old_dialog(client, "договор.pdf", pdf("Payment is due within five days"), "application/pdf")
        with_note = old_dialog(client, "заметки.txt", "Секретные заметки".encode(), "text/plain")
        without = client.post("/api/chats", json={}).json()["id"]
        # администратор включает общий MCP, принимающий файлы чата: прежние диалоги получают его по умолчанию
        uploads = files_server(live, f"/chat-files-{width}")
        made = admin.post("/api/admin/shared/mcp", json={"name": f"Файлы {width}", "url": HOST + f"/chat-files-{width}",
                                                          "token": MCP_KEY})
        assert made.status_code == 201, made.text
        sid = made.json()["id"]
        assert client.patch(f"/api/chats/{without}", json={"mcp_off": [sid]}).status_code == 200
        before = len(live.gw.chat_requests)
        live.gw.chat_script = [["Файл есть"], {"tool_calls": [call(f"mcp_{sid}_1")]}, ["Готово"], ["Б1"], ["Б2"]]
        consent = f"{LAST}.querySelector('.msg-consent')"
        steps = [*signin(login), ("wait", READY),
            # старый диалог с PDF: текстовое сообщение — серверу ничего, в ленте запрос согласия
            *open_chat(with_pdf), ("eval", "acceptAsk", ACCEPT), *say("Что в файле?"),
            ("wait", f"{consent}?.querySelector('.consent-allow')"),
            ("eval", "ask", f"{consent}.querySelector('.msg-consent-text').textContent"),
            ("eval", "askFits", NO_OVERFLOW),
            ("click", ".msg-consent .consent-allow"),
            ("wait", f"{consent} && !{consent}.querySelector('button')"),
            ("eval", "allowedText", f"{consent}.textContent"),
            ("eval", "allowedView", f"{chat_of(with_pdf)}.then(d => d.chat.mcp_file_consent)"),
            ("eval", "acceptAllowed", ACCEPT),
            # звук больше предела — отказ до загрузки; в пределе — в диалог
            media_file("big.mp3", LIMIT + 1),
            ("wait", "document.querySelector('#chat-media .att-chip .error')"),
            ("eval", "tooBig", "document.querySelector('#chat-media .att-chip').textContent"),
            ("eval", "noMedia", f"{chat_of(with_pdf)}.then(d => d.media.length)"),
            ("click", "#chat-media .att-remove"), ("wait", "!document.querySelector('#chat-media .att-chip')"),
            media_file("voice.mp3", 1000),
            ("wait", "[...document.querySelectorAll('#chat-media .chat-media-chip')].some(c => c.dataset.id"
                     " && c.textContent.includes('voice.mp3'))"),
            ("eval", "mediaChip", "document.querySelector('#chat-media .chat-media-chip').textContent"),
            # следующий ход: файлы уходят серверу, результат — картинка и docx под ответом
            *say("Сделай файлы"),
            ("wait", f"{LAST}.querySelector('.msg-files img')?.complete"
                     f" && {LAST}.querySelector('.msg-files img').naturalWidth > 0"),
            ("eval", "files", f"""(async () => {{ const box = {LAST}.querySelector('.msg-files');
              const link = box.querySelector('a.msg-file-link'); const r = await fetch(link.href);
              return [box.querySelectorAll('img[loading=lazy]').length, link.textContent, link.hasAttribute('download'),
                r.status, (await r.arrayBuffer()).byteLength]; }})()"""),
            ("eval", "filesFit", NO_OVERFLOW),
            ("click", ".msg-files .msg-file-image"), ("wait", is_open("#msg-file-dialog")),
            ("eval", "preview", "[document.querySelector('#msg-file-dialog img').src.includes('/files/0/0'),"
                                " document.querySelector('#msg-file-save').getAttribute('href').includes('/files/0/0')]"),
            ("click", "#msg-file-dialog .dialog-close"), ("wait", f"!{is_open('#msg-file-dialog')}"),
            ("click", "#chat-media .att-remove"), ("wait", is_open("#chat-media-delete-dialog")),
            ("click", "#chat-media-delete-confirm"), ("wait", "!document.querySelector('#chat-media .chat-media-chip')"),
            ("eval", "afterDelete", f"{chat_of(with_pdf)}.then(d => d.media)"),
            # «Не передавать»: запрос больше не повторяется, звук не предлагается
            *open_chat(with_note), *say("А здесь?"),
            ("wait", f"{consent}?.querySelector('.consent-refuse')"),
            ("click", ".msg-consent .consent-refuse"),
            ("wait", f"{consent} && !{consent}.querySelector('button')"),
            ("eval", "refusedText", f"{consent}.textContent"),
            ("eval", "acceptRefused", ACCEPT),
            *say("Ещё вопрос"),
            ("eval", "askedAgain", f"!!{consent}"),
            # MCP выключен в диалоге — звук не предлагается
            ("eval", "go", f"location.hash = '#/chat/{without}'"),
            ("wait", f"location.hash === '#/chat/{without}' && document.querySelector('#messages .chat-empty')"),
            ("eval", "acceptOff", ACCEPT),
            # новый диалог: звук спрашивает согласие, диалог создаётся из черновика, звук — в нём
            *open_new_chat(width == 320), ("wait", f"{ACCEPT}.includes('audio/*')"),
            media_file("first.mp3", 2000), ("wait", is_open("#media-consent-dialog")),
            ("eval", "newAsk", "document.querySelector('#media-consent-dialog p').textContent"),
            ("click", "#media-consent-allow"),
            ("wait", f"{CHAT_ID} && [...document.querySelectorAll('#chat-media .chat-media-chip')].some(c => c.dataset.id)"),
            ("eval", "created", f"""(async () => {{ const m = {API};
              const d = await m.api('GET', '/api/chats/' + location.hash.split('/')[2]);
              return [d.chat.mcp_file_consent, d.media.map(x => x.name), d.messages.length]; }})()""")]
        result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
        assert not result.get("failed"), result
        values = result["values"]
        assert "audio/*" in values["acceptAsk"]                  # сервер есть, решения ещё нет — спросим при выборе
        assert values["ask"] == f"Передать серверу «Файлы {width}» файлы диалога: договор.pdf?"
        assert values["askFits"] is True and values["filesFit"] is True
        assert "разрешено" in values["allowedText"]
        assert values["allowedView"] == {str(sid): "allowed"}
        assert "audio/*" in values["acceptAllowed"] and "video/*" in values["acceptAllowed"]
        assert "big.mp3" in values["tooBig"] and "64 КиБ" in values["tooBig"] and values["noMedia"] == 0
        assert "voice.mp3" in values["mediaChip"] and "1 КиБ" in values["mediaChip"]
        assert values["files"] == [1, "Скачать «report.docx»", True, 200, len(DOCX)]
        assert values["preview"] == [True, True]
        assert values["afterDelete"] == []
        assert "не передаются" in values["refusedText"]
        assert "audio/*" not in values["acceptRefused"] and values["askedAgain"] is False
        assert "audio/*" not in values["acceptOff"]
        assert values["newAsk"] == f"Передать серверу «Файлы {width}» файлы диалога: first.mp3?"
        assert values["created"] == [{str(sid): "allowed"}, ["first.mp3"], 0]
        # ход с запросом согласия — без загрузок; после «Передать» — PDF и звук, заметки отказавшего диалога — никогда
        assert [name for name, _ in uploads] == ["договор.pdf", "voice.mp3"]
        assert min(at for _, at in uploads) >= before + 1
        assert not problems(result), result.get("log")
    finally:
        if sid is not None:
            admin.delete(f"/api/admin/shared/mcp/{sid}")
        admin.close()
        client.close()


def test_media_picker_follows_upload_policy(browser, live):
    login = "chat-files-policy"
    live.add_user(login)
    files_server(live, "/chat-files-policy")
    with closing(admin_client(live)) as admin, closing(member_client(live, login)) as client:
        made = admin.post("/api/admin/shared/mcp", json={"name": "Protected files", "url": HOST + "/chat-files-policy",
                                                        "token": MCP_KEY})
        assert made.status_code == 201, made.text
        sid = made.json()["id"]
        try:
            policy = {"artifact_upload": {"open_world": True, "confirm": "always"}}
            assert admin.put(f"/api/admin/shared/mcp/{sid}/policy", json={"tools": policy}).status_code == 200
            assert client.put("/api/me/exa-key", json={"key": "exa-test-key"}).status_code == 204
            cid = old_dialog(client, "note.txt", b"note", "text/plain")
            supports_audio = f"{ACCEPT}.includes('audio/*')"
            switch = tool_switch(sid, "artifact_upload")
            result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *open_chat(cid),
                *open_dialog(sid), ("eval", "initial", supports_audio),
                ("eval", "openTools", f"{row(sid)}.querySelector('.chat-mcp-tools').open = true"),
                ("eval", "enableUpload", f"{switch}.click()"),
                ("click", "#chat-mcp-save"), ("wait", f"!{is_open(DIALOG)}"),
                ("eval", "withoutWeb", supports_audio),
                ("click", "#web-toggle"), ("wait", f"{WEB_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
                ("wait", supports_audio), ("eval", "allowed", ACCEPT),
                ("click", "#web-toggle"), ("wait", f"!{supports_audio}")], width=1280)
            assert not result.get("failed"), result
            assert result["values"]["initial"] is False and result["values"]["withoutWeb"] is False
            assert "video/*" in result["values"]["allowed"]
            assert not problems(result), result.get("log")
        finally:
            admin.delete(f"/api/admin/shared/mcp/{sid}")
