"""Новые рабочие инструменты в браузере: клавиатура, очередь, ветвление и изоляция каталогов."""
import pytest

from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, READY, browser, code_chat, code_free_machine, code_online,
                             code_session, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_suggestions_diffs_content_search_and_editable_queue(browser, live, width, tmp_path):
    login = f"workspace-tools-{width}"
    live.add_user(login)
    messages = [{"id": "source", "role": "user", "text": "Первый вопрос", "steps": [], "time": 1},
                {"id": "answer", "role": "assistant", "text": "Старое решение с маркером Needle", "time": 2,
                 "steps": [{"id": "edit", "tool": "Edit", "input": {"file_path": "src/util.py"}, "text": "ok",
                            "error": False, "changes": [{"path": "src/util.py", "before": "old", "after": "new"}]}]}]
    machine = code_free_machine({CODE_CHAT_A: code_session("Основной чат", messages=messages)})
    machine.file_suggestions = machine.chat_search = machine.queue_edit = True
    machine.projects["alpha"]["files"] = {"src/util.py": b"new"}
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "diff", "document.querySelector('.code-step').open=true; true"),
            ("eval", "diffText", "document.querySelector('.code-changes').textContent"),
            ("click", "#code-text"), set_value("#code-text", "@util"), ("wait", "document.querySelector('#code-file-options button')"),
            ("press", "Enter"), ("eval", "inserted", "document.querySelector('#code-text').value"),
            ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-search-content')"),
            ("click", "#code-search-content"), set_value("#code-chat-search", "Needle"),
            ("wait", "!!document.querySelector('.code-search-snippet')"),
            ("eval", "snippet", "document.querySelector('.code-search-snippet').textContent"),
            ("click", f"#code-chats li[data-chat='{CODE_CHAT_A}'] button"),
            ("wait", f"document.querySelector('#code-chat')?.dataset.chat === '{CODE_CHAT_A}' && document.querySelector('#code-chat').dataset.loaded === '1'"),
            set_value("#code-text", "Основная задача"), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            set_value("#code-delivery", "queue"), set_value("#code-text", "После задачи"), ("click", "#code-send"),
            ("wait", "document.querySelector('.code-queue-preview')?.textContent === 'После задачи'"),
            ("click", "button[aria-label='Изменить ожидающее задание']"),
            ("wait", "!!document.querySelector('.code-queue-text')"),
            set_value(".code-queue-text", "Исправленное задание"),
            ("eval", "paused", "document.querySelector('#code-queue').textContent.includes('на паузе') && document.querySelector('#code-send').disabled"),
            ("screenshot", str(tmp_path / f"queue-edit-{width}.png")),
            ("click", ".code-queue-actions button"),
            ("wait", "document.querySelector('.code-queue-preview')?.textContent === 'Исправленное задание'"),
            ("click", "button[aria-label='Удалить ожидающее задание']"),
            ("wait", "document.querySelector('#code-queue').hidden"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("click", "#code-stop"), ("wait", "document.querySelector('#code-chat').dataset.running === '0'")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["inserted"] == "src/util.py "
    assert "old" in result["values"]["diffText"] and "new" in result["values"]["diffText"]
    assert "Needle" in result["values"]["snippet"]
    assert result["values"]["paused"] and result["values"]["fits"]
    assert not problems(result), problems(result)
    assert not any("Needle" in url for url in result.get("sent", []))
    assert len([row for row in machine.requests if row["op"] == "turn"]) == 1


@pytest.mark.parametrize("width", [320, 1280])
def test_fork_subagents_and_worktree_browser_flows(browser, live, width, tmp_path):
    login = f"branch-tools-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Основной чат", messages=[
        {"id": "first", "role": "user", "text": "Измени постановку", "steps": [], "time": 1},
        {"id": "answer", "role": "assistant", "text": "Исходный ответ", "steps": [], "time": 2}])})
    machine.chat_fork = machine.subagents = machine.worktrees = True
    machine.projects["alpha"]["chats"][CODE_CHAT_A]["children"] = {
        "reviewer": {"title": "Проверка кода", "messages": [{"role": "assistant", "text": "Результат подзадачи", "steps": []}]}}
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("click", "#code-subagents"), ("wait", "!!document.querySelector('.code-subagent-row')"),
            ("click", ".code-subagent-row"), ("wait", "document.querySelector('.code-subagent-history').textContent.includes('Результат подзадачи')"),
            ("eval", "child", "document.querySelector('.code-subagent-history').textContent"),
            ("click", "#code-subagents-dialog .dialog-actions button"),
            ("click", ".code-msg.user .code-fork"),
            ("wait", f"document.querySelector('#code-chat')?.dataset.loaded === '1' && document.querySelector('#code-chat').dataset.chat !== '{CODE_CHAT_A}'"),
            ("eval", "draft", "document.querySelector('#code-text').value"),
            ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-worktrees')"),
            ("click", "#code-worktrees"), ("wait", "!!document.querySelector('.code-worktree-row')"),
            set_value("#code-worktree-name", "task"), ("click", ".code-worktree-form button"),
            ("wait", f"location.hash.includes('/{pid('alpha--task')}') && !!document.querySelector('#code-new-chat')"),
            ("wait", "document.querySelector('#code-place .code-place-project')?.textContent === 'alpha--task'"),
            ("eval", "copy", "location.hash"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("screenshot", str(tmp_path / f"worktree-{width}.png"))], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["draft"] == "Измени постановку"
    assert result["values"]["fits"] and "Результат подзадачи" in result["values"]["child"]
    assert len(machine.projects["alpha"]["chats"][CODE_CHAT_A]["messages"]) == 2
    assert not problems(result), problems(result)
