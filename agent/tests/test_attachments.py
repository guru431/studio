"""Произвольные вложения доходят до CLI файлами и удаляются после хода."""

import asyncio
import base64
import json
import threading
from pathlib import Path

import pytest

from fake_sdk import HOLD, FakeFactory
from studio_agent import attachments, limits, projects
from studio_agent.errors import Refused
from test_agent import CHAT, agent, opened, req, root, settle
from test_codex import CHAT as ENGINE_CHAT, asking as codex_asking, runner as codex_runner
from test_opencode import asking as opencode_asking, runner as opencode_runner
from test_turns import until

FILE = {"name": "report.pdf", "mime": "application/pdf", "data": base64.b64encode(b"%PDF-test").decode()}
TEXT = {"name": "notes.txt", "mime": "text/plain", "data": base64.b64encode("Текст\nвложения".encode()).decode()}


def metadata(prompt):
    return json.loads(prompt.rsplit("\n", 1)[-1])


async def test_attachment_only_and_followup_are_readable_until_claude_disconnect(agent):
    factory = FakeFactory([HOLD])
    agent.runner.factory = factory
    await opened(agent)
    pid = projects.pid("demo")
    result = (await agent.handle(req("turn", project=pid, chat=CHAT, text="", attachments=[TEXT])))[0]
    assert result["ok"]
    t = agent.runner._turns[CHAT]
    await until(lambda: t.accepted)
    first = Path(metadata(factory.clients[0].queries[0])[0]["path"])
    assert first.read_bytes() == base64.b64decode(TEXT["data"])
    reply = (await agent.handle(req("steer", project=pid, chat=CHAT, text="", attachments=[TEXT])))[0]
    assert reply["ok"]
    second = Path(metadata(factory.clients[0].queries[1])[0]["path"])
    assert second != first and second.read_bytes() == base64.b64decode(TEXT["data"])
    _, snapshot = await agent.runner.watch(pid, CHAT)
    assert "studio-attachments-" not in str(snapshot) and TEXT["data"] not in str(snapshot)
    await agent.runner.stop(pid, CHAT)
    await t.task
    assert factory.clients[0].disconnected
    assert not first.parent.exists()


@pytest.mark.parametrize("items", [None, {}, [FILE] * 9, [{"name": "x", "mime": "text/plain", "data": "???"}],
    [{"name": "x", "mime": "text/plain", "path": "C:/private.txt"}],
    [{**FILE, "name": ".."}], [{**FILE, "mime": "text/plain\nX: secret"}]])
async def test_invalid_attachment_never_starts_cli(agent, items):
    await opened(agent)
    result = (await agent.handle(req("turn", project=projects.pid("demo"), chat=CHAT, text="x",
                                     attachments=items)))[0]
    assert result["code"] == "invalid" and not agent.runner.live()


def test_combined_size_and_count_limits_include_images(monkeypatch):
    from test_image_input import PNG

    with pytest.raises(Refused, match="invalid"):
        attachments.validate([FILE] * 8, [PNG])
    monkeypatch.setattr(limits, "IMAGES_MAX", 25)
    with pytest.raises(Refused, match="too_large"):
        attachments.validate([FILE], [PNG])
    monkeypatch.setattr(limits, "IMAGE_MAX", 4)
    with pytest.raises(Refused, match="too_large"):
        attachments.validate([FILE])


@pytest.mark.parametrize("operation", ["turn", "steer"])
async def test_combined_size_is_checked_before_start_or_followup(agent, monkeypatch, operation):
    from test_image_input import PNG

    await opened(agent)
    monkeypatch.setattr(limits, "IMAGES_MAX", 25)
    reply = (await agent.handle(req(operation, project=projects.pid("demo"), chat=CHAT, text="",
                                    attachments=[FILE], images=[PNG])))[0]
    assert reply["code"] == "too_large" and not agent.runner.live()


def test_original_name_never_controls_disk_path_and_empty_files_are_allowed():
    files = attachments.Files()
    try:
        prompt = files.prompt("Read", [{**FILE, "name": "../../private\\report\n.pdf", "data": ""}])
        [item] = metadata(prompt)
        path = Path(item["path"])
        assert item["name"] == "report.pdf" and path.name == "file-1.pdf"
        assert path.read_bytes() == b"" and path.parent.name.startswith("studio-attachments-")
    finally:
        files.cleanup()
    assert not path.parent.exists()


