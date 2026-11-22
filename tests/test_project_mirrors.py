"""Маршруты Студии и реальные операции диска моста на двух временных корнях."""
import logging
from pathlib import Path
import shutil

import bridge
import pytest
import state

from agent.studio_agent import files, mirror
from agent.studio_agent.errors import Refused as AgentRefused
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, enroll, pid


class MirrorMachine(FakeMachine):
    def __init__(self, roots, *, source=None, pin=None):
        super().__init__({source.name: {}} if source else {}, pin=pin)
        self.roots = [str(root) for root in roots]
        self.source = source
        self.incoming = mirror.Transfers()
        self.project_create = True
        self.platform = "win32"
        self.discovery = True
        self.exclusions = True
        self.groups = True

    @property
    def hello(self):
        return {**super().hello, "project_mirror": True, "project_mirror_discovery": self.discovery,
                "mirror_exclude": self.exclusions, "mirror_groups": self.groups}

    def handle(self, req):
        try:
            return super().handle(req)
        except AgentRefused as err:
            return {"t": "res", "id": req["id"], "ok": False, "code": err.code, "message": err.message}, []

    def _project_path(self, project):
        """Любой проект корней машины — источник: группа отправляет с любой своей копии."""
        return mirror.projects.find_project(self.roots, project)

    def _op_project_roots(self, req, events):
        return {"items": mirror.projects.list_roots(self.roots)}

    def _op_mirror_identity(self, req, events):
        return mirror.identity(self.roots, self._project_path(req["project"]))

    def _op_mirror_candidates(self, req, events):
        return mirror.candidates(self.roots, req["name"], req["repositories"])

    def _op_mirror_scan(self, req, events):
        return mirror.scan(self._project_path(req["project"]), req.get("exclude") or (), req.get("reasons") is True)

    def _op_mirror_destination(self, req, events):
        return mirror.destination(self.roots, req["root"], req["name"], metadata_only=req.get("metadata_only"),
                                  exclude=req.get("exclude") or (), reasons=req.get("reasons") is True)

    def _op_mirror_read(self, req, events):
        return mirror.read(self._project_path(req["project"]), req["reads"])

    def _op_read(self, req, events):
        return files.read_text(self._project_path(req["project"]), req["path"])

    def _op_mirror_text(self, req, events):
        return files.read_text(mirror.target(self.roots, req["root"], req["name"]), req["path"])

    def _receive(self, req, events):
        return self.incoming.run(req["op"], req, self.roots)

    _op_mirror_begin = _op_mirror_chunk = _op_mirror_apply = _op_mirror_abort = _receive


@pytest.fixture
def computers(tmp_path):
    source = tmp_path / "source" / "project"
    source.mkdir(parents=True)
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (source / ".gitignore").write_bytes(b"local.env\n")
    (source / "local.env").write_bytes(b"PRIVATE=example\n")
    (source / "empty").mkdir()
    (source / "notes.txt").write_bytes(b"original\n")
    destinations = [tmp_path / "first", tmp_path / "second"]
    for root in destinations:
        root.mkdir()
    return source, MirrorMachine([source.parent], source=source), MirrorMachine(destinations, pin="2468")


@pytest.fixture
def trio(computers, tmp_path):
    """Три компьютера: `third` — без PIN, с одной папкой проектов."""
    (tmp_path / "third").mkdir()
    return (*computers, MirrorMachine([tmp_path / "third"]))


