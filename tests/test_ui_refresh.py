"""Полная перезагрузка интерфейса, всех сводок и открытого чата."""
from pathlib import Path

import httpx
import pytest
import release

from fake_bridge import FakeMachine, Refused, pid
from test_codex import CATALOG
from test_ui_desktop import desktop_browser
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, CODE_CHAT_C, READY, browser, code_online, code_requests,
                             code_session, go, live, problems, signin, wait_for)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize("width,section,refuse", [(320, "code", False), (1280, "settings/appearance", True)])
def test_refresh_claims_active_sessions_across_projects_and_computers(desktop_browser, live, width, section, refuse):
    login = f"refresh-claim-{desktop_browser.browser_type.name}-{width}"
    live.add_user(login)
    first = FakeMachine({"alpha": {"chats": {CODE_CHAT_A: code_session("Claude")}},
                         "beta": {"chats": {CODE_CHAT_B: {**code_session("Codex"), "engine": "codex"}}}})
    second = FakeMachine({"gamma": {"chats": {CODE_CHAT_C: {**code_session("OpenCode"), "engine": "opencode"},
                                               CODE_CHAT_B: code_session("Idle")}}})
    locked = FakeMachine({"private": {"chats": {CODE_CHAT_A: code_session("PIN")}}}, pin="4829")
    locked.running[CODE_CHAT_A] = {"turn": "locked", "project": pid("private"), "owner": "a" * 32}
    first.codex = CATALOG
    second.opencode = {"version": "1", "models": [], "efforts": []}
    refused = False
    original_claim = first._op_claim

    def claim(req, events):
        nonlocal refused
        if refuse and not refused and req["chat"] == CODE_CHAT_B:
            refused = True
            raise Refused("busy", "Перенос временно недоступен")
        return original_claim(req, events)

    first._op_claim = claim
    channels = []
    try:
        aids = []
        for machine in (first, second, locked):
            machine.project_activity = True
            aid, channel = code_online(live, login, machine)
            aids.append(aid)
            channels.append(channel)
        runs = [(first, CODE_CHAT_A, "alpha", "claude"), (first, CODE_CHAT_B, "beta", "codex"),
                (second, CODE_CHAT_C, "gamma", "opencode")]
        for machine, chat, project, engine in runs:
            machine.running[chat] = {"turn": "original", "project": pid(project), "text": "В работе",
                "steps": [], "asks": {}, "owner": "a" * 32, "followups": [], "engine": engine}
        signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
        cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
        with desktop_browser.new_context(viewport={"width": width, "height": 850}, has_touch=width == 320,
                extra_http_headers={"Cookie": cookie}, service_workers="block") as context:
            if desktop_browser.browser_type.name == "firefox":
                context.add_cookies([{"name": key, "value": value, "url": live.url, "secure": True,
                    "httpOnly": True, "sameSite": "Lax"} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(15000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            address = f"#/code/{aids[0]}/{pid('alpha')}/{CODE_CHAT_A}"
            page.goto(live.url + "/" + address)
            page.wait_for_function(f"() => {READY}")
            page.locator("#code-take-session").wait_for()
            assert not any(code_requests(machine, "claim") for machine in (first, second))
            if section != "code":
                page.evaluate("hash => { location.hash = hash; }", f"#/{section}")
                page.locator(".settings-panel").wait_for()
                address = f"#/{section}"
            page.locator("#refresh-app").click()
            page.wait_for_function(f"() => {READY}")
            # A bridge request records the actual ownership transfer, including sessions never opened here.
            wait_for(lambda: sum(len(code_requests(machine, "claim")) for machine in (first, second)) == 3, 15)
            claims = [req for machine in (first, second) for req in code_requests(machine, "claim")]
            owners = {req["window"] for req in claims}
            assert len(owners) == 1 and owners != {"a" * 32}
            assert {req.get("engine", "claude") for req in claims} == {"claude", "codex", "opencode"}
            assert all(req["fork"] is False for req in claims)
            assert all(machine.running[chat]["turn"] == "original" for machine, chat, _, _ in runs)
            assert all(machine.running[chat]["owner"] in owners for machine, chat, _, _ in runs
                       if not (refuse and chat == CODE_CHAT_B))
            assert page.evaluate("location.hash") == address
            if refuse:
                assert first.running[CODE_CHAT_B]["owner"] == "a" * 32
                page.locator("#refresh-status").wait_for()
                page.locator("#refresh-app").click()
                page.wait_for_function(f"() => {READY}")
                wait_for(lambda: sum(len(code_requests(machine, "claim")) for machine in (first, second)) == 6, 15)
                assert page.locator("#refresh-status").count() == 0
                owners = {machine.running[chat]["owner"] for machine, chat, _, _ in runs}
                assert len(owners) == 1 and owners != {"a" * 32}
            if section == "code":
                page.wait_for_function("() => document.querySelector('#code-session-notice')?.hidden"
                                       " && !document.querySelector('#code-send').disabled")
                page.locator("#code-text").fill("Уточнение после обновления")
                page.locator("#code-send").click()
                wait_for(lambda: bool(code_requests(first, "steer")), 15)
                assert code_requests(first, "steer")[-1]["window"] in owners
            page.reload()
            page.wait_for_function(f"() => {READY}")
            assert sum(len(code_requests(machine, "claim")) for machine in (first, second)) == (6 if refuse else 3)
            assert not code_requests(locked, "claim") and not code_requests(locked, "open")
            assert locked.running[CODE_CHAT_A]["owner"] == "a" * 32
            assert not any(req["op"] in ("turn", "stop", "chat_fork")
                           for machine in (first, second) for req in machine.requests)
            assert not errors
    finally:
        for channel in channels:
            channel.stop()


class RefreshMachine(FakeMachine):
    def _op_project_summary(self, req, events):
        return {**super()._op_project_summary(req, events), "findings": 9 if req.get("refresh") else 1}

    def _op_history(self, req, events):
        return {"items": [{"role": "user", "text": f"history revision {len(code_requests(self, 'history'))}"}],
                "start": 0, "total": 1}


@pytest.mark.parametrize("width", [320, 1280])
def test_refresh_reloads_all_projects_and_the_open_chat(browser, live, width, tmp_path):
    login = f"refresh-chat-{width}"
    live.add_user(login)
    machine = RefreshMachine({"alpha": {"chats": {CODE_CHAT_A: code_session("Current chat")}},
                              **{f"project-{n:02}": {} for n in range(25)}})
    machine.project_summary = True
    aid, server = code_online(live, login, machine)
    address = f"#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    refreshed = ("document.querySelector('.code-panel-footer')?.getAttribute('aria-busy') === 'false'"
                 " && [...document.querySelectorAll('.code-project-findings')].every(n => n.textContent.includes('9'))"
                 " && document.querySelectorAll('.code-project-findings').length === 26")
    geometry = """(() => {
      const b = document.querySelector('#refresh-app'), r = b.getBoundingClientRect();
      const s = document.querySelector('#search-open').getBoundingClientRect();
      return {inside: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight,
        adjacent: innerWidth < 760 ? r.right <= s.left && s.left - r.right < 16 : r.bottom <= s.top,
        clickable: b.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2)),
        label: b.getAttribute('aria-label'), overflow: document.documentElement.scrollWidth > innerWidth};
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("click", "#avatar"), ("eval", "version", "document.querySelector('#menu-version').textContent"),
            ("press", "Escape"), *go(address[2:]),
            ("wait", "document.querySelector('#code-feed')?.textContent.includes('history revision 1')"),
            ("eval", "before", geometry),
            ("screenshot", str(tmp_path / f"refresh-{width}.png")),
            ("route", "/api/me", 503, '{"error":"offline"}'), ("click", "#refresh-app"),
            ("wait", "document.querySelector('#start-retry')"), ("unroute", "/api/me"),
            ("click", "#refresh-app"), ("wait", READY),
            ("wait", "document.querySelector('#code-feed')?.textContent.includes('history revision 2')"),
            ("wait", refreshed),
            ("eval", "after", "[location.hash, performance.getEntriesByType('navigation')[0].type,"
                                " history.state.entry > 0, 'studioRefresh' in history.state]"),
            ("eval", "geometry", geometry),
            ("eval", "normalReload", "location.reload(); true"), ("wait", READY),
            ("wait", "document.querySelector('#code-feed')?.textContent.includes('history revision 3')")],
            width=width, mobile=width == 320, touch=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["after"] == [address, "reload", True, False]
    assert result["values"]["version"] == f"Studio {release.VERSION}"
    expected = {"inside": True, "adjacent": True, "clickable": True, "label": "Обновить", "overflow": False}
    assert result["values"]["before"] == result["values"]["geometry"] == expected
    summaries = [q for q in code_requests(machine, "project_summary") if q.get("refresh")]
    assert {q["project"] for q in summaries} == {pid(name) for name in machine.projects}
    assert len(summaries) == len(machine.projects)
    assert any(q.get("refresh") for q in code_requests(machine, "projects"))
    assert any(q.get("refresh") for q in code_requests(machine, "chats"))
    unexpected = [line for line in problems(result) if "status of 503" not in str(line)]
    assert not unexpected, unexpected


@pytest.mark.parametrize("engine", ["webkit", "firefox"])
def test_refresh_supports_keyboard_english_and_blocked_storage(live, engine, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    login = f"refresh-{engine}"
    live.add_user(login)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with pw.sync_playwright() as driver:
        kind = getattr(driver, engine)
        if not Path(kind.executable_path).is_file():
            pytest.skip(f"{engine} unavailable")
        with kind.launch() as instance:
            with instance.new_context(viewport={"width": 320, "height": 740}, has_touch=True,
                                      extra_http_headers={"Cookie": cookie}, service_workers="block") as context:
                context.add_init_script("""localStorage.setItem('studio-language', 'en');
                  const read = Storage.prototype.getItem;
                  Storage.prototype.getItem = function(key) {
                    if (key === 'studio-language') return read.call(this, key);
                    throw new DOMException('Storage blocked', 'SecurityError');
                  };
                  Storage.prototype.setItem = () => {
                    throw new DOMException('Storage blocked', 'SecurityError');
                  };""")
                page = context.new_page()
                page.set_default_timeout(15000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(live.url + "/#/settings/appearance")
                page.wait_for_function(f"() => {READY}")
                button = page.locator("#refresh-app")
                assert button.get_attribute("aria-label") == "Refresh"
                page.screenshot(path=str(tmp_path / f"refresh-{engine}.png"))
                button.focus()
                page.evaluate("window.beforeRefresh = true")
                page.keyboard.press("Enter")
                page.wait_for_function(f"() => {READY} && !window.beforeRefresh")
                assert page.evaluate("location.hash") == "#/settings/appearance"
                assert page.evaluate("performance.getEntriesByType('navigation')[0].type") == "reload"
                assert page.evaluate("!('studioRefresh' in history.state)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors
