"""Выбор папки, PIN второго компьютера, копия и разница через настоящий интерфейс."""
from pathlib import Path

import pytest

from fake_bridge import pid
from test_project_mirrors import computers
from test_ui_browser import READY, browser, code_online, go, live, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_mirror_dialog_selects_destination_and_shows_diff(browser, live, computers):
    login = "project-mirror"
    live.add_user(login)
    source, first, second = computers
    aid, left = code_online(live, login, first, "Source")
    target, right = code_online(live, login, second, "Destination")
    destination = Path(second.roots[1]) / source.name
    name = f"code/{aid}/{pid(source.name)}"
    ui = "#code-project-mirrors"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(name),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("eval", "order", "document.querySelector('#code-project-mirror').previousElementSibling.textContent"
                              " + '/' + document.querySelector('#code-project-mirror').nextElementSibling.textContent"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelector('{ui} .project-mirror-form select')?.options.length === 2"),
            ("eval", "computer", f"""(() => {{
                const select = document.querySelector('{ui} .project-mirror-form select');
                select.value = '{target}';
                select.dispatchEvent(new Event('change'));
                return select.value;
            }})()"""),
            ("wait", f"document.querySelector('{ui} input[type=password]')"),
            ("type", f"{ui} input[type=password]", "2468"),
            ("click", f"{ui} form:has(input[type=password]) button"),
            ("wait", f"document.querySelectorAll('{ui} .project-mirror-form select')[1]?.options.length === 3"),
            ("eval", "root", f"""(() => {{
                const select = document.querySelectorAll('{ui} .project-mirror-form select')[1];
                const requested = !select.value;
                select.selectedIndex = 2;
                select.dispatchEvent(new Event('change'));
                return requested;
            }})()"""),
            ("click", f"{ui} .project-mirror-form button"),
            ("wait", f"document.querySelector('{ui} .project-mirror-state')?.textContent.includes('Скопировать весь проект')"),
            ("click", f"{ui} summary"),
            ("wait", f"document.querySelector('{ui} .project-mirror-diff')?.textContent.includes('local.env')"),
            ("click", f"{ui} .project-mirror-state .actions button:last-child"),
            ("wait", f"document.querySelector('{ui} .project-mirror-state')?.textContent.includes('файлы совпадают')"),
            ("eval", "fits", f"document.querySelector('{ui}').scrollWidth <= document.querySelector('{ui}').clientWidth + 1")],
            width=360, touch=True)
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert result["values"]["order"] == "Файлы/Доступ"
    assert result["values"]["root"] and result["values"]["fits"]
    assert (destination / "local.env").read_bytes() == (source / "local.env").read_bytes()