@pytest.mark.integration  # Реальные деревья файлов, несколько полных циклов синхронизации.
def test_project_mirror_copies_all_files_detects_conflicts_and_recovers(studio, computers, caplog, monkeypatch):
    source, first, second = computers
    second.discovery = False  # Ручная привязка моста 0.39.0.
    # Дробление больших файлов и упаковка нескольких файлов проверяются без записи мегабайт.
    import project_mirrors
    monkeypatch.setattr(project_mirrors, "CHUNK", 12)
    make_user(studio, "owner")
    aid, _ = enroll(studio, name="Source")
    target, _ = enroll(studio, name="Destination")
    signin(studio, "owner")
    source_link = connect(studio, aid, first)
    connect(studio, target, second)
    client = studio.c
    assert client.post(f"/api/code/agents/{aid}/open", json={"pin": None}).status_code == 204
    url = f"/api/code/agents/{aid}/projects/{pid(source.name)}/mirrors"
    root = mirror.projects.list_roots(second.roots)[1]
    settings = {"destination": target, "root": root["id"], "name": source.name}
    assert client.post(url, json=settings).json()["detail"]["code"] == "closed"
    assert client.post(f"/api/code/agents/{target}/open", json={"pin": "2468"}).status_code == 204
    created = client.post(url, json=settings)
    assert created.status_code == 201, created.text
    assert created.json()["folder"] == root["path"]
    endpoint = f"{url}/{created.json()['id']}"
    destination = Path(root["path"]) / source.name
    assert not destination.exists()
    assert client.post(url, json=settings).status_code == 409

    def compare():
        response = client.post(endpoint + "/compare")
        assert response.status_code == 200, response.text
        return response.json()

    def sync(plan, **options):
        response = client.post(endpoint + "/sync", json={"plan": plan["plan"], **options})
        assert response.status_code == 202, response.text
        async def finish():
            running = project_mirrors.TRANSFERS.get(target)
            if running is not None:
                await running
        client.portal.call(finish)
        job = client.get(endpoint).json()["job"]
        assert job["state"] in ("done", "failed"), job
        return job

    plan = compare()
    assert plan["exists"] is False and plan["conflicts"] == 0
    assert {path for path, *_ in plan["changes"]} >= {".git/HEAD", "empty", "local.env"}
    assert sync(plan)["state"] == "done"
    assert not (Path(second.roots[0]) / source.name).exists()
    assert mirror.scan(destination)["entries"] == mirror.scan(source)["entries"]
    assert compare()["changes"] == []

    (source / "notes.txt").write_bytes(b"new from source\n")
    assert client.post(endpoint + "/diff", json={"path": "notes.txt"}).json() == {
        "before": "original\n", "after": "new from source\n"}
    old_plan = compare()
    (destination / "notes.txt").write_bytes(b"local destination edit\n")
    assert sync(old_plan)["error"]["code"] == "conflict"
    plan = compare()
    assert plan["conflicts"] == 1 and sync(plan)["error"]["code"] == "conflict"
    assert (destination / "notes.txt").read_bytes() == b"local destination edit\n"
    (source / "local.env").write_bytes(b"PRIVATE=changed\n")
    plan = compare()
    local = next(index for index, change in enumerate(plan["changes"]) if change[0] == "notes.txt")
    assert sync(plan, keep=[local])["state"] == "done"
    assert (destination / "notes.txt").read_bytes() == b"local destination edit\n"
    assert (destination / "local.env").read_bytes() == b"PRIVATE=changed\n"
    (source / "local.env").unlink()
    sync_plan = compare()
    assert sync_plan["conflicts"] == 1
    assert sync(sync_plan, overwrite=True)["state"] == "done"
    assert compare()["changes"] == []
    assert not (destination / "local.env").exists()
    assert client.get(url).json()["items"][0]["synced"]
    assert client.delete(endpoint).status_code == 204
    assert (destination / "notes.txt").read_bytes() == b"new from source\n"
    assert not list(destination.parent.glob(".studio-mirror-*"))
    assert "PRIVATE=example" not in caplog.text and "local.env" not in caplog.text
    studio.c.portal.call(bridge.detach, source_link)


def test_skipped_subtree_preserves_files_at_destination():
    from project_mirrors import _plan

    original = ["file", "0" * 64, 3, 0]
    source = {"link": mirror.SKIPPED}
    target = {"link": mirror.DIRECTORY, "link/keep": original}
    updates, remove, _, conflicts, skipped = _plan(source, target, target)
    assert updates == {} and remove == [] and (conflicts, skipped) == (0, 2)


F1, F2, F3 = (["file", c * 64, 3, 0] for c in "123")


def test_plan_accepts_a_copy_entry_the_source_held_before():
    from project_mirrors import _plan
    args = ({"f": F3}, {"f": F2}, {"f": F1})
    assert _plan(*args)[3] == 1
    assert _plan(*args, held={"f": {tuple(F2)}})[3] == 0


def test_plan_flags_a_newer_copy_entry_the_source_never_held():
    from project_mirrors import _plan
    assert _plan({"f": F1}, {"f": F3}, {"f": F1}, held={"f": {tuple(F1), tuple(F2)}})[3] == 1


def test_plan_flags_an_executable_bit_set_in_the_copy_over_content_the_source_held():
    from project_mirrors import _plan
    executable = [*F2[:3], 1]
    held = {"f": {tuple(F2)}}
    assert _plan({"f": F3}, {"f": executable}, {"f": F1}, held=held)[3] == 1
    # Новая запись сохраняет режим копии (у источника Windows режима нет) — бит не теряется.
    assert _plan({"f": [*F3[:3], 1]}, {"f": executable}, {"f": F1}, held=held)[3] == 0


def test_file_removed_in_the_copy_is_checked_only_against_the_pair_state():
    from project_mirrors import _plan
    assert _plan({"f": F2}, {}, {"f": F1}, held={"f": {tuple(F1)}})[3] == 1


def test_local_changes_refusal_carries_a_flag_the_window_reads_instead_of_words():
    from fastapi import HTTPException
    from project_mirrors import Sync, _plan, _selection
    updates, remove, changes, conflicts, _ = _plan({"f": F2}, {"f": F3}, {"f": F1})
    assert conflicts == 1
    with pytest.raises(HTTPException) as error:
        _selection({"changes": changes}, Sync(plan="0" * 64), updates, remove)
    assert error.value.status_code == 409 and error.value.detail["local"] is True


def test_source_state_keeps_the_previous_entry_for_paths_not_applied():
    from project_mirrors import _source_state
    raw = {"a": F2, "b": F2, "c": F3}
    assert _source_state(raw, {"b": F1}, {"b", "c"}) == {"a": F2, "b": F1}


def test_skip_reasons_are_recorded_and_normalized():
    from fastapi import HTTPException
    from project_mirrors import SKIP, _entries
    reasons = {}
    entries = _entries({"entries": {"x.db": ["skip", "sqlite", 0, 0], "x.db-wal": ["skip", "sqlite-journal", 0, 0]}},
                       reasons=reasons)
    assert entries == {"x.db": SKIP, "x.db-wal": SKIP}
    assert reasons == {"x.db": "sqlite", "x.db-wal": "sqlite-journal"}
    # Причина будущего моста — тоже пропуск, без подписи; не строка или длинная — неверный список.
    reasons = {}
    assert _entries({"entries": {"x": ["skip", "other", 0, 0]}}, reasons=reasons) == {"x": SKIP} and reasons == {}
    for bad in (["skip", 1, 0, 0], ["skip", "x" * 33, 0, 0], ["skip", "other", 1, 0]):
        with pytest.raises(HTTPException):
            _entries({"entries": {"x": bad}})


