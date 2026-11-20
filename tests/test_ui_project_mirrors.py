"""Выбор папки, PIN второго компьютера, копия и разница через настоящий интерфейс."""
from pathlib import Path
import shutil

import pytest

import security
import state
from agent.studio_agent import mirror
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
            ("wait", "document.querySelector('#code-mirror-pin input')"),
            # PIN компьютера — не форма входа: text с точками вне <form>, как в панели «Агентов».
            ("eval", "pin", """(() => { const i = document.querySelector('#code-mirror-pin input');
                return [i.type, !!i.closest('form'), getComputedStyle(i).webkitTextSecurity, i.getAttribute('autocomplete')];
            })()"""),
            ("type", "#code-mirror-pin input", "2468"),
            ("press", "Enter"),
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
            ("click", f"{ui} .project-mirror-exclude summary"),
            ("type", "#code-mirror-exclude", "empty"),
            ("click", f"{ui} .project-mirror-exclude button"),
            ("wait", f"document.querySelector('{ui} .project-mirror-exclude summary')?.textContent === 'Постоянные исключения (1)'"),
            ("eval", "excluded", f"document.querySelector('{ui} .project-mirror-state').textContent"
                                 ".includes('Пропущено исключений, ссылок, специальных объектов, недопустимых имён и связанных папок: 1')"),
            ("eval", "fits", f"document.querySelector('{ui}').scrollWidth <= document.querySelector('{ui}').clientWidth + 1")],
            width=360, touch=True)
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert result["values"]["order"] == "Файлы/Доступ"
    assert result["values"]["pin"] == ["text", False, "disc", "off"]
    assert result["values"]["root"] and result["values"]["fits"] and result["values"]["excluded"]
    assert (destination / "local.env").read_bytes() == (source / "local.env").read_bytes()


def test_mirror_dialog_finds_a_renamed_clone_and_preserves_a_selected_local_file(browser, live, computers):
    if not shutil.which("git"):
        pytest.skip("Git нужен для чтения .git/config")
    login = "existing-mirror"
    live.add_user(login)
    source, first, second = computers
    (source / ".git/config").write_text('[remote "origin"]\nurl = https://git.example.com/team/project.git\n',
                                       encoding="utf-8")
    clone = Path(second.roots[1]) / "renamed"
    second.roots.append(str(clone.parent.with_name("missing")))
    (clone / ".git").mkdir(parents=True)
    (clone / ".git/config").write_text('[remote "origin"]\nurl = git@git.example.com:team/project\n', encoding="utf-8")
    (clone / "notes.txt").write_bytes(b"independent local edit")
    aid, left = code_online(live, login, first, "Source")
    _, right = code_online(live, login, second, "Destination")
    address = f"code/{aid}/{pid(source.name)}"
    found = "#code-mirror-discovery"
    panel = "#code-project-mirrors"
    selection = f"{panel} input[aria-label='Оставить в копии «notes.txt»']"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(address),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelector('{found} button')?.textContent === 'Проверить копии'"),
            ("click", f"{found} button"),
            ("type", "#code-mirror-pin input", "2468"),
            ("click", "#code-mirror-pin button"),
            ("wait", f"document.querySelector('{found} button')?.textContent === 'Привязать'"),
            ("eval", "found", f"document.querySelector('{found} li').textContent"),
            ("eval", "partial", f"document.querySelector('{found}').textContent.includes('Проверена часть папок компьютера')"),
            ("click", f"{found} button"),
            ("wait", f"document.querySelector('{panel} .project-mirror-state')?.textContent.includes('Синхронизировать изменения')"),
            ("eval", "comparison", f"document.querySelector('{panel} .project-mirror-state').textContent"),
            ("click", f"{panel} summary"),
            ("click", selection),
            ("wait", f"document.querySelector('{panel} .project-mirror-diff')?.textContent.includes('Останется в копии')"),
            ("eval", "accept", "void (window.confirm = (text) => { window.mirrorConfirmation = text; return true; })"),
            ("click", f"{panel} .project-mirror-state .actions button:last-child"),
            ("wait", f"document.querySelector('{panel} .project-mirror-state')?.textContent.includes('Последняя синхронизация')"),
            ("eval", "confirmation", "window.mirrorConfirmation")])
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert "renamed" in result["values"]["found"] and "Тот же Git-репозиторий" in result["values"]["found"]
    assert second.roots[1] in result["values"]["found"]
    assert result["values"]["partial"] is True
    assert "Локальных изменений в зеркале" in result["values"]["comparison"]
    assert "Будет изменено файлов и папок" in result["values"]["confirmation"]
    assert (clone / "notes.txt").read_bytes() == b"independent local edit"
    assert (clone / "local.env").read_bytes() == (source / "local.env").read_bytes()


