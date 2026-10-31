"""Операции дерева на телефоне и широком экране через сервер и подставной мост."""

import pytest

from fake_bridge import FakeMachine
from test_ui_browser import READY, browser, code_item, code_online, code_tab, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def menu(path):
    return ("click", f'#code-files li[data-path="{path}"] > .code-file-menu')


def action(name):
    return ("click", f'#code-file-actions [data-action="{name}"]')


def exists(path):
    return f'!!document.querySelector(\'#code-files li[data-path="{path}"]\')'


DONE = ("wait", "!document.querySelector('#code-file-operation[open]') && !document.querySelector('#code-files[aria-busy]')")


@pytest.mark.parametrize("width", [320, 1280])
def test_tree_create_rename_move_delete_and_unsaved_guard(browser, live, width, tmp_path):
    login = f"file-operations-{width}"
    live.add_user(login, theme="dark" if width == 320 else "light")
    machine = FakeMachine({"alpha": {"files": {"src/app.txt": b"original", "dest/keep.txt": b"keep"}}})
    machine.file_operations = True
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            ("wait", exists("src")),
            ("click", "#code-new-folder"), set_value("#code-file-operation-path", "new"),
            ("click", "#code-file-operation-submit"), DONE, ("wait", exists("new")),
            ("click", code_item("src")), ("wait", exists("src/app.txt")),
            ("click", "#code-new-file"), set_value("#code-file-operation-path", "draft.txt"),
            ("click", "#code-file-operation-submit"), DONE, ("wait", exists("src/draft.txt")),
            menu("src/draft.txt"), action("rename"), set_value("#code-file-operation-path", "renamed.txt"),
            ("click", "#code-file-operation-submit"), DONE, ("wait", exists("src/renamed.txt")),
            menu("src/renamed.txt"), action("move"), set_value("#code-file-operation-path", "new/renamed.txt"),
            ("click", "#code-file-operation-submit"), DONE,
            ("click", code_item("new")), ("wait", exists("new/renamed.txt")),
            ("eval", "scroll", "document.querySelector('.code-file-table').scrollLeft = 0"),
            ("eval", "menuFits", """(() => {
              const b = document.querySelector('#code-files li[data-path="new/renamed.txt"] > .code-file-menu').getBoundingClientRect();
              const table = document.querySelector('.code-file-table').getBoundingClientRect();
              return b.width >= 28 && b.left >= table.left && b.right <= Math.min(table.right, innerWidth);
            })()"""),
            ("screenshot", str(tmp_path / f"file-tree-operations-{width}.png")),
            ("click", code_item("renamed.txt")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "unsaved"),
            ("click", "#code-open-file-actions"), action("rename"), set_value("#code-file-operation-path", "saved.txt"),
            ("click", "#code-file-operation-submit"),
            ("wait", "document.querySelector('#code-file-operation-error').textContent.includes('Сначала сохраните')"),
            ("eval", "kept", "document.querySelector('#code-editor').value"),
            ("click", "#code-file-operation .dialog-close"), ("click", "#code-save"),
            ("wait", "document.querySelector('#code-file-status').textContent === 'Сохранено'"),
            ("click", "#code-open-file-actions"), action("rename"), set_value("#code-file-operation-path", "saved.txt"),
            ("click", "#code-file-operation-submit"), DONE,
            ("wait", "document.querySelector('#code-editor')?.getAttribute('aria-label') === 'saved.txt'"),
            ("eval", "renamedContent", "document.querySelector('#code-editor').value"),
            ("click", "#code-open-file-actions"), action("delete"),
            ("eval", "confirm", "document.querySelector('#code-file-operation').textContent"),
            ("click", "#code-file-operation .dialog-close"),
            ("eval", "cancelKept", "document.querySelector('#code-editor').value"),
            ("click", "#code-open-file-actions"), action("delete"),
            ("click", "#code-file-operation-submit"), DONE,
            ("wait", "document.querySelector('#code-file').hidden"),
            menu("src/app.txt"), action("move"), set_value("#code-file-operation-path", "dest/keep.txt"),
            ("click", "#code-file-operation-submit"),
            ("wait", "document.querySelector('#code-file-operation-error').textContent.includes('уже существует')"),
            ("click", "#code-file-operation .dialog-close"),
            menu("src"), action("delete"),
            ("eval", "recursive", "document.querySelector('#code-file-operation').textContent"),
            ("screenshot", str(tmp_path / f"file-delete-confirm-{width}.png")),
            ("click", "#code-file-operation-submit"), DONE,
            ("wait", "!" + exists("src")),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["menuFits"] and not values["overflow"]
    assert values["kept"] == values["renamedContent"] == values["cancelKept"] == "unsaved"
    assert "без возможности отмены" in values["confirm"]
    assert "всё её содержимое" in values["recursive"]
    assert machine.projects["alpha"]["files"] == {"dest/keep.txt": b"keep"}
    assert len([r for r in machine.requests if r["op"] == "file_operation" and r["operation"] == "delete"]) == 2
    errors = problems(result)
    assert len(errors) == 1 and "409 (Conflict)" in errors[0] and "/files/operate" in errors[0], errors