def test_windows_source_preserves_posix_executable_flags():
    from project_mirrors import _plan, _portable

    original = ["file", "0" * 64, 3, 1]
    target = {"run": original}
    source = {"run": ["file", original[1], 3, 0]}
    assert _plan(_portable(source, "linux", "win32"), target, target)[2] == []
    new = ["file", "1" * 64, 4, 0]
    updates, remove, _, conflicts, _ = _plan(_portable({"run": new}, "darwin", "win32"), target, target)
    assert updates == {"run": ["file", new[1], 4, 1]} and not remove and not conflicts


def test_windows_destination_skips_links_and_names_windows_cannot_create():
    """Проект с Linux/macOS на Windows: ссылки (`node_modules/.bin`) и имена `aux.c`, `a:b` — «пропущен», а не
    откат всей синхронизации общим «не удалось записать зеркало»."""
    import project_mirrors
    from project_mirrors import _plan, _portable

    code = ["file", "0" * 64, 3, 1]
    source = {"node_modules": mirror.DIRECTORY, "node_modules/.bin": mirror.DIRECTORY,
              "node_modules/.bin/tool": ["link", "../tool/cli.js", 0, 0], "aux.c": code, "a:b": mirror.DIRECTORY,
              "a:b/inner.txt": code, "trailing.": code, "src": mirror.DIRECTORY, "src/main.c": code}
    updates, remove, changes, conflicts, skipped = _plan(_portable(dict(source), "win32", "linux"), None, None,
                                                         windows=True)
    assert set(updates) == {"node_modules", "node_modules/.bin", "src", "src/main.c"} and not remove
    assert skipped == 5 and {path for path, kind, *_ in changes if kind == "skipped"} == {
        "node_modules/.bin/tool", "aux.c", "a:b", "a:b/inner.txt", "trailing."}
    # Другая ОС назначения создаёт и ссылки, и такие имена.
    assert _plan(_portable(dict(source), "linux", "darwin"), None, None)[4] == 0
    # Запреты сервера — те же, что у моста на Windows (`files._parts`).
    assert project_mirrors.WINDOWS_DEVICE.pattern == files.DEVICE.pattern
    assert project_mirrors.WINDOWS_BAD == files.BAD_CHARS - {"\\"}


def test_legacy_manifest_with_external_git_is_rejected():
    from fastapi import HTTPException
    from project_mirrors import _entries

    with pytest.raises(HTTPException) as error:
        _entries({"entries": {".git": ["file", "0" * 64, 42, 0]}})
    assert error.value.status_code == 409
    assert "отдельный клон" in error.value.detail["message"]


def test_preserving_nested_file_keeps_its_parent_but_applies_other_changes():
    from fastapi import HTTPException
    from project_mirrors import Sync, _plan, _selection

    old = ["file", "0" * 64, 3, 0]
    new = ["file", "1" * 64, 4, 0]
    target = {"tree": mirror.DIRECTORY, "tree/keep": old, "tree/remove": old}
    source = {"tree": new, "other": new}
    updates, remove, changes, _, _ = _plan(source, target, target)
    keep = [index for index, entry in enumerate(changes) if entry[0] == "tree/keep"]
    selected = Sync(plan="0" * 64, keep=keep)
    assert _selection({"changes": changes}, selected, updates, remove) == ({"other": new}, ["tree/remove"])
    with pytest.raises(HTTPException) as error:
        _selection({"changes": changes}, Sync(plan="0" * 64, keep=[len(changes)]), updates, remove)
    assert error.value.status_code == 422 and error.value.detail["code"] == "invalid"

    # На Windows смена регистра выглядит как удаление и создание, но путь остаётся тем же.
    target = {"Settings": old}
    source = {"settings": new, "other": new}
    updates, remove, changes, _, _ = _plan(source, target, target, windows=True)
    keep = [index for index, entry in enumerate(changes) if entry[0] == "Settings"]
    selected = Sync(plan="0" * 64, keep=keep)
    assert _selection({"changes": changes}, selected, updates, remove) == ({"other": new}, [])


def test_first_equal_comparison_establishes_baseline_for_both_directions(studio, computers):
    source, first, second = computers
    root = mirror.projects.list_roots(second.roots)[1]
    clone = Path(root["path"]) / source.name
    shutil.copytree(source, clone)
    make_user(studio, "owner")
    left, _ = enroll(studio, name="Source")
    right, _ = enroll(studio, name="Destination")
    connect(studio, left, first)
    connect(studio, right, second)
    signin(studio, "owner")
    client = studio.c
    assert client.post(f"/api/code/agents/{left}/open", json={"pin": None}).status_code == 204
    assert client.post(f"/api/code/agents/{right}/open", json={"pin": "2468"}).status_code == 204
    url = f"/api/code/agents/{left}/projects/{pid(source.name)}/mirrors"
    created = client.post(url, json={"destination": right, "root": root["id"], "name": source.name, "existing": True})
    assert created.status_code == 201, created.text
    endpoint = f"{url}/{created.json()['id']}"
    assert client.post(endpoint + "/compare").json()["changes"] == []
    assert client.get(endpoint).json()["synced"] is None
    (source / "notes.txt").write_bytes(b"source only")
    assert client.post(endpoint + "/compare").json()["conflicts"] == 0
    (source / "notes.txt").write_bytes(b"original\n")
    (clone / "notes.txt").write_bytes(b"copy only")
    assert client.post(endpoint + "/compare").json()["conflicts"] == 1


