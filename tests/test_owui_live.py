"""Живая приёмка раздела «Open WebUI» (спека 2026-11-22-studio-open-webui-design, «Тесты»): Студия в процессе теста
с настоящей сетью ходит в настоящий Open WebUI тестового пользователя — подключение (сертификат подтверждается по
отпечатку, как на странице), дерево, новый чат с потоком до `done`, продолжение, «Остановить», вложение картинки,
вопрос модели и `resolve` (если модель его задала), отзыв сессии → `relogin` и вход заново. В конце созданные чаты
и файлы удаляются в самом Open WebUI, учётка — в Студии (сессия «Studio» отзывается).

Адрес и учётные данные — только окружением процесса: `OWUI_LIVE_URL` (`https://10.77.1.x:порт`), `OWUI_LIVE_EMAIL`,
`OWUI_LIVE_PASSWORD`; модель — `OWUI_LIVE_MODEL` (без неё — модель по умолчанию учётки). Без них — пропуск. Команда —
docs/architecture/open-webui.md, «Проверки»."""
import json
import logging
import os
import struct
import time
import zlib
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

import httpx
import pytest

from conftest import make_user, signin
from tools.pytest_support import new_event_loop

pytestmark = pytest.mark.manual

URL = os.environ.get("OWUI_LIVE_URL", "").rstrip("/")
EMAIL = os.environ.get("OWUI_LIVE_EMAIL", "")
PASSWORD = os.environ.get("OWUI_LIVE_PASSWORD", "")
MODEL = os.environ.get("OWUI_LIVE_MODEL", "")
NEED = "OWUI_LIVE_URL, OWUI_LIVE_EMAIL and OWUI_LIVE_PASSWORD are required"
NO_QUESTION = "the model answered without asking a question (ask_user)"
BASE = "/api/owui/accounts"
#: срок одного ответа модели (холодная модель на общей карте отвечает не сразу)
ANSWER_SEC = 300


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class Events:
    """SSE учётки через настоящий маршрут `GET …/events`; пинги службы (owui.PING_SEC) будят ожидание и без событий."""

    def __init__(self, client, aid: int):
        self.stack = ExitStack()
        response = self.stack.enter_context(client.stream("GET", f"{BASE}/{aid}/events"))
        assert response.status_code == 200, response.read()
        self.lines = response.iter_lines()
        self.seen: list[tuple[str, dict]] = []

    def until(self, match, seconds: float = ANSWER_SEC) -> tuple[str, dict]:
        deadline, kind = time.monotonic() + seconds, None
        for line in self.lines:
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: ") and kind is not None:
                found = (kind, json.loads(line[len("data: "):]))
                kind = None
                self.seen.append(found)
                if match(*found):
                    return found
            if time.monotonic() > deadline:
                raise AssertionError(f"не дождались события за {seconds} с: {[k for k, _ in self.seen[-20:]]}")
        raise AssertionError(f"поток кончился: {[k for k, _ in self.seen[-20:]]}")

    def text(self, message_id: str) -> str:
        return "".join(d["text"] for k, d in self.seen if k == "delta" and d.get("message_id") == message_id)

    def close(self) -> None:
        self.stack.close()


