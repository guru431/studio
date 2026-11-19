"""Маршруты Студии и реальные операции диска моста на двух временных корнях."""
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

    @property
    def hello(self):
        return {**super().hello, "project_mirror": True, "project_mirror_discovery": self.discovery,
                "mirror_exclude": self.exclusions}

    def handle(self, req):
        try:
            return super().handle(req)
        except AgentRefused as err:
            return {"t": "res", "id": req["id"], "ok": False, "code": err.code, "message": err.message}, []

    def _op_project_roots(self, req, events):
        return {"items": mirror.projects.list_roots(self.roots)}

    def _op_mirror_identity(self, req, events):
        return mirror.identity(self.source)

    def _op_mirror_candidates(self, req, events):
        return mirror.candidates(self.roots, req["name"], req["repositories"])

    def _op_mirror_scan(self, req, events):
        assert req["project"] == pid(self.source.name)
        return mirror.scan(self.source, req.get("exclude") or ())

    def _op_mirror_destination(self, req, events):
        return mirror.destination(self.roots, req["root"], req["name"], metadata_only=req.get("metadata_only"),
                                  exclude=req.get("exclude") or ())

    def _op_mirror_read(self, req, events):
        return mirror.read(self.source, req["reads"])

    def _op_read(self, req, events):
        return files.read_text(self.source, req["path"])

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


def test_windows_source_preserves_posix_executable_flags():
    from project_mirrors import _plan, _portable

    original = ["file", "0" * 64, 3, 1]
    target = {"run": original}
    source = {"run": ["file", original[1], 3, 0]}
    assert _plan(_portable(source, "linux", "win32"), target, target)[2] == []
    new = ["file", "1" * 64, 4, 0]
    updates, remove, _, conflicts, _ = _plan(_portable({"run": new}, "darwin", "win32"), target, target)
    assert updates == {"run": ["file", new[1], 4, 1]} and not remove and not conflicts


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
        assert client.put(endpoint + "/exclude", json={"exclude": bad}).status_code == 422, bad
    for capable in (False, True):  # прежний мост назначения удалил бы исключённое в копии
        second.exclusions = capable
        connect(studio, right, second)
        assert client.post(f"/api/code/agents/{right}/open", json={"pin": "2468"}).status_code == 204
        if not capable:
            refused = client.put(endpoint + "/exclude", json={"exclude": [".venv"]})
            assert refused.json()["detail"]["code"] == "unsupported"
    saved = client.put(endpoint + "/exclude", json={"exclude": [".venv", "/.venv/"]})
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
    assert client.put(endpoint + "/exclude", json={"exclude": []}).json()["exclude"] == []


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
    assert client.get(url).json() == {"items": []}
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
    assert discover() == []
    assert (clone / "notes.txt").read_bytes() == b"local edit"
    listing = client.get(url).json()["items"]
    assert [item["name"] for item in listing] == ["renamed"]

    second.pin = None
    second.passive_requests = True
    connect(studio, right, second)
    clone.rename(renamed)
    assert [item["name"] for item in discover()] == ["moved-away"]
    assert [(frame["op"], frame.get("passive")) for frame in second.requests[-2:]] == [
        ("open", True), ("mirror_candidates", True)]
