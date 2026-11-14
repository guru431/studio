"""Полная перезагрузка интерфейса, всех сводок и открытого чата."""
from pathlib import Path

import httpx
import pytest
import release

from fake_bridge import FakeMachine, pid
from test_ui_browser import (CODE_CHAT_A, READY, browser, code_online, code_requests,
                             code_session, go, live, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


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
    refreshed = ("document.querySelector('#code-project-refresh')?.getAttribute('aria-busy') === 'false'"
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
