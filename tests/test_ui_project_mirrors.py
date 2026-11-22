"""Окно «Зеркало» через настоящий интерфейс: добавление копии с PIN второго компьютера, сравнение и разница, группа
копий и «Отправить во все копии», поиск копий и присоединение к группе."""
from pathlib import Path
import shutil

import pytest

import mirror_groups
import security
import state
from agent.studio_agent import mirror
from fake_bridge import pid
from test_project_mirrors import computers, trio
from test_ui_browser import READY, browser, code_online, go, live, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

PANEL = "#code-project-mirrors"
COPIES = "#code-mirror-copies"
COUNT = "#code-project-mirror .code-mirror-count"
#: окно ничем не занято: работа окна закончена (сравнения, передачи, перечитывание списка)
IDLE = f"document.querySelector('{PANEL} .project-mirror-group')?.inert === false"


def group(owner_of, *copies):
    """Группа копий учётки компьютера `owner_of`: `copies` — (компьютер, pid, корень `list_roots`, имя)."""
    uid = state.DB.execute("SELECT user_id FROM agents WHERE id=?", (owner_of,)).fetchone()[0]
    with mirror_groups.transaction():
        grp = mirror_groups.create_group(uid)
        return [mirror_groups.add(grp, agent, project, root["id"], name, root["path"])
                for agent, project, root, name in copies]


