"""Самопроверка моста, хвост журнала для Студии и уведомление о задании, ждущем ответа (мост 0.47.0)."""

import json
from types import SimpleNamespace

import pytest

from studio_agent import app, journal, limits, selfcheck, tray, window
from test_agent import make, req, root  # noqa: F401 — фикстура `root`


@pytest.fixture
async def opened(home, root):
    agent = make(home, root)
    assert (await agent.handle(req("open", pin=None)))[0]["ok"]
    return agent


async def test_self_check_needs_pin_and_answers_codes_without_paths(home, root, monkeypatch):
    agent = make(home, root)
    assert (await agent.handle(req("self_check")))[0]["code"] == "closed"
    assert (await agent.handle(req("open", pin=None)))[0]["ok"]
    monkeypatch.setattr(selfcheck, "git", lambda: {"state": "ok", "version": "2.47.1"})
    monkeypatch.setattr(selfcheck, "login", lambda engine: "no" if engine == "claude" else "unknown")
    agent.cfg.projects_roots = [str(root), str(root / "missing-private-folder")]
    data = (await agent.handle(req("self_check")))[0]["data"]
    items = {item["id"]: item for item in data["items"]}
    assert list(items) == ["git", "claude", "codex", "opencode", "deepseek", "projects", "home", "link", "desktop", "disk"]
    assert items["git"] == {"id": "git", "state": "ok", "version": "2.47.1"}
    assert items["claude"] == {"id": "claude", "state": "warn", "code": "login", "version": "2.3.1", "login": "no"}
    assert items["codex"]["state"] == "fail" and items["codex"]["code"] == "missing"
    assert items["projects"] == {"id": "projects", "state": "warn", "code": "unreadable", "count": 1, "total": 2}
    assert items["home"]["state"] == items["link"]["state"] == "ok"
    assert type(items["disk"]["free"]) is int
    text = json.dumps(data)
    assert home.name not in text and "private" not in text


async def test_each_check_has_its_own_time_limit(opened, home, monkeypatch):
    """Предел — подменой `limited` (без настоящих часов): истёк у одной проверки — `timeout` только у неё."""
    def hung():
        raise AssertionError("зависшая проверка не исполняется")

    async def limited(check, limit):
        limits_seen.append(limit)
        if check is hung:
            raise TimeoutError
        return check()

    limits_seen = []
    monkeypatch.setattr(selfcheck, "git", hung)
    monkeypatch.setattr(selfcheck, "disk", lambda h: 1 / 0)
    data = await selfcheck.run(opened, home, "online", limited=limited)
    items = {item["id"]: item for item in data["items"]}
    assert items["git"] == {"id": "git", "state": "fail", "code": "timeout"}
    assert items["disk"] == {"id": "disk", "state": "fail", "code": "error"}
    assert items["home"]["state"] == "ok"
    assert limits_seen == [limits.SELF_CHECK_SEC] * len(items)


def test_helper_reports_node_and_version_of_the_catalog(monkeypatch):
    monkeypatch.setattr(selfcheck, "login", lambda engine: "yes")
    agent = SimpleNamespace(deepseek=SimpleNamespace(catalog=None, unavailable="node"),
                            codex=SimpleNamespace(catalog={"version": "codex-cli 0.63.0"}))
    assert selfcheck.helper("deepseek", agent) == {"state": "fail", "code": "node", "version": None, "login": "yes"}
    assert selfcheck.helper("codex", agent) == {"state": "ok", "version": "0.63.0", "login": "yes"}


def test_login_signs_come_from_files_and_variables_only(tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck.host, "MACOS", False)
    env = {"USERPROFILE": str(tmp_path), "HOME": str(tmp_path)}
    assert [selfcheck.login(e, env, tmp_path) for e in selfcheck.ENGINES] == ["no", "no", "unknown", "unknown"]
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "x"}}', encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    assert selfcheck.login("claude", env, tmp_path) == selfcheck.login("codex", env, tmp_path) == "yes"
    assert selfcheck.login("deepseek", {**env, "DEEPSEEK_API_KEY": "x"}, tmp_path) == "yes"