def test_permanent_exclusions_are_neither_copied_nor_removed_in_the_copy(studio, computers):
    """Платформенные каталоги: исходный `.venv` не переносится, местный `.venv` копии не предлагается удалить."""
    import project_mirrors
    source, first, second = computers
    (source / ".venv" / "Scripts").mkdir(parents=True)
    (source / ".venv" / "Scripts" / "site.py").write_bytes(b"windows")
    root = mirror.projects.list_roots(second.roots)[1]
    clone = Path(root["path"]) / source.name
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "site.py").write_bytes(b"linux")
    make_user(studio, "owner")
    left, _ = enroll(studio, name="Source")
    right, _ = enroll(studio, name="Destination")
    connect(studio, left, first)
    connect(studio, right, second)
    signin(studio, "owner")
    client = studio.c
    assert client.post(f"/api/code/agents/{left}/open", json={"pin": None}).status_code == 204
    assert client.post(f"/api/code/agents/{right}/open", json={"pin": "2468"}).status_code == 204
    url = f"/api/code/agents/{left}/projects/{pid(source.name)}/mirrors"
    created = client.post(url, json={"destination": right, "root": root["id"], "name": source.name, "existing": True})
    endpoint = f"{url}/{created.json()['id']}"
    plan = client.post(endpoint + "/compare").json()
    assert ".venv/Scripts/site.py" in {path for path, *_ in plan["changes"]} and plan["deletions"] > 0
    for bad in ([".git"], [".git/objects"], ["../x"], [f"d{i}" for i in range(project_mirrors.EXCLUDE_MAX + 1)]):
        assert client.put(url + "/exclude", json={"exclude": bad}).status_code == 422, bad
    for capable in (False, True):  # прежний мост назначения удалил бы исключённое в копии
        second.exclusions = capable
        connect(studio, right, second)
        assert client.post(f"/api/code/agents/{right}/open", json={"pin": "2468"}).status_code == 204
        if not capable:
            refused = client.put(url + "/exclude", json={"exclude": [".venv"]})
            assert refused.json()["detail"]["code"] == "unsupported"
    saved = client.put(url + "/exclude", json={"exclude": [".venv", "/.venv/"]})
    assert saved.status_code == 200 and saved.json()["exclude"] == [".venv"], saved.text
    plan = client.post(endpoint + "/compare").json()
    assert [change[:2] for change in plan["changes"] if change[0].startswith(".venv")] == [[".venv", "skipped"]]
    response = client.post(endpoint + "/sync", json={"plan": plan["plan"], "overwrite": True})
    assert response.status_code == 202, response.text

    async def finish():
        if (running := project_mirrors.TRANSFERS.get(right)) is not None:
            await running
    client.portal.call(finish)
    assert client.get(endpoint).json()["job"]["state"] == "done"
    assert (clone / ".venv" / "bin" / "site.py").read_bytes() == b"linux" and not (clone / ".venv" / "Scripts").exists()
    assert (clone / "notes.txt").read_bytes() == b"original\n"
    assert client.put(url + "/exclude", json={"exclude": []}).json()["exclude"] == []