async def test_cancel_during_disk_write_waits_for_write_before_cleanup(agent, monkeypatch):
    ready, release = threading.Event(), threading.Event()
    original = attachments.Files.prompt
    captured = []

    def held(self, text, items):
        result = original(self, text, items)
        captured.append(Path(metadata(result)[0]["path"]))
        ready.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(attachments.Files, "prompt", held)
    await opened(agent)
    result = (await agent.handle(req("turn", project=projects.pid("demo"), chat=CHAT, text="",
                                     attachments=[FILE])))[0]
    assert result["ok"]
    t = agent.runner._turns[CHAT]
    await until(ready.is_set)
    t.task.cancel()
    await asyncio.sleep(0)
    assert captured[0].exists()
    release.set()
    await asyncio.gather(t.task, return_exceptions=True)
    assert not captured[0].parent.exists()


async def test_codex_turn_and_steer_receive_local_paths_and_release_files(codex_runner):
    r = codex_runner
    r.start(r.project, "project", ENGINE_CHAT, "", None, None, attachments=[TEXT])
    t = r._turns[ENGINE_CHAT]
    await codex_asking(r)
    first = Path(metadata(next(p for m, p in r.fake.calls if m == "turn/start")["input"][0]["text"])[0]["path"])
    assert first.read_bytes() == base64.b64decode(TEXT["data"])
    await r.steer("project", ENGINE_CHAT, "", [], attachments=[TEXT])
    second = Path(metadata(next(p for m, p in r.fake.calls if m == "turn/steer")["input"][0]["text"])[0]["path"])
    assert second.read_bytes() == base64.b64decode(TEXT["data"])
    await r.stop("project", ENGINE_CHAT)
    await t.task
    assert not first.parent.exists() and not second.exists()


async def test_opencode_queued_attachment_lives_until_stop_and_unsent_has_no_local_path(opencode_runner):
    r = opencode_runner
    r.start(r.project, r.pid, ENGINE_CHAT, "", None, None, attachments=[TEXT])
    t = r._turns[ENGINE_CHAT]
    await opencode_asking(r)
    prompt = next(p for m, p in r.fake.calls if m == "session/prompt")["prompt"][0]["text"]
    first = Path(metadata(prompt)[0]["path"])
    assert first.read_bytes() == base64.b64decode(TEXT["data"])
    reply = await r.steer(r.pid, ENGINE_CHAT, "Следом", [], attachments=[TEXT])
    second = Path(metadata(t.queued[0][1])[0]["path"])
    assert reply["mode"] == "queue" and second.read_bytes() == base64.b64decode(TEXT["data"])
    await r.stop(r.pid, ENGINE_CHAT)
    await t.task
    assert not first.parent.exists() and not second.exists()
    assert r.sent[-1]["e"]["unsent"] == [{"id": reply["id"], "text": "Следом\n[Вложений: 1]"}]


async def test_other_window_cannot_write_steer_attachments(codex_runner, monkeypatch):
    r = codex_runner
    r.start(r.project, "project", ENGINE_CHAT, "Start", None, None, owner="a" * 32)
    t = r._turns[ENGINE_CHAT]
    await codex_asking(r)

    def forbidden(*_):
        raise AssertionError("write before ownership check")

    monkeypatch.setattr(attachments.Files, "prompt", forbidden)
    try:
        with pytest.raises(Refused, match="owned"):
            await r.steer("project", ENGINE_CHAT, "", [], owner="b" * 32, attachments=[FILE])
        assert t.files._directory is None
    finally:
        await r.stop("project", ENGINE_CHAT)
        await t.task


@pytest.mark.parametrize("backend", ["agent", "codex_runner", "opencode_runner"])
async def test_attachment_write_failure_cleans_files_and_restores_unaccepted_input(request, monkeypatch, backend):
    instance = request.getfixturevalue(backend)
    if backend == "agent":
        r, project = instance.runner, Path(instance.cfg.projects_root) / "demo"
        sent = instance.sent
    else:
        r, project, sent = instance, instance.project, instance.sent
    original, paths = attachments.Files.prompt, []

    def failure(self, text, items):
        prompt = original(self, text, items)
        paths.append(Path(metadata(prompt)[0]["path"]))
        raise OSError("disk unavailable: private path")

    monkeypatch.setattr(attachments.Files, "prompt", failure)
    r.start(project, "project", ENGINE_CHAT, "Read", None, None, attachments=[TEXT])
    await r._turns[ENGINE_CHAT].task
    assert paths and not paths[0].parent.exists()
    assert sent[-1]["e"]["status"] == "error" and sent[-1]["e"]["not_started"] is True
