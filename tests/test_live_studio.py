"""Сквозная проверка: живая студия (её среда, `tests/live_studio_server.py`) и настоящий Claude Code.

Тратит квоту — четыре коротких хода (ответ, карточка с allow, «Остановить», вопрос AskUserQuestion). Мост — настоящие `Agent`, `Runner` (SDK и CLI) и `Link` в цикле теста;
сторона браузера — `urllib` в потоке: вход как в `tests/conftest.py` студии, cookie сессии — руками в `Cookie`
(`__Host-` и Secure: cookiejar не шлёт её по http), CSRF — заголовком из `GET /api/me`."""

import asyncio
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from studio_agent import cli, enroll, projects, store
from studio_agent.agent import Agent
from studio_agent.link import Link
from studio_agent.pin import PinStore
from studio_agent.turns import Runner

pytestmark = [pytest.mark.manual, pytest.mark.timeout(600)]

STUDIO_REPO = Path(os.environ.get("STUDIO_REPO") or "C:/work/projects/studio")
SERVER = Path(__file__).with_name("live_studio_server.py")
PASSWORD = "password-123"
CSRF_HEADER = "x-studio-csrf"
PROBE = "studio-agent-probe"
ONLINE_SEC = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    """Сторона страницы: сессия студии и запросы `/api/…`; всё блокирующее — звать из потока."""

    def __init__(self, url: str):
        self.url = url
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.headers = {}
        body = urllib.parse.urlencode({"login": "owner", "password": PASSWORD}).encode()
        status, headers, _ = self._open("POST", "/login", body, {"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 303
        cookie = next(v.split(";", 1)[0] for k, v in headers if k.lower() == "set-cookie" and v.startswith("__Host-"))
        self.headers = {"Cookie": cookie}
        self.headers[CSRF_HEADER] = self.call("GET", "/api/me")[1]["csrf"]

    def _open(self, method: str, path: str, body: bytes | None, headers: dict, timeout: float = 60):
        request = urllib.request.Request(self.url + path, data=body, method=method, headers={**self.headers, **headers})
        try:
            with self.opener.open(request, timeout=timeout) as r:
                return r.status, r.getheaders(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, list(e.headers.items()), e.read()

    def call(self, method: str, path: str, data=None) -> tuple[int, object]:
        body = None if data is None else json.dumps(data).encode()
        status, _, raw = self._open(method, path, body, {"Content-Type": "application/json"} if body else {})
        return status, (json.loads(raw) if raw else None)

    def stream(self, path: str, data: dict, on_event=lambda kind, e: None) -> list[tuple[str, dict]]:
        """Поток SSE хода до `done` (или конца потока): события по порядку; `on_event` — на каждом."""
        request = urllib.request.Request(self.url + path, data=json.dumps(data).encode(), method="POST",
                                         headers={**self.headers, "Content-Type": "application/json"})
        out, kind = [], None
        with self.opener.open(request, timeout=300) as r:
            assert r.status == 200
            for raw in r:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event: "):
                    kind = line[len("event: "):]
                elif line.startswith("data: "):
                    e = json.loads(line[len("data: "):])
                    out.append((kind, e))
                    on_event(kind, e)
                    if kind in ("done", "offline", "closed"):
                        break
        return out


@pytest.fixture
def studio(tmp_path):
    """Живая студия отдельным процессом её `.venv`; адрес — из строки `READY <адрес>`."""
    data = tmp_path / "studio"
    data.mkdir()
    proc = subprocess.Popen([str(STUDIO_REPO / ".venv" / "Scripts" / "python.exe"), str(SERVER), str(data)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                            env={**os.environ, "STUDIO_REPO": str(STUDIO_REPO), "PYTHONIOENCODING": "utf-8"})
    try:
        for line in proc.stdout:
            if line.startswith("READY "):
                break
        else:
            pytest.fail(f"студия не поднялась: код {proc.wait()}")
        yield line.split(" ", 1)[1].strip()
    finally:
        proc.stdin.close()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def probe(tmp_path):
    """Папка проектов с проектом `probe`: `a.md`, `git init`, карточка на `echo studio-agent-probe`."""
    root = tmp_path / "projects"
    project = root / "probe"
    (project / ".claude").mkdir(parents=True)
    (project / "a.md").write_text("# Проба\n", encoding="utf-8", newline="\n")
    (project / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "default", "ask": [f"Bash(echo {PROBE}*)"]}}), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return root


def _forget_session(chat: str) -> None:
    """Файл сессии пробы — прочь из `~/.claude/projects` (папка — если опустела)."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for f in (config / "projects").glob(f"*/{chat}.jsonl"):
        f.unlink(missing_ok=True)
        side = f.with_suffix("")
        if side.is_dir():
            shutil.rmtree(side, ignore_errors=True)
        if not any(f.parent.iterdir()):
            f.parent.rmdir()


async def _until(fn, sec: float = ONLINE_SEC):
    """Ждать, пока `fn()` (в потоке) не вернёт истину."""
    for _ in range(int(sec * 10)):
        value = await asyncio.to_thread(fn)
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("не дождались")


async def test_bridge_works_with_the_live_studio_and_claude_code(studio, probe, home, monkeypatch):
    clean = dict(os.environ)
    cli.drop_session_env(clean)                                 # как app.prepare_env: тест идёт и из Claude Code
    for name in set(os.environ) - set(clean):
        monkeypatch.delenv(name)
    url = studio
    agent_id, key = await asyncio.to_thread(enroll.enroll, url, "owner", PASSWORD, "Проба")
    cli_path = await asyncio.to_thread(cli.find_cli)
    version = await asyncio.to_thread(cli.cli_version, cli_path)
    pins = PinStore(home)
    agent = None
    runner = Runner(lambda f: agent.emit(f), choices=store.Choices(home), cli_path=cli_path)
    agent = Agent(store.Config(url=url, projects_root=str(probe)), pins, runner, cc_version=version, models=[],
                  efforts=[])
    online = asyncio.Event()

    def start_link():
        online.clear()
        lk = Link(url, key, agent, lambda s: online.set() if s == "online" else None)
        return lk, asyncio.create_task(lk.run())

    link, task = start_link()
    chat = None
    try:
        await asyncio.wait_for(online.wait(), ONLINE_SEC)
        b = await asyncio.to_thread(Browser, url)
        base = f"/api/code/agents/{agent_id}"

        def agent_row():
            status, data = b.call("GET", "/api/agents")
            assert status == 200
            return next((a for a in data["items"] if a["id"] == agent_id and a["online"]), None)

        row = await _until(agent_row)
        assert row["name"] == "Проба" and row["has_pin"] is False and row["cc_version"] == version

        assert (await asyncio.to_thread(b.call, "POST", f"{base}/open", {"pin": None}))[0] == 204
        status, data = await asyncio.to_thread(b.call, "GET", f"{base}/projects")
        assert status == 200 and data["items"] == [{"pid": projects.pid("probe"), "name": "probe"}]
        pid = data["items"][0]["pid"]
        proj = f"{base}/projects/{pid}"

        status, data = await asyncio.to_thread(b.call, "POST", f"{proj}/chats")
        assert status == 201
        chat = data["chat"]

        # ход 1: ответ одним словом
        events = await asyncio.to_thread(b.stream, f"{proj}/chats/{chat}/turn", {"text": "Ответь одним словом: готово"})
        kinds = [k for k, _ in events]
        assert kinds[0] == "turn" and kinds[-1] == "done", kinds
        assert events[-1][1] == {"status": "done"}, events[-1]
        assert "готово" in "".join(e["text"] for k, e in events if k == "text").lower()

        # ход 2: карточка разрешения на Bash, ответ allow, итог шага
        asks = []

        def on_event(kind, e):
            if kind == "ask":
                asks.append(e)
                status, _ = b.call("POST", f"{proj}/chats/{chat}/answer",
                                   {"request_id": e["request_id"], "decision": "allow"})
                assert status == 204

        events = await asyncio.to_thread(b.stream, f"{proj}/chats/{chat}/turn",
                                         {"text": f"Выполни в Bash: echo {PROBE}"}, on_event)
        kinds = [k for k, _ in events]
        assert kinds[-1] == "done" and events[-1][1] == {"status": "done"}, events[-1]
        assert [(a["kind"], a["tool"]) for a in asks] == [("permission", "Bash")], kinds
        assert PROBE in asks[0]["input"]["command"]
        assert "asked" in kinds
        assert any(k == "result" and PROBE in e["text"] and not e["error"] for k, e in events), kinds

        status, data = await asyncio.to_thread(b.call, "GET", f"{proj}/chats")
        assert status == 200
        mine = [c for c in data["items"] if c["chat"] == chat]
        assert len(mine) == 1 and mine[0]["origin"] == "studio" and mine[0]["running"] is False

        status, data = await asyncio.to_thread(b.call, "GET", f"{proj}/chats/{chat}")
        assert status == 200
        assert [m["role"] for m in data["items"]] == ["user", "assistant", "user", "assistant"]
        assert [s["tool"] for s in data["items"][3]["steps"]] == ["Bash"]
        assert PROBE in data["items"][3]["steps"][0]["text"]

        # файлы и git
        status, data = await asyncio.to_thread(b.call, "POST", f"{proj}/files", {"path": ""})
        assert status == 200 and "a.md" in [i["name"] for i in data["items"] if not i["dir"]]
        status, data = await asyncio.to_thread(b.call, "POST", f"{proj}/read", {"path": "a.md"})
        assert status == 200 and data["text"] == "# Проба\n"
        status, data = await asyncio.to_thread(b.call, "GET", f"{proj}/git")
        assert status == 200 and {"path": "a.md", "status": "??"} in data["files"]

        # ход 3: карточка разрешения, «Остановить» — настоящий interrupt() CLI, done stopped
        stops = []

        def on_stop(kind, e):
            if kind == "ask" and not stops:
                stops.append(e)
                status, _ = b.call("POST", f"{proj}/chats/{chat}/stop")
                assert status == 204

        events = await asyncio.to_thread(b.stream, f"{proj}/chats/{chat}/turn",
                                         {"text": f"Выполни в Bash: echo {PROBE}-stop"}, on_stop)
        kinds = [k for k, _ in events]
        assert [(a["kind"], a["tool"]) for a in stops] == [("permission", "Bash")], kinds
        assert kinds[-1] == "done" and events[-1][1] == {"status": "stopped"}, events[-1]
        assert "asked" in kinds and not any(k == "result" and f"{PROBE}-stop" in e["text"] and not e["error"]
                                            for k, e in events), kinds

        # ход 4 того же чата (не running после остановки): вопрос AskUserQuestion → answers → ответ модели
        questions = []

        def on_question(kind, e):
            if kind == "ask":
                questions.append(e)
                answers = {}
                for q in e.get("questions") or []:
                    labels = [o["label"] for o in q["options"]]
                    answers[q["question"]] = next((x for x in labels if "син" in x.lower()), "синий")
                status, _ = b.call("POST", f"{proj}/chats/{chat}/answer",
                                   {"request_id": e["request_id"], "decision": "allow", "answers": answers})
                assert status == 204

        events = await asyncio.to_thread(
            b.stream, f"{proj}/chats/{chat}/turn",
            {"text": "Задай мне один вопрос инструментом AskUserQuestion с двумя вариантами: красный, синий; "
                     "затем ответь одним словом, что я выбрал"}, on_question)
        kinds = [k for k, _ in events]
        assert kinds[0] == "turn" and kinds[-1] == "done" and events[-1][1] == {"status": "done"}, events[-1]
        assert [q["kind"] for q in questions] == ["question"], kinds
        assert len(questions[0]["questions"]) == 1 and len(questions[0]["questions"][0]["options"]) == 2
        assert "син" in "".join(e["text"] for k, e in events if k == "text").lower(), kinds

        # PIN: задан на машине, канал заново — открытость сброшена, нужен PIN
        await asyncio.to_thread(pins.set_pin, "2468")
        await agent.refresh_pin()                                # как ядро перед новым каналом
        await link.stop()
        await task
        link, task = start_link()
        await asyncio.wait_for(online.wait(), ONLINE_SEC)
        await _until(lambda: (r := agent_row()) is not None and r["has_pin"])
        status, data = await asyncio.to_thread(b.call, "POST", f"{base}/open", {"pin": None})
        assert status == 409 and data["detail"]["code"] == "pin_needed"
        assert (await asyncio.to_thread(b.call, "POST", f"{base}/open", {"pin": "2468"}))[0] == 204
        assert (await asyncio.to_thread(b.call, "GET", f"{base}/projects"))[0] == 200
    finally:
        await runner.close()
        await link.stop()
        await asyncio.gather(task, return_exceptions=True)
        if chat is not None:
            _forget_session(chat)
