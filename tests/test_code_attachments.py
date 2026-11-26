"""Файлы агента: только ретрансляция, ограничения общего объёма и явная совместимость."""

import base64

import pytest

import bridge
import claude_code
from test_code import A, ALPHA, DONE, chat_url, detail, sent, setup
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
    assert (result.status_code, detail(result)["message"]) == (422, "можно прикрепить до 8 файлов")
    monkeypatch.setattr(claude_code, "IMAGES_MAX", 25)
    result = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": [FILE], "images": [PNG]})
    assert result.status_code == 413 and not sent(machine, "turn")


def test_excess_attachment_count_is_refused_before_decoding(studio, monkeypatch):
    aid, machine, _ = setup(studio)

    def unexpected(*args):
        pytest.fail("Изображения декодируются только после проверки числа")

    monkeypatch.setattr(claude_code, "_turn_images", unexpected)
    answer = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "images": [PNG] * 5})
    assert (answer.status_code, detail(answer)["message"]) == (422, "можно прикрепить до 4 изображений")
    assert not sent(machine, "turn")


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


def test_old_bridge_is_refused_with_its_own_number_before_the_bridge(studio):
    """Мост 0.47 без `hello.limits`: прежние 4 изображения и 8 вложений, отказ словами с этим числом до моста."""
    aid, machine, link = setup(studio)
    assert studio.c.get("/api/agents").json()["items"][0]["limits"] == {"images": 4, "attachments": 8}
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "images": [PNG] * 5})
    assert (r.status_code, detail(r)) == (422, {"code": "invalid", "message": "можно прикрепить до 4 изображений"})
    link.hello["attachments"] = True
    r = studio.c.post(chat_url(aid, "steer"), json={"text": "x", "attachments": [FILE] * 9})
    assert r.status_code == 422 and detail(r)["message"] == "можно прикрепить до 8 файлов"
    # больше общего предела формы (13) — отказ разбора тела, но слова — с числом этого моста
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": [FILE] * 13})
    assert (r.status_code, detail(r)) == (422, {"code": "invalid", "message": "нужны текст и до 8 вложений"})
    link.hello["limits"] = {"images": 12, "attachments": 12}
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "attachments": [FILE] * 13})
    assert detail(r)["message"] == "нужны текст и до 12 вложений"
    assert not sent(machine, "turn") and not sent(machine, "steer")