def png() -> bytes:
    """Красный квадрат 16×16 — PNG без сторонних пакетов."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 16 for _ in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Live:
    def __init__(self, client):
        self.c = client
        self.aid: int | None = None
        self.model = MODEL
        self.chat: str | None = None
        self.chats: list[str] = []
        self.files: list[str] = []
        self.events: Events | None = None
        self.pem: str | None = None
        self.records = Records()

    def stream(self) -> Events:
        if self.events is None:
            self.events = Events(self.c, self.aid)
        return self.events

    def reopen(self) -> Events:
        if self.events is not None:
            self.events.close()
            self.events = None
        return self.stream()

    @contextmanager
    def owui(self):
        """Прямой клиент Open WebUI для подготовки и уборки — с тем же доверенным сертификатом, что принял тест."""
        import state
        import upstream
        if self.pem is None:                                # после удаления учётки строки уже нет — помним PEM
            self.pem = state.DB.execute("SELECT cert_pem FROM owui_accounts WHERE id = ?",
                                        (self.aid,)).fetchone()["cert_pem"] or ""
        pem = self.pem
        with httpx.Client(base_url=URL, verify=upstream.pinned_context(pem) if pem else True, trust_env=False,
                          timeout=30) as client:
            r = client.post("/api/v1/auths/signin", json={"email": EMAIL, "password": PASSWORD})
            assert r.status_code == 200, r.status_code
            client.headers["Authorization"] = "Bearer " + r.json()["token"]
            yield client

    def answer(self, message_id: str) -> dict:
        _, data = self.stream().until(lambda k, d: k == "done" and d.get("message_id") == message_id)
        return data

    def view(self) -> dict:
        r = self.c.get(f"{BASE}/{self.aid}/chats/{self.chat}")
        assert r.status_code == 200, r.text
        return r.json()

    def send(self, text: str, files: list[str] | None = None) -> str:
        parent = self.view()["messages"][-1]["id"]
        r = self.c.post(f"{BASE}/{self.aid}/chats/{self.chat}/messages",
                        json={"model": self.model, "text": text, "files": files or [], "parent_id": parent})
        assert r.status_code == 200, r.text
        assert r.json()["queued"] is False
        return r.json()["message_id"]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if not (URL and EMAIL and PASSWORD):
        pytest.skip(NEED)
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    import main
    import state
    import upstream

    root = tmp_path_factory.mktemp("owui-live")
    (root / "media").mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STUDIO_DATA_KEY", Fernet.generate_key().decode())
        mp.setenv("STUDIO_DB", str(root / "studio.db"))
        mp.setenv("STUDIO_MEDIA_DIR", str(root / "media"))
        mp.setenv("STUDIO_ORIGIN", "https://testserver")
        mp.setattr(state, "WATCHER", False)
        with TestClient(main.app, base_url="https://testserver",
                        backend_options={"loop_factory": new_event_loop}) as client:
            s = Live(client)
            logging.getLogger("studio").addHandler(s.records)
            try:
                make_user(SimpleNamespace(c=client), "owner")
                signin(SimpleNamespace(c=client), "owner")
                entry = upstream._entry(httpx.URL(URL))
                state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                                 (json.dumps([*upstream.allowlist(), entry]),))
                yield s
            finally:
                try:
                    if s.events is not None:
                        s.events.close()
                    if s.aid is not None:
                        _clean(s)
                finally:
                    logging.getLogger("studio").removeHandler(s.records)


def _clean(s: Live) -> None:
    """Тестовые чаты и файлы — в самом Open WebUI (у Студии удаления чатов нет); учётку — в Студии: её сессия
    «Studio» в Open WebUI отзывается."""
    import owui_tokens
    session = owui_tokens.load(s.aid).session_id
    try:
        with s.owui() as owui:
            for chat in s.chats:
                owui.delete(f"/api/v1/chats/{chat}")
            for file in s.files:
                owui.delete(f"/api/v1/files/{file}")
            left = owui.get("/api/v1/chats/?page=1").text
            assert not any(chat in left for chat in s.chats)
    finally:
        assert s.c.delete(f"{BASE}/{s.aid}").status_code == 204
    with s.owui() as owui:
        sessions = owui.get("/api/v1/auths/sessions")
        assert sessions.status_code == 200 and session not in sessions.text


@pytest.mark.timeout(120)
def test_connect_confirms_the_certificate_and_reads_the_tree(live):
    body = {"label": "live", "url": URL, "email": EMAIL, "password": PASSWORD}
    r = live.c.post(BASE, json=body)
    if r.status_code == 409 and r.json()["detail"]["code"] == "certificate":
        detail = r.json()["detail"]
        assert len(detail["sha256"].replace(":", "")) == 64
        r = live.c.post(BASE, json={**body, "cert_sha256": detail["sha256"]})
    assert r.status_code == 201, r.text
    account = r.json()
    live.aid = account["id"]
    assert account["state"] == "ok" and account["who"]["email"] == EMAIL
    assert tuple(int(n) for n in account["version"].split(".")[:2]) >= (0, 13)
    tree = live.c.get(f"{BASE}/{live.aid}/tree")
    assert tree.status_code == 200, tree.text
    assert {"pinned", "folders", "chats"} <= set(tree.json())
    models = live.c.get(f"{BASE}/{live.aid}/models").json()
    assert models["models"] and not any(m["id"].startswith("agent:") for m in models["models"])
    live.model = live.model or models["default"]
    assert live.model in {m["id"] for m in models["models"]}


@pytest.mark.timeout(ANSWER_SEC + 60)
def test_new_chat_streams_until_done(live):
    events = live.stream()
    r = live.c.post(f"{BASE}/{live.aid}/chats", json={"model": live.model, "text": "Reply with one word: pong"})
    assert r.status_code == 201, r.text
    live.chat, message = r.json()["chat_id"], r.json()["message_id"]
    live.chats.append(live.chat)
    assert live.answer(message)["error"] is None
    assert events.text(message).strip()                     # ответ шёл потоком, а не только итогом
    messages = live.view()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["id"] == message and messages[-1]["done"] is True and messages[-1]["text"].strip()


@pytest.mark.timeout(ANSWER_SEC + 60)
def test_continue_adds_a_turn(live):
    message = live.send("And now reply with one word: ping")
    assert live.answer(message)["error"] is None
    messages = live.view()["messages"]
    assert len(messages) == 4 and messages[-1]["id"] == message and messages[-1]["text"].strip()


@pytest.mark.timeout(2 * ANSWER_SEC)
def test_stop_ends_the_answer(live):
    message = live.send("Count from 1 to 400, one number per line, no other text.")
    events = live.stream()
    events.until(lambda k, d: k == "delta" and d.get("message_id") == message)
    assert live.c.post(f"{BASE}/{live.aid}/chats/{live.chat}/stop").status_code == 204
    events.until(lambda k, d: (k == "done" and d.get("message_id") == message)
                 or (k == "active" and d.get("chat_id") == live.chat and d.get("active") is False), 120)
    for _ in range(30):
        if not live.view()["active"]:
            break
        time.sleep(1)
    assert live.view()["active"] is False


@pytest.mark.timeout(ANSWER_SEC + 120)
def test_image_attachment_goes_with_the_message(live):
    r = live.c.post(f"{BASE}/{live.aid}/files", content=png(),
                    headers={"X-Studio-Name": "red-square.png", "Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    upload = r.json()
    live.files.append(upload["id"])
    assert (upload["name"], upload["kind"]) == ("red-square.png", "image") and not upload.get("failed")
    message = live.send("What colour is this picture? One word.", [upload["id"]])
    live.answer(message)                                    # модель без зрения может ответить ошибкой — не про Студию
    sent = live.view()["messages"][-2]
    assert sent["role"] == "user" and upload["id"] in [f["id"] for f in sent["files"]]
    picture = live.c.get(f"{BASE}/{live.aid}/files/{upload['id']}")
    assert picture.status_code == 200 and picture.headers["cache-control"] == "no-store"
    assert picture.content == png()


@pytest.mark.timeout(2 * ANSWER_SEC + 60)
def test_model_question_is_resolved_from_studio(live):
    message = live.send("Before answering you must call the ask_user tool and ask me which colour I prefer: red or "
                        "blue. After my answer, reply with one sentence.")
    kind, _ = live.stream().until(lambda k, d: k in ("pending", "done") and d.get("message_id") == message)
    paused = live.view()["messages"][-1]["pending"]
    if kind == "done" or not paused:
        pytest.skip(NO_QUESTION)
    if paused["kind"] == "question":
        answers = {q["id"]: {"type": "option", "option_index": 0, "label": q["options"][0]["label"],
                             "description": q["options"][0].get("description") or ""}
                   for q in paused["questions"]}
        body = {"message_id": message, "call_id": paused["call_id"], "action": "answer", "answers": answers}
    else:
        body = {"message_id": message, "call_id": paused["call_id"], "action": "approve"}
    r = live.c.post(f"{BASE}/{live.aid}/chats/{live.chat}/resolve", json=body)
    assert r.status_code == 204, r.text
    live.answer(message)
    last = live.view()["messages"][-1]
    assert last["pending"] is None and last["done"] is True
    again = live.c.post(f"{BASE}/{live.aid}/chats/{live.chat}/resolve", json=body)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "resolved"


@pytest.mark.timeout(180)
def test_revoked_session_needs_relogin(live):
    import owui_tokens
    session = owui_tokens.load(live.aid).session_id
    with live.owui() as owui:
        assert owui.delete(f"/api/v1/auths/sessions/{session}").status_code == 200
    r = live.c.get(f"{BASE}/{live.aid}/tree")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "relogin"
    [account] = live.c.get(BASE).json()
    assert account["state"] == "relogin"
    r = live.c.post(f"{BASE}/{live.aid}/login", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json()["state"] == "ok", r.text
    assert owui_tokens.load(live.aid).session_id != session
    assert live.c.get(f"{BASE}/{live.aid}/tree").status_code == 200
    events = live.reopen()                                  # прежний поток кончился на `link relogin`
    events.until(lambda k, d: k == "link" and d.get("state") == "up", 60)


def test_log_has_no_address_email_password_or_texts(live):
    leaked = [URL, httpx.URL(URL).host, EMAIL, PASSWORD, "pong", "red-square", *live.chats, *live.files]
    assert not [line for line in live.records.lines if any(word and word in line for word in leaked)]