def test_old_bridge_keeps_reading_without_mutation_controls(browser, live):
    login = "file-operations-old"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"files": {"a.txt": b"old bridge"}}})
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            ("wait", exists("a.txt")),
            ("eval", "hidden", "document.querySelector('.code-files-create').hidden && !document.querySelector('.code-file-menu')"),
            ("click", code_item("a.txt")), ("wait", "document.querySelector('#code-editor')"),
            ("eval", "content", "document.querySelector('#code-editor').value")])
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["hidden"] and result["values"]["content"] == "old bridge"
    assert not problems(result), problems(result)


def test_moving_parent_retargets_editor_and_unrelated_operations_keep_draft(browser, live):
    login = "file-operations-parent"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"files": {"src/a.txt": b"original", "dest/keep.txt": b"keep", "other.txt": b"other"}}})
    machine.file_operations = True
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            ("wait", exists("src")), ("click", code_item("src")), ("wait", exists("src/a.txt")),
            ("click", code_item("a.txt")), ("wait", "document.querySelector('#code-editor')"),
            menu("src"), action("move"), set_value("#code-file-operation-path", "dest/moved"),
            ("click", "#code-file-operation-submit"), DONE, ("wait", exists("dest/moved/a.txt")),
            ("wait", "document.querySelector('#code-editor')?.value === 'original'"),
            ("eval", "path", "document.querySelector('#code-path').textContent"),
            set_value("#code-editor", "after move"), ("click", "#code-save"),
            ("wait", "document.querySelector('#code-file-status').textContent === 'Сохранено'"),
            ("click", code_item("other.txt")), ("wait", "document.querySelector('#code-editor')?.value === 'other'"),
            set_value("#code-editor", "draft"), ("click", "#code-new-file"),
            set_value("#code-file-operation-path", "extra.txt"), ("click", "#code-file-operation-submit"), DONE,
            ("wait", exists("extra.txt")), ("eval", "afterCreate", "document.querySelector('#code-editor').value"),
            menu("extra.txt"), action("delete"), ("click", "#code-file-operation-submit"), DONE,
            ("wait", "!" + exists("extra.txt")), ("eval", "afterDelete", "document.querySelector('#code-editor').value")], width=1280)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert "dest" in result["values"]["path"] and "moved" in result["values"]["path"]
    assert result["values"]["afterCreate"] == result["values"]["afterDelete"] == "draft"
    assert machine.projects["alpha"]["files"] == {"dest/moved/a.txt": b"after move", "dest/keep.txt": b"keep", "other.txt": b"other"}
    assert not problems(result), problems(result)
