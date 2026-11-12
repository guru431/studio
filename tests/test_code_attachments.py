"""Файлы агента: только ретрансляция, ограничения общего объёма и явная совместимость."""

import base64

import pytest

import bridge
import claude_code
from test_code import A, ALPHA, DONE, chat_url, sent, setup
from test_code_images import PNG

FILE = {"name": "report.pdf", "mime": "application/pdf", "data": base64.b64encode(b"%PDF-test").decode()}


@pytest.mark.parametrize("op", ["turn", "steer"])
def test_attachment_only_is_relayed_with_sanitized_name_and_not_logged(studio, caplog, op):
    caplog.set_level("INFO", logger="studio")
    aid, machine, link = setup(studio)
    link.hello["attachments"] = True
    machine.script = [DONE]
    if op == "steer":
        machine.running[A] = {"turn": "t1", "project": ALPHA, "text": "", "steps": [], "asks": {}, "followups": []}
    result = studio.c.post(chat_url(aid, op), json={"text": "", "attachments": [{**FILE, "name": "../private\\report\n.pdf"}]})
    assert result.status_code == 200
    [request] = sent(machine, op)
    assert request["attachments"] == [FILE] and request["text"] == ""
    assert FILE["name"] not in caplog.text and FILE["data"] not in caplog.text


def test_old_bridge_cannot_silently_ignore_files(studio):
    aid, machine, link = setup(studio)
    link.hello.pop("attachments", None)
    result = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": [FILE]})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "unsupported"
    assert not sent(machine, "turn")


@pytest.mark.parametrize("items", [None, [FILE] * 9, [{**FILE, "name": ".."}], [{**FILE, "data": "???"}],
    [{"name": "x", "mime": "text/plain", "path": "C:/secret"}], [{**FILE, "mime": "text/plain\nX: a"}]])
def test_invalid_files_are_rejected_before_bridge(studio, items):
    aid, machine, link = setup(studio)
    link.hello["attachments"] = True
    result = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": items})
    assert result.status_code == 422 and not sent(machine, "turn")


def test_shared_limits_count_files_and_images_together(studio, monkeypatch):
    aid, machine, link = setup(studio)
    link.hello["attachments"] = True
    result = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": [FILE] * 8, "images": [PNG]})
    assert result.status_code == 422
    monkeypatch.setattr(claude_code, "IMAGES_MAX", 25)
    result = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": [FILE], "images": [PNG]})
    assert result.status_code == 413 and not sent(machine, "turn")


@pytest.mark.integration
@pytest.mark.parametrize("op", ["turn", "steer"])
def test_thirty_mib_of_attachments_reaches_bridge_and_one_extra_byte_is_refused(studio, op):
    aid, machine, link = setup(studio)
    link.hello.update(attachments=True, large_attachments=True)
    machine.script = [DONE]
    if op == "steer":
        machine.running[A] = {"turn": "t1", "project": ALPHA, "text": "", "steps": [], "asks": {}, "followups": []}
    ten_mib = {**FILE, "data": base64.b64encode(b"x" * (10 * 1024 * 1024)).decode()}
    result = studio.c.post(chat_url(aid, op), json={"text": "", "attachments": [ten_mib] * 3})
    assert result.status_code == 200
    [request] = sent(machine, op)
    assert request["attachments"] == [ten_mib] * 3
    result = studio.c.post(chat_url(aid, op), json={"text": "", "attachments": [ten_mib] * 3 + [{**FILE, "data": "eA=="}]})
    assert result.status_code == 413 and len(sent(machine, op)) == 1


@pytest.mark.integration
def test_legacy_bridge_refuses_large_files_without_sending_them(studio):
    aid, machine, link = setup(studio)
    link.hello["attachments"] = True
    item = {**FILE, "data": base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()}
    result = studio.c.post(chat_url(aid, "turn"), json={"text": "", "attachments": [item]})
    assert result.status_code == 409 and result.json()["detail"]["code"] == "unsupported"
    assert not sent(machine, "turn")