def test_discovery_respects_pin_and_links_only_a_present_unreserved_copy(studio, computers, monkeypatch):
    source, first, second = computers
    root = mirror.projects.list_roots(second.roots)[1]
    clone = Path(root["path"]) / "renamed"
    clone.mkdir()
    (clone / "notes.txt").write_bytes(b"local edit")
    monkeypatch.setattr(mirror, "_repositories", lambda _: {"2" * 64})
    make_user(studio, "owner")
    make_user(studio, "guest")
    left, _ = enroll(studio, name="Source")
    right, _ = enroll(studio, name="Destination")
    foreign, _ = enroll(studio, login="guest", name="Someone else")
    foreign_machine = MirrorMachine(second.roots)
    connect(studio, left, first)
    connect(studio, right, second)
    connect(studio, foreign, foreign_machine)
    signin(studio, "owner")
    client = studio.c
    url = f"/api/code/agents/{left}/projects/{pid(source.name)}/mirrors"
    assert client.post(f"/api/code/agents/{left}/open", json={"pin": None}).status_code == 204
    assert client.post(url + "/discover").json()["devices"] == [
        {"id": right, "computer": "Destination", "state": "locked", "items": []}]
    assert not foreign_machine.requests
    assert client.post(url + f"/discover?destination={foreign}").status_code == 404
    assert client.post(f"/api/code/agents/{right}/open", json={"pin": "2468"}).status_code == 204

    def discover():
        response = client.post(url + f"/discover?destination={right}")
        assert response.status_code == 200, response.text
        return response.json()["devices"][0]["items"]

    found, = discover()
    assert found == {"root": root["id"], "folder": root["path"], "name": "renamed", "match": "repository"}
    assert client.get(url).json() == {"copy": None, "items": [], "exclude": [], "pending": False}
    assert (clone / "notes.txt").read_bytes() == b"local edit"
    guest_id = state.DB.execute("SELECT user_id FROM agents WHERE id=?", (foreign,)).fetchone()[0]
    project = mirror.projects.project_id(second.roots, clone.parent, clone.name)
    state.DB.execute("INSERT INTO project_members(agent_id, project, user_id, created) VALUES(?,?,?,?)",
                     (right, project, guest_id, 0))
    assert discover() == []
    state.DB.execute("DELETE FROM project_members WHERE agent_id=? AND project=? AND user_id=?", (right, project, guest_id))

    request = {"destination": right, "root": found["root"], "name": found["name"], "existing": True}
    renamed = clone.with_name("moved-away")
    clone.rename(renamed)
    assert client.post(url, json=request).status_code == 404
    renamed.rename(clone)
    adopted = client.post(url, json=request)
    assert adopted.status_code == 201, adopted.text
    # Компьютер, где копия группы уже есть, поиск не опрашивает.
    assert client.post(url + f"/discover?destination={right}").json()["devices"] == []
    assert (clone / "notes.txt").read_bytes() == b"local edit"
    listing = client.get(url).json()["items"]
    assert [item["name"] for item in listing] == ["renamed"]
    assert client.delete(f"{url}/{adopted.json()['id']}").status_code == 204

    second.pin = None
    second.passive_requests = True
    connect(studio, right, second)
    clone.rename(renamed)
    assert [item["name"] for item in discover()] == ["moved-away"]
    assert [(frame["op"], frame.get("passive")) for frame in second.requests[-2:]] == [
        ("open", True), ("mirror_candidates", True)]
    # Открытие поиском копий — строка `open` истории подключений, как у явного входа.
    opened = [e for e in client.get("/api/agents/events").json()["items"] if e["kind"] == "open"]
    assert [e["agent_id"] for e in opened] == [right, left] and opened[0]["device"].startswith("браузер ")


def test_transfer_survives_screen_lock_but_stops_on_logout_and_aborts_the_destination(studio, computers, monkeypatch):
    """Передача начата незапертой сессией: блокировка экрана по таймеру (страница ушла с экрана) её не рвёт;
    выход рвёт, копия остаётся прежней."""
    import project_mirrors
    source, first, second = computers
    monkeypatch.setattr(project_mirrors, "CHUNK", 12)
    make_user(studio, "owner")
    left, _ = enroll(studio, name="Source")
    right, _ = enroll(studio, name="Destination")
    connect(studio, left, first)
    connect(studio, right, second)
    signin(studio, "owner")
    client = studio.c
    assert client.post(f"/api/code/agents/{left}/open", json={"pin": None}).status_code == 204
    assert client.post(f"/api/code/agents/{right}/open", json={"pin": "2468"}).status_code == 204
    root = mirror.projects.list_roots(second.roots)[1]
    url = f"/api/code/agents/{left}/projects/{pid(source.name)}/mirrors"
    mid = client.post(url, json={"destination": right, "root": root["id"], "name": source.name}).json()["id"]
    key = (client.get(url).json()["copy"], mid)  # список заперт на время передачи — ход берём из памяти
    original = first._op_mirror_read
    reads = []

    def lock_on_first_read(req, events):
        reads.append(req)
        if len(reads) == 1:          # студия заперлась по таймеру посреди передачи
            state.DB.execute("UPDATE users SET screen_lock = 1, screen_lock_sections = '[\"code\"]'")
            state.DB.execute("UPDATE sessions SET seen = 0")
        return original(req, events)
    first._op_mirror_read = lock_on_first_read

    def run():
        plan = client.post(f"{url}/{mid}/compare").json()
        assert client.post(f"{url}/{mid}/sync", json={"plan": plan["plan"]}).status_code == 202
        async def finish():
            if (task := project_mirrors.TRANSFERS.get(right)) is not None:
                await task
        client.portal.call(finish)
        return project_mirrors.RESULTS[key]

    job = run()
    assert len(reads) > 1 and job["state"] == "done", job
    assert (Path(root["path"]) / source.name / "notes.txt").read_bytes() == b"original\n"
    assert client.get(url).status_code == 423                         # страница по-прежнему заперта
    state.DB.execute("UPDATE users SET screen_lock = 0")
    (source / "notes.txt").write_bytes(b"changed after the first copy\n")
    reads.clear()

    def logout_on_first_read(req, events):
        reads.append(req)
        if len(reads) == 1:
            state.DB.execute("DELETE FROM sessions")
        return original(req, events)
    first._op_mirror_read = logout_on_first_read
    job = run()
    assert job["state"] == "failed" and job["error"]["code"] == "closed", job
    assert (Path(root["path"]) / source.name / "notes.txt").read_bytes() == b"original\n"


def signed_in(studio, *machines):
    """Учётка `owner`; компьютеры Source, Destination, Third — на связи и открыты. id компьютеров."""
    make_user(studio, "owner")
    ids = [enroll(studio, name=name)[0] for name, _ in zip(("Source", "Destination", "Third"), machines)]
    for aid, machine in zip(ids, machines):
        connect(studio, aid, machine)
    signin(studio, "owner")
    for aid, machine in zip(ids, machines):
        assert studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": machine.pin}).status_code == 204
    return ids


def mirrors(aid, project):
    return f"/api/code/agents/{aid}/projects/{project}/mirrors"