async def test_agent_log_returns_masked_tail_of_both_files(opened, home):
    (home / "agent.log.1").write_text("".join(f"old {i}\n" for i in range(300)), encoding="utf-8")
    (home / "agent.log").write_text("new C:\\Users\\someone\\x.txt\nnew /home/someone/x\x1b[0m\nnew \\\\host\\share\n"
                                    + "".join(f"new {i}\n" for i in range(150)), encoding="utf-8")
    lines = (await opened.handle(req("agent_log")))[0]["data"]["lines"]
    assert len(lines) == limits.LOG_TAIL
    assert lines[0] == "old 253" and lines[-1] == "new 149"
    assert lines[47:50] == ["new <path>"] * 3
    assert "someone" not in json.dumps(lines)


def test_agent_log_without_file_is_empty(home):
    assert journal.tail(home) == []


def test_wait_notice_once_per_waiting_after_a_minute():
    clock = SimpleNamespace(t=0.0)
    notice = app.WaitNotice(lambda: clock.t)
    assert not notice.check(1)
    clock.t = limits.WAIT_NOTICE_SEC - 1
    assert not notice.check(2)
    clock.t = limits.WAIT_NOTICE_SEC
    assert notice.check(1)
    clock.t += 600
    assert not notice.check(1)  # то же ожидание — без повтора
    assert not notice.check(0)
    assert not notice.check(1)
    clock.t += limits.WAIT_NOTICE_SEC
    assert notice.check(1)  # новое ожидание


def test_controller_posts_waiting_notice_from_activity(home):
    clock = SimpleNamespace(t=0.0)
    ctrl = app.Controller(home, now=lambda: clock.t)
    waiting = {"chat": True}
    backend = SimpleNamespace(live=lambda: dict(waiting))
    ctrl.runner = backend
    ctrl.agent = SimpleNamespace(desktop=SimpleNamespace(status=lambda: {"active": False}),
                                 software=SimpleNamespace(busy=False), restart_blocking_requests=0, mirror_active=False)
    posted = []
    ctrl.on_waiting = lambda: posted.append(True)
    ctrl._refresh_activity()
    clock.t = limits.WAIT_NOTICE_SEC
    ctrl._refresh_activity()
    ctrl._refresh_activity()
    assert posted == [True]


def test_balloon_click_runs_the_action_once_and_passes_other_messages():
    calls, passed = [], []
    action = [lambda: calls.append("studio")]
    handle = tray.balloon_handler(lambda w, l: passed.append(l), lambda: action.pop() if action else None)
    handle(0, tray.NIN_BALLOONUSERCLICK)
    handle(0, tray.NIN_BALLOONUSERCLICK)
    handle(0, 0x0202)
    assert calls == ["studio"] and passed == [0x0202]


def test_window_rows_name_codes_versions_and_numbers():
    rows = window.check_rows({"items": [
        {"id": "git", "state": "ok", "version": "2.47.1"},
        {"id": "codex", "state": "warn", "code": "login", "version": "0.63.0", "login": "no"},
        {"id": "projects", "state": "warn", "code": "unreadable", "count": 1, "total": 2},
        {"id": "link", "state": "fail", "code": "offline"},
        {"id": "desktop", "state": "off", "code": "disabled"},
        {"id": "disk", "state": "ok", "free": 3 * 1024 ** 3}]})
    assert rows == [("Git", "В порядке · 2.47.1", "ok"), ("Codex", "Нет входа · 0.63.0", "warn"),
                    ("Папки проектов", "Нет доступа · доступно 1 из 2", "warn"),
                    ("Канал со студией", "Нет связи — переподключаюсь", "danger"),
                    ("Удалённый рабочий стол", "Выключен в окне моста", "muted"),
                    ("Свободное место", "В порядке · свободно 3.0 ГБ", "ok")]
    assert window.check_rows(None) == [("Самопроверка", "Не удалось проверить", "danger")]