def test_mirror_dialog_unlinks_a_mirror_sealed_with_a_former_key(browser, live, computers):
    """Настройку зеркала не расшифровать (сменился ключ Студии): окно открывается, объясняет и отвязывает."""
    from cryptography.fernet import Fernet
    login = "sealed-mirror"
    live.add_user(login)
    source, first, second = computers
    aid, left = code_online(live, login, first, "Source")
    target, right = code_online(live, login, second, "Destination")
    root = mirror.projects.list_roots(second.roots)[1]["id"]
    state.DB.execute(
        "INSERT INTO project_mirrors(source, project, destination, root, name_key, location) VALUES(?,?,?,?,?,?)",
        (aid, pid(source.name), target, root, security.digest(source.name),
         Fernet(Fernet.generate_key()).encrypt(b"{}")))
    panel = "#code-project-mirrors"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"code/{aid}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelector('{panel} .project-mirror-state')?.textContent.includes('после смены ключа')"),
            ("eval", "option", f"document.querySelector('{panel} select').selectedOptions[0].textContent"),
            ("click", f"{panel} .project-mirror-state .actions button"),
            ("wait", f"document.querySelector('{panel} .status')?.textContent.includes('Зеркало отвязано')")])
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert result["values"]["option"] == "Destination · настройка недоступна"
    assert state.DB.execute("SELECT 1 FROM project_mirrors WHERE destination = ?", (target,)).fetchone() is None


def test_mirror_dialog_relinks_the_copy_of_the_project_under_its_former_name(browser, live, computers, monkeypatch):
    """Проект переименован: его прежнее зеркало держит папку копии. Поиск показывает её «уже зеркалом другого
    проекта», «Перепривязать» после подтверждения снимает прежнюю привязку и назначает папку этому проекту."""
    login = "relink-mirror"
    live.add_user(login)
    source, first, second = computers
    monkeypatch.setattr(mirror, "_repositories", lambda _: {"4" * 64})
    clone = Path(second.roots[1]) / "former"
    clone.mkdir()
    aid, left = code_online(live, login, first, "Source")
    target, right = code_online(live, login, second, "Destination")
    root = mirror.projects.list_roots(second.roots)[1]["id"]
    state.DB.execute(
        "INSERT INTO project_mirrors(source, project, destination, root, name_key, location) VALUES(?,?,?,?,?,?)",
        (aid, pid("former"), target, root, security.digest("former"),
         security.encrypt('{"folder":"former","name":"former"}')))
    found = "#code-mirror-discovery"
    panel = "#code-project-mirrors"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"code/{aid}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelector('{found} button')?.textContent === 'Проверить копии'"),
            ("click", f"{found} button"),
            ("type", "#code-mirror-pin input", "2468"),
            ("click", "#code-mirror-pin button"),
            ("wait", f"document.querySelector('{found} button')?.textContent === 'Перепривязать'"),
            ("eval", "found", f"document.querySelector('{found} li').textContent"),
            ("eval", "accept", "void (window.confirm = (text) => { window.mirrorConfirmation = text; return true; })"),
            ("click", f"{found} button"),
            ("wait", f"document.querySelector('{panel} .project-mirror-state')?.textContent.includes('Синхронизировать изменения')"),
            ("eval", "confirmation", "window.mirrorConfirmation")])
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert "Уже зеркало другого проекта" in result["values"]["found"]
    assert "Перепривязать её к этому проекту?" in result["values"]["confirmation"]
    assert [tuple(r) for r in state.DB.execute("SELECT source, project FROM project_mirrors WHERE destination = ?",
                                               (target,))] == [(aid, pid(source.name))]