def send(client, url, cid, agent, plan=None):
    """Отправить в копию `cid` компьютера `agent` по плану сравнения (нет — сравнить сейчас); ход передачи после
    её конца."""
    import project_mirrors
    if plan is None:
        response = client.post(f"{url}/{cid}/compare")
        assert response.status_code == 200, response.text
        plan = response.json()["plan"]
    started = client.post(f"{url}/{cid}/sync", json={"plan": plan})
    assert started.status_code == 202, started.text

    async def finish():
        if (task := project_mirrors.TRANSFERS.get(agent)) is not None:
            await task
    client.portal.call(finish)
    return client.get(f"{url}/{cid}").json()["job"]


@pytest.mark.integration  # Три компьютера, несколько полных передач.
def test_group_is_seen_from_every_copy_and_sends_from_any_of_them(studio, trio, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    source, first, second, third = trio
    a, b, c = signed_in(studio, first, second, third)
    client = studio.c
    root_b = mirror.projects.list_roots(second.roots)[1]
    root_c = mirror.projects.list_roots(third.roots)[0]
    url_a = mirrors(a, pid(source.name))
    added = [client.post(url_a, json={"destination": agent, "root": root["id"], "name": source.name})
             for agent, root in ((b, root_b), (c, root_c))]
    assert [response.status_code for response in added] == [201, 201], [response.text for response in added]
    cb, cc = (response.json()["id"] for response in added)
    listing = client.get(url_a).json()
    ca = listing["copy"]
    assert [item["destination"] for item in listing["items"]] == [b, c]
    url_b = mirrors(b, mirror.projects.project_id(second.roots, Path(root_b["path"]), source.name))
    seen = client.get(url_b).json()
    assert seen["copy"] == cb and [item["id"] for item in seen["items"]] == [ca, cc]

    assert send(client, url_a, cb, b)["state"] == "done" and send(client, url_a, cc, c)["state"] == "done"
    clone_b, clone_c = Path(root_b["path"]) / source.name, Path(root_c["path"]) / source.name
    (clone_b / "notes.txt").write_bytes(b"edited on B\n")
    # По одной копии, каждая — своим сравнением после передачи в предыдущую (так же — копия, бывшая не на связи во
    # время «Отправить во все копии»). Пары B–C ещё нет; в C версия A, которую B держал после синхронизации с A.
    # Передача B→A переписала состояние B в паре A–B, но накопленное B эту версию помнит (правило 3).
    for cid, agent in ((ca, a), (cc, c)):
        plan = client.post(f"{url_b}/{cid}/compare").json()
        assert plan["conflicts"] == 0, plan
        job = send(client, url_b, cid, agent, plan["plan"])
        assert job["state"] == "done", job
    assert (source / "notes.txt").read_bytes() == (clone_c / "notes.txt").read_bytes() == b"edited on B\n"

    (clone_b / "notes.txt").write_bytes(b"edited on B again\n")
    assert send(client, url_b, cc, c)["state"] == "done"
    assert client.post(f"{url_a}/{cc}/compare").json()["conflicts"] == 1  # этой версии A не держал
    assert "notes.txt" not in caplog.text and "edited on B" not in caplog.text and "PRIVATE" not in caplog.text


def test_joining_a_folder_of_another_group_asks_first_and_merges(studio, trio):
    import mirror_groups
    source, first, second, third = trio
    a, b, c = signed_in(studio, first, second, third)
    client = studio.c
    root_b = mirror.projects.list_roots(second.roots)[1]
    root_c = mirror.projects.list_roots(third.roots)[0]
    for name in (source.name, "solo"):
        (Path(root_c["path"]) / name).mkdir()
    project_c = mirror.projects.project_id(third.roots, Path(root_c["path"]), source.name)
    url_a, url_c = mirrors(a, pid(source.name)), mirrors(c, project_c)
    url_solo = mirrors(c, mirror.projects.project_id(third.roots, Path(root_c["path"]), "solo"))
    on_b = {"destination": b, "root": root_b["id"]}
    assert client.post(url_a, json={**on_b, "name": source.name}).status_code == 201   # {A, B}
    assert client.post(url_solo, json={**on_b, "name": "solo"}).status_code == 201     # {C, папка solo на B}

    def refused(url, settings, code, computers):
        response = client.post(url, json=settings)
        assert response.status_code == 409 and response.json()["detail"]["code"] == code, response.text
        assert response.json()["detail"]["computers"] == computers

    refused(url_a, {**on_b, "name": "solo"}, "grouped", ["Third"])
    refused(url_a, {**on_b, "name": "solo", "join": True}, "conflict", ["Destination"])
    refused(url_c, {**on_b, "name": source.name}, "grouped", ["Source"])
    joined = client.post(url_c, json={**on_b, "name": source.name, "join": True})
    assert joined.status_code == 201, joined.text
    assert [item["destination"] for item in client.get(url_a).json()["items"]] == [b, c]
    assert [item["destination"] for item in client.get(url_c).json()["items"]] == [a, b]

    # Группа из одной копии (остальные компьютеры отозваны) сливается с группой выбранной папки.
    assert client.delete(f"{url_a}/{client.get(url_c).json()['copy']}").status_code == 204
    uid = state.DB.execute("SELECT user_id FROM agents WHERE id=?", (c,)).fetchone()[0]
    with mirror_groups.transaction():
        mirror_groups.add(mirror_groups.create_group(uid), c, project_c, root_c["id"], source.name, root_c["path"])
    root_a = mirror.projects.list_roots(first.roots)[0]
    on_a = {"destination": a, "root": root_a["id"], "name": source.name}
    refused(url_c, on_a, "grouped", ["Destination"])
    merged = client.post(url_c, json={**on_a, "join": True})
    assert merged.status_code == 201, merged.text
    assert [item["destination"] for item in client.get(url_c).json()["items"]] == [a, b]
    assert [item["destination"] for item in client.get(url_a).json()["items"]] == [b, c]


def test_adopted_mirror_is_located_and_listed_from_both_computers(studio, computers):
    import json
    import mirror_groups
    import security
    source, first, second = computers
    a, b = signed_in(studio, first, second)
    client = studio.c
    root = mirror.projects.list_roots(second.roots)[1]
    shutil.copytree(source, Path(root["path"]) / source.name)
    location = {"name": source.name, "folder": root["path"], "exclude": [".venv"]}
    state.DB.execute(
        "INSERT INTO project_mirrors(source, project, destination, root, name_key, location) VALUES(?,?,?,?,?,?)",
        (a, pid(source.name), b, root["id"], mirror_groups.name_key(source.name),
         security.encrypt(json.dumps(location))))
    assert mirror_groups.adopt() == 1
    url_a = mirrors(a, pid(source.name))
    listing = client.get(url_a).json()
    item, = listing["items"]
    assert (item["destination"], item["name"], item["folder"]) == (b, source.name, root["path"])
    assert listing["exclude"] == [".venv"] and listing["pending"] is True
    url_b = mirrors(b, mirror.projects.project_id(second.roots, Path(root["path"]), source.name))
    assert client.get(url_b).json() == {"copy": None, "items": [], "exclude": [], "pending": True}
    located = client.post(url_b + "/locate").json()
    assert located["copy"] == item["id"] and [other["destination"] for other in located["items"]] == [a]
    assert located["items"][0]["unknown"] is True and located["exclude"] == [".venv"]
    assert client.post(url_a + "/locate").json()["pending"] is False
    seen = client.get(url_b).json()["items"][0]
    assert "unknown" not in seen and seen["folder"] == mirror.projects.list_roots(first.roots)[0]["path"]
    assert client.post(f"{url_b}/{listing['copy']}/compare").json()["changes"] == []


def test_open_sqlite_database_is_skipped_with_a_reason_in_the_comparison(studio, computers):
    import sqlite3
    source, first, second = computers
    database = sqlite3.connect(source / "data.db")
    try:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA wal_autocheckpoint=0")
        database.execute("CREATE TABLE t(x)")
        database.execute("INSERT INTO t VALUES (1)")
        database.commit()
        a, b = signed_in(studio, first, second)
        client = studio.c
        root = mirror.projects.list_roots(second.roots)[1]
        url = mirrors(a, pid(source.name))
        created = client.post(url, json={"destination": b, "root": root["id"], "name": source.name})
        assert created.status_code == 201, created.text
        endpoint = f"{url}/{created.json()['id']}"
        plan = client.post(endpoint + "/compare")
        assert plan.status_code == 200, plan.text
        reasons = plan.json()["reasons"]
        assert reasons["data.db"] == "sqlite" and reasons["data.db-wal"] == "sqlite-journal"
        assert [frame.get("reasons") for frame in second.requests if frame["op"] == "mirror_destination"][-1] is True
        second.groups = False  # прежний мост назначения поля `reasons` не знает
        connect(studio, b, second)
        assert client.post(f"/api/code/agents/{b}/open", json={"pin": "2468"}).status_code == 204
        assert client.post(endpoint + "/compare").json()["reasons"]["data.db"] == "sqlite"
        assert "reasons" not in [frame for frame in second.requests if frame["op"] == "mirror_destination"][-1]
        assert [frame for frame in first.requests if frame["op"] == "mirror_scan"][-1]["reasons"] is True
    finally:
        database.close()


def test_old_bridge_refusing_an_open_sqlite_database_asks_to_update_that_computer(studio, computers):
    """Мост без `mirror_groups` при открытой базе SQLite отказывает во всём списке: окно просит обновить мост этого
    компьютера, а не показывает сырой отказ. Тот же отказ нового моста (его быть не должно) — как есть."""
    import project_mirrors
    source, first, second = computers
    a, b = signed_in(studio, first, second)
    client = studio.c
    root = mirror.projects.list_roots(second.roots)[1]
    url = mirrors(a, pid(source.name))
    created = client.post(url, json={"destination": b, "root": root["id"], "name": source.name})
    assert created.status_code == 201, created.text

    def refuse(req, events):
        raise AgentRefused("busy", project_mirrors.OLD_SQLITE)
    second._op_mirror_destination = refuse
    for groups, code, words in ((False, "unsupported", "компьютере «Destination»"), (True, "busy", "SQLite")):
        second.groups = groups
        connect(studio, b, second)
        assert client.post(f"/api/code/agents/{b}/open", json={"pin": "2468"}).status_code == 204
        refused = client.post(f"{url}/{created.json()['id']}/compare")
        assert refused.json()["detail"]["code"] == code and words in refused.json()["detail"]["message"], refused.text


def test_renamed_project_rejoins_its_group_replacing_the_former_copy(studio, computers, monkeypatch):
    """Папка проекта переименована — новый pid. Поиск копий показывает папку группы с её компьютерами; присоединение
    требует снять прежнюю копию этого компьютера (`replace`)."""
    source, first, second = computers
    monkeypatch.setattr(mirror, "_repositories", lambda _: {"3" * 64})
    a, b = signed_in(studio, first, second)
    client = studio.c
    root = mirror.projects.list_roots(second.roots)[1]
    settings = {"destination": b, "root": root["id"], "name": source.name}
    old_url = mirrors(a, pid(source.name))
    old = client.post(old_url, json=settings)
    assert old.status_code == 201, old.text
    (Path(root["path"]) / source.name).mkdir()
    moved = source.with_name("project-v2")
    source.rename(moved)
    url = mirrors(a, pid(moved.name))
    assert client.get(url).json()["copy"] is None
    found, = client.post(url + f"/discover?destination={b}").json()["devices"][0]["items"]
    assert found["name"] == source.name and found["grouped"] == ["Source"]
    refused = client.post(url, json={**settings, "join": True})
    assert refused.status_code == 409 and refused.json()["detail"]["former"] is True, refused.text
    relinked = client.post(url, json={**settings, "existing": True, "join": True, "replace": True})
    assert relinked.status_code == 201, relinked.text
    assert client.get(old_url).json()["copy"] is None
    assert [item["id"] for item in client.get(url).json()["items"]] == [old.json()["id"]]
    assert client.post(url + f"/discover?destination={b}").json()["devices"] == []
    again = client.post(url, json={**settings, "join": True, "replace": True})
    assert again.status_code == 409 and "former" not in again.json()["detail"]


def test_bridge_without_groups_is_refused_as_source_and_still_works_as_destination(studio, computers):
    source, first, second = computers
    first.groups = False
    a, b = signed_in(studio, first, second)
    client = studio.c
    root = mirror.projects.list_roots(second.roots)[1]
    url = mirrors(a, pid(source.name))
    settings = {"destination": b, "root": root["id"], "name": source.name}
    refused = client.post(url, json=settings)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "unsupported"
    first.groups, second.groups = True, False
    for aid, machine in ((a, first), (b, second)):
        connect(studio, aid, machine)
        assert client.post(f"/api/code/agents/{aid}/open", json={"pin": machine.pin}).status_code == 204
    created = client.post(url, json=settings)
    assert created.status_code == 201, created.text
    plan = client.post(f"{url}/{created.json()['id']}/compare")
    assert plan.status_code == 200 and plan.json()["exists"] is False, plan.text
    assert not any("reasons" in frame for frame in second.requests)
    assert [frame for frame in first.requests if frame["op"] == "mirror_scan"][-1]["reasons"] is True


def test_group_sealed_with_a_former_key_lists_unlinks_and_rewrites_exclusions(studio, computers):
    """Сменился `STUDIO_DATA_KEY`: список не падает 500 — копия с `reenter` без имени и папки, исключения группы —
    `exclude_reenter`; сравнение и разница — 409 `reenter`; исключения можно записать заново, копию — отвязать."""
    from cryptography.fernet import Fernet
    source, first, second = computers
    a, b = signed_in(studio, first, second)
    client = studio.c
    root = mirror.projects.list_roots(second.roots)[1]
    url = mirrors(a, pid(source.name))
    mid = client.post(url, json={"destination": b, "root": root["id"], "name": source.name}).json()["id"]
    assert client.put(url + "/exclude", json={"exclude": [".venv"]}).status_code == 200
    stale = Fernet(Fernet.generate_key())
    state.DB.execute("UPDATE mirror_copies SET location = ? WHERE id = ?",
                     (stale.encrypt(b'{"name":"x","folder":"y"}'), mid))
    state.DB.execute("UPDATE mirror_groups SET exclude = ?", (stale.encrypt(b'[".venv"]'),))
    listing = client.get(url)
    assert listing.status_code == 200
    item, = listing.json()["items"]
    assert item["reenter"] is True and item["name"] is None and item["folder"] is None
    assert listing.json()["exclude"] == [] and listing.json()["exclude_reenter"] is True
    assert client.get(f"{url}/{mid}").json()["reenter"] is True
    for suffix, body in (("/compare", None), ("/diff", {"path": "notes.txt"})):
        response = client.post(f"{url}/{mid}{suffix}", **({"json": body} if body else {}))
        assert response.status_code == 409 and response.json()["detail"]["code"] == "reenter", suffix
    rewritten = client.put(url + "/exclude", json={"exclude": ["dist"]})
    assert rewritten.status_code == 200 and rewritten.json()["exclude"] == ["dist"], rewritten.text
    assert "exclude_reenter" not in rewritten.json()
    assert client.delete(f"{url}/{mid}").status_code == 204
    assert client.get(url).json() == {"copy": None, "items": [], "exclude": [], "pending": False}
    # Окно узнаёт исчезнувшую копию по признаку `gone`, а не по словам (их переводит страница).
    for method, suffix in (("post", "/compare"), ("get", ""), ("delete", "")):
        gone = getattr(client, method)(f"{url}/{mid}{suffix}")
        assert gone.status_code == 404 and gone.json()["detail"]["gone"] is True, suffix
    assert client.put(url + "/exclude", json={"exclude": []}).json()["detail"]["gone"] is True