def test_mirror_dialog_selects_destination_and_shows_diff(browser, live, computers):
    login = "project-mirror"
    live.add_user(login)
    source, first, second = computers
    aid, left = code_online(live, login, first, "Source")
    target, right = code_online(live, login, second, "Destination")
    destination = Path(second.roots[1]) / source.name
    name = f"code/{aid}/{pid(source.name)}"
    form = f"{PANEL} .project-mirror-form"
    state_box = f"{PANEL} .project-mirror-state"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(name),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("eval", "order", "document.querySelector('#code-project-mirror').previousElementSibling.textContent"
                              " + '/' + document.querySelector('#code-project-mirror').nextElementSibling.textContent"),
            ("eval", "before", f"[document.querySelector('{COUNT}').hidden,"
                               " document.querySelector('#code-project-mirror').hasAttribute('aria-label')]"),
            ("click", "#code-project-mirror"),
            # Без группы окно сразу в режиме добавления: форма и поиск, без списка копий и «Отправить во все».
            ("wait", f"document.querySelector('{form} select')?.options.length === 2"),
            ("eval", "adding", "[document.querySelector('#code-mirror-add-panel').hidden,"
                               " !!document.querySelector('#code-mirror-send-all').closest('[hidden]'),"
                               f" !!document.querySelector('{COPIES}').closest('[hidden]')]"),
            ("eval", "computer", f"""(() => {{
                const select = document.querySelector('{form} select');
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
            ("wait", f"document.querySelectorAll('{form} select')[1]?.options.length === 3"),
            ("eval", "root", f"""(() => {{
                const select = document.querySelectorAll('{form} select')[1];
                const requested = !select.value;
                select.selectedIndex = 2;
                select.dispatchEvent(new Event('change'));
                return requested;
            }})()"""),
            ("click", f"{form} button"),
            ("wait", f"document.querySelector('{COPIES} li[data-copy]')"
                     f" && document.querySelector('{state_box}')?.textContent.includes('Скопировать весь проект')"),
            ("eval", "added", "document.querySelector('#code-mirror-add-panel').hidden"),
            ("click", f"{state_box} summary"),
            ("wait", f"document.querySelector('{PANEL} .project-mirror-diff')?.textContent.includes('local.env')"),
            ("click", f"{state_box} .actions button:last-child"),
            ("wait", f"document.querySelector('{state_box}')?.textContent.includes('файлы совпадают')"),
            ("eval", "row", f"document.querySelector('{COPIES} li').textContent"),
            ("click", f"{PANEL} .project-mirror-exclude summary"),
            ("type", "#code-mirror-exclude", "empty"),
            ("click", f"{PANEL} .project-mirror-exclude button"),
            ("wait", f"document.querySelector('{PANEL} .project-mirror-exclude summary')?.textContent === 'Постоянные исключения (1)'"
                     f" && document.querySelector('{state_box}').textContent.includes('Пропущено исключений, ссылок,"
                     " специальных объектов, баз SQLite, недопустимых имён и связанных папок: 1')"),
            ("eval", "fits", f"document.querySelector('{PANEL}').scrollWidth <= document.querySelector('{PANEL}').clientWidth + 1"),
            ("click", f"{PANEL} .dialog-close"),
            ("wait", f"document.querySelector('{COUNT}')?.textContent === '1' && !document.querySelector('{COUNT}').hidden"),
            ("eval", "label", "document.querySelector('#code-project-mirror').getAttribute('aria-label')")],
            width=320, touch=True)
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["order"] == "Файлы/Доступ"
    assert values["before"] == [True, False]
    assert values["adding"] == [False, True, True]
    assert values["pin"] == ["text", False, "disc", "off"]
    assert values["root"] and values["added"] and values["fits"]
    assert values["row"].startswith("Destination · ") and source.name in values["row"]
    assert "синхронизирована с этим компьютером" in values["row"]
    assert values["label"] == "Зеркало · копий: 1"
    assert (destination / "local.env").read_bytes() == (source / "local.env").read_bytes()


def test_mirror_group_is_seen_from_the_copy_and_sends_to_all(browser, live, trio):
    login = "mirror-group"
    live.add_user(login)
    source, first, second, third = trio
    second.pin = None
    roots = [mirror.projects.list_roots(machine.roots)[0] for machine in (first, second, third)]
    clone_b, clone_c = (Path(root["path"]) / source.name for root in roots[1:])
    for clone in (clone_b, clone_c):
        shutil.copytree(source, clone)
    second.projects[source.name] = {}  # копия B — проект компьютера B, его страница
    machines = [code_online(live, login, machine, title)
                for machine, title in ((first, "Source"), (second, "Laptop"), (third, "Third"))]
    a, b, c = (aid for aid, _ in machines)
    ca, _, cc = group(a, *((aid, pid(source.name), root, source.name) for aid, root in zip((a, b, c), roots)))
    send_all = [("eval", "accept", "void (window.mirrorConfirmations = [],"
                                   " window.confirm = (text) => { window.mirrorConfirmations.push(text); return true; })"),
                ("click", "#code-project-mirror"),
                ("wait", f"document.querySelectorAll('{COPIES} li[data-copy]').length === 2 && {IDLE}"),
                ("click", "#code-mirror-send-all")]

    def notes(*expected):
        """Обе строки копий с пометками и окно свободно: пометки строк и подтверждения."""
        rows = " && ".join(f"document.querySelector(\"{COPIES} li[data-copy='{cid}']\")?.textContent.includes('{text}')"
                           for cid, text in expected)
        return [("wait", f"{rows} && {IDLE}"),
                ("eval", "rows", f"Object.fromEntries([...document.querySelectorAll('{COPIES} li')]"
                                 ".map((li) => [li.dataset.copy, li.textContent]))"),
                ("eval", "confirmations", "window.mirrorConfirmations")]

    page = live.url + "/"
    try:
        # Совпадающие копии: сравнения запоминают общее состояние пар, подтверждения нет.
        same = browser.drive(page, [*signin(login), ("wait", READY), *go(f"code/{b}/{pid(source.name)}"),
            ("wait", f"document.querySelector('{COUNT}')?.textContent === '2'"),
            ("eval", "label", "document.querySelector('#code-project-mirror').getAttribute('aria-label')"),
            *send_all, *notes((ca, "совпадает"), (cc, "совпадает")),
            ("eval", "adding", "document.querySelector('#code-mirror-add-panel').hidden")], width=1280)
        assert not same.get("failed"), same
        assert same["values"]["label"] == "Зеркало · копий: 2" and same["values"]["adding"] is True
        assert same["values"]["confirmations"] == []
        rows = same["values"]["rows"]
        assert rows[str(ca)].startswith("Source · ") and rows[str(cc)].startswith("Third · ")

        (clone_b / "notes.txt").write_bytes(b"edited on B\n")
        sent = browser.drive(page, [("wait", READY), *go(f"code/{b}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            *send_all, *notes((ca, "отправлено"), (cc, "отправлено"))], width=1280)
        assert not sent.get("failed"), sent
        confirmation, = sent["values"]["confirmations"]
        assert confirmation.startswith("Отправить изменения в копии?\n")
        assert "Source — изменений: 1, удалений: 0" in confirmation and "Third — изменений: 1" in confirmation
        assert (source / "notes.txt").read_bytes() == (clone_c / "notes.txt").read_bytes() == b"edited on B\n"

        # Своя правка в C: C в отправку не входит, A получает новую правку B.
        (clone_c / "notes.txt").write_bytes(b"edited on C\n")
        (clone_b / "notes.txt").write_bytes(b"edited on B again\n")
        skipped = browser.drive(page, [("wait", READY), *go(f"code/{b}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            *send_all, *notes((ca, "отправлено"), (cc, "есть изменения, которых нет здесь — откройте сравнение"))],
            width=1280)
    finally:
        for _, link in machines:
            link.stop()
    assert not skipped.get("failed"), skipped
    confirmation, = skipped["values"]["confirmations"]
    assert "Source — изменений: 1" in confirmation and "Third" not in confirmation
    assert (source / "notes.txt").read_bytes() == b"edited on B again\n"
    assert (clone_c / "notes.txt").read_bytes() == b"edited on C\n"


def test_mirror_dialog_refreshes_the_list_when_a_copy_is_gone_in_english(browser, live, trio):
    """Копию отвязали в другой вкладке: «Compare» получает 404 с признаком `gone` и перечитывает список — и в
    английском интерфейсе, где текст отказа переведён."""
    login = "gone-mirror"
    live.add_user(login)
    source, first, second, third = trio
    second.pin = None
    roots = [mirror.projects.list_roots(machine.roots)[0] for machine in (first, second, third)]
    machines = [code_online(live, login, machine, title)
                for machine, title in ((first, "Source"), (second, "Laptop"), (third, "Third"))]
    a, b, c = (aid for aid, _ in machines)
    _, cb, cc = group(a, *((aid, pid(source.name), root, source.name) for aid, root in zip((a, b, c), roots)))
    url = f"/api/code/agents/{a}/projects/{pid(source.name)}/mirrors/{cc}"
    unlink = (f"fetch('/api/me').then(r => r.json()).then(m => fetch('{url}', {{ method: 'DELETE',"
              " headers: { 'x-studio-csrf': m.csrf } })).then(r => r.status)")
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"code/{a}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelectorAll('{COPIES} li[data-copy]').length === 2 && {IDLE}"),
            ("eval", "unlinked", unlink),
            ("click", f"{COPIES} li[data-copy='{cc}'] button"),
            ("wait", f"document.querySelectorAll('{COPIES} li[data-copy]').length === 1 && {IDLE}"
                     f" && document.querySelector('{PANEL} .status').textContent === 'The list of copies was refreshed.'"),
            ("eval", "rows", f"[...document.querySelectorAll('{COPIES} li[data-copy]')].map((li) => li.dataset.copy)")],
            init="localStorage.setItem('studio-language', 'en')", width=1280)
    finally:
        for _, link in machines:
            link.stop()
    assert not result.get("failed"), result
    assert result["values"]["unlinked"] == 204 and result["values"]["rows"] == [str(cb)]


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
    state_box = f"{PANEL} .project-mirror-state"
    selection = f"{PANEL} input[aria-label='Оставить в копии «notes.txt»']"
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
            ("wait", f"document.querySelector('{state_box}')?.textContent.includes('Отправить изменения')"),
            ("eval", "comparison", f"document.querySelector('{state_box}').textContent"),
            ("click", f"{state_box} summary"),
            ("click", selection),
            ("wait", f"document.querySelector('{PANEL} .project-mirror-diff')?.textContent.includes('Останется в копии')"),
            ("eval", "accept", "void (window.confirm = (text) => { window.mirrorConfirmation = text; return true; })"),
            ("click", f"{state_box} .actions button:last-child"),
            ("wait", f"document.querySelector('{state_box}')?.textContent.includes('Последняя синхронизация')"),
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


def test_mirror_dialog_unlinks_a_copy_sealed_with_a_former_key(browser, live, computers):
    """Адрес копии не расшифровать (сменился ключ Студии): окно открывается, объясняет и отвязывает копию."""
    from cryptography.fernet import Fernet
    login = "sealed-mirror"
    live.add_user(login)
    source, first, second = computers
    aid, left = code_online(live, login, first, "Source")
    target, right = code_online(live, login, second, "Destination")
    root_a, root_b = mirror.projects.list_roots(first.roots)[0], mirror.projects.list_roots(second.roots)[1]
    _, copy = group(aid, (aid, pid(source.name), root_a, source.name), (target, None, root_b, source.name))
    state.DB.execute("UPDATE mirror_copies SET location = ? WHERE id = ?",
                     (Fernet(Fernet.generate_key()).encrypt(b"{}"), copy))
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"code/{aid}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelector('{COPIES} li[data-copy] button')"),
            ("eval", "row", f"document.querySelector('{COPIES} li').textContent"),
            ("click", f"{COPIES} li[data-copy] button"),
            ("wait", f"document.querySelector('{PANEL} .project-mirror-state')?.textContent.includes('после смены ключа')"),
            ("click", f"{PANEL} .project-mirror-state .actions button"),
            ("wait", f"document.querySelector('{PANEL} .status')?.textContent.includes('Копия отвязана')")])
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert result["values"]["row"] == "Destinationнастройка недоступнаСравнить"
    assert state.DB.execute("SELECT 1 FROM mirror_copies WHERE agent = ?", (target,)).fetchone() is None


def test_mirror_dialog_rejoins_the_group_of_the_project_under_its_former_name(browser, live, computers, monkeypatch):
    """Проект переименован: его прежняя копия держит группу с папкой на втором компьютере. Поиск показывает папку
    «В группе копий с Source», «Присоединить» после подтверждения заменяет прежнюю копию этим проектом."""
    import json
    login = "relink-mirror"
    live.add_user(login)
    source, first, second = computers
    monkeypatch.setattr(mirror, "_repositories", lambda _: {"4" * 64})
    clone = Path(second.roots[1]) / "former"
    clone.mkdir()
    aid, left = code_online(live, login, first, "Source")
    target, right = code_online(live, login, second, "Destination")
    root = mirror.projects.list_roots(second.roots)[1]
    state.DB.execute(
        "INSERT INTO project_mirrors(source, project, destination, root, name_key, location) VALUES(?,?,?,?,?,?)",
        (aid, pid("former"), target, root["id"], mirror_groups.name_key("former"),
         security.encrypt(json.dumps({"folder": root["path"], "name": "former"}))))
    assert mirror_groups.adopt() == 1
    found = "#code-mirror-discovery"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"code/{aid}/{pid(source.name)}"),
            ("wait", "document.querySelector('#code-project-mirror')?.hidden === false"),
            ("click", "#code-project-mirror"),
            ("wait", f"document.querySelector('{found} button')?.textContent === 'Проверить копии'"),
            ("click", f"{found} button"),
            ("type", "#code-mirror-pin input", "2468"),
            ("click", "#code-mirror-pin button"),
            ("wait", f"document.querySelector('{found} button')?.textContent === 'Присоединить'"),
            ("eval", "found", f"document.querySelector('{found} li').textContent"),
            ("eval", "accept", "void (window.mirrorConfirmations = [],"
                               " window.confirm = (text) => { window.mirrorConfirmations.push(text); return true; })"),
            ("click", f"{found} button"),
            ("wait", f"document.querySelector('{PANEL} .project-mirror-state')?.textContent.includes('Отправить изменения')"),
            ("eval", "rows", f"document.querySelectorAll('{COPIES} li[data-copy]').length"),
            ("eval", "confirmations", "window.mirrorConfirmations")])
    finally:
        left.stop()
        right.stop()
    assert not result.get("failed"), result
    assert "В группе копий с Source" in result["values"]["found"]
    confirmation, = result["values"]["confirmations"]
    assert "Заменить её этим проектом?" in confirmation
    assert result["values"]["rows"] == 1
    mine = mirror_groups.find(aid, pid(source.name))
    assert mine is not None and mirror_groups.find(aid, pid("former")) is None
    assert [row["agent"] for row in mirror_groups.members(mine["grp"])] == [target, aid]
