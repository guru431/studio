"""Администратор заводит группу доступа и выдаёт ей общий API моделей и инструмент общего MCP (access.py)."""
from contextlib import closing

import pytest

import db
from fakes import TOKEN as MODEL_KEY
from test_mcp import TOKEN, URL
from test_mcp_sections import service
from test_ui_browser import API, NO_OVERFLOW, READY, browser, go, is_open, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


def row(items: str, name: str) -> str:
    """Строка списка `items` (селектор), в тексте которой есть `name`."""
    return f"[...document.querySelectorAll({items!r})].find(e => e.textContent.includes({name!r}))"


def group_box(scope: str, group: str) -> str:
    """Галочка группы `group` в диалоге доступа (`scope` — селектор строки или диалога)."""
    return f"{row(scope + ' .access-groups label', group)}?.querySelector('input')"


#: блоки над «Группами» дорисованы: нажатие по кнопке не уедет от сдвига страницы
ADMIN_LOADED = ("document.querySelector('#invitations tr') && document.querySelector('#users tbody tr')"
                " && document.querySelector('#allow-list li') && document.querySelector('#group-list li')")


def reload_page():
    return [("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}")]


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_sets_groups_and_tool_access(browser, live, width, monkeypatch):
    login, group = f"access-member-{width}", f"Media {width}"
    mcp, models = f"Access tools {width}", f"Access API {width}"
    live.add_user(login)
    service(live.gw, monkeypatch=monkeypatch)
    tool = "#mcp-access-dialog .access-tool[data-tool=image_generate]"
    hidden = "#mcp-access-dialog .access-tool[data-tool=internal_action]"
    retired = "#mcp-access-dialog .access-tool[data-tool=retired_tool]"
    mcp_row, card = row("#admin-shared-mcp [data-mcp-id]", mcp), row("#shared-models-dialog .link", models)
    group_row = row("#group-list li", group)
    member = f'#group-members-dialog input[data-login="{login}"]'
    user_groups = f'#users tr[data-login="{login}"] [data-col=groups]'
    open_mcp = [("wait", mcp_row), ("eval", "openMcp", f"{mcp_row}.querySelector('.mcp-access').click()"),
                ("wait", f"document.querySelector('{tool}') && !document.querySelector('#mcp-access-save').disabled")]
    open_models = [("click", "#admin-shared-models"), ("wait", f"{card}?.querySelector('.link-access')"),
                   ("eval", "openModels", f"{card}.querySelector('.link-access').click()"),
                   ("wait", "document.querySelector('#access-dialog input[value=groups]')")]
    setup = ("eval", "setup", f"""(async () => {{ const m = {API};
      const s = await m.api('POST', '/api/admin/shared/mcp', {{ name: {mcp!r}, url: {URL!r}, token: {TOKEN!r} }});
      // правило инструмента, которого у сервера нет: показывается с пометкой и сохраняется
      await m.api('PUT', `/api/admin/shared/mcp/${{s.id}}/access`, {{ groups: null, tools: {{ retired_tool: [] }} }});
      await m.api('POST', '/api/admin/shared/connections', {{ name: {models!r}, kind: 'openai',
        base_url: 'http://10.77.1.15:4000/v1', token: {MODEL_KEY!r}, sections: ['chat'] }});
      return true; }})()""")
    steps = [*signin(), ("wait", READY), setup, *go("admin"), ("wait", ADMIN_LOADED),
        # группа и участник
        ("click", "#group-add"), ("type", "#group-name", group), ("click", "#group-save"),
        ("wait", f"!{is_open('#group-dialog')} && {group_row}"),
        ("eval", "openMembers", f"{group_row}.querySelector('.group-members').click()"),
        ("wait", f"document.querySelector({member!r})"),
        ("eval", "member", f"document.querySelector({member!r}).click()"),
        ("click", "#group-members-save"),
        ("wait", f"!{is_open('#group-members-dialog')} && {group_row}.querySelector('.group-count').textContent.endsWith(': 1')"),
        ("wait", f"document.querySelector({user_groups!r})?.textContent === {group!r}"),
        ("eval", "pageFits", NO_OVERFLOW),
        # общий MCP: инструмент — группе, другой — только администраторам
        *open_mcp,
        ("eval", "missing", f"!!document.querySelector('{retired} .access-missing')"
                            f" && !document.querySelector('{tool} .access-missing')"),
        set_value(f"{tool} select", "groups"), ("eval", "pickTool", f"{group_box(tool, group)}.click()"),
        set_value(f"{hidden} select", "admins"),
        ("eval", "mcpFits", "document.querySelector('#mcp-access-dialog').scrollWidth <= innerWidth"),
        ("click", "#mcp-access-save"), ("wait", f"!{is_open('#mcp-access-dialog')}"),
        # общий API моделей: выбранные группы
        *open_models,
        ("eval", "nobody", "document.querySelector('#access-dialog .access-summary').textContent"),
        ("click", "#access-dialog input[value=groups]"),
        ("eval", "adminsOnly", "document.querySelector('#access-dialog .access-summary').textContent"),
        ("eval", "pickModels", f"{group_box('#access-dialog', group)}.click()"),
        ("eval", "accessFits", "document.querySelector('#access-dialog').scrollWidth <= innerWidth"),
        ("click", "#access-save"), ("wait", f"!{is_open('#access-dialog')}"),
        # после перезагрузки всё на месте
        *reload_page(), *go("admin"), ("wait", f"{ADMIN_LOADED} && {group_row}"),
        ("eval", "count", f"{group_row}.querySelector('.group-count').textContent"),
        *open_mcp,
        ("eval", "toolSaved", f"[document.querySelector('{tool} select').value, {group_box(tool, group)}.checked,"
                              f" document.querySelector('{tool} .access-summary').textContent,"
                              f" document.querySelector('{hidden} select').value,"
                              f" document.querySelector('{retired} select').value,"
                              " document.querySelector('#mcp-access-dialog input[value=all]').checked]"),
        ("click", "#mcp-access-dialog .dialog-close"), ("wait", f"!{is_open('#mcp-access-dialog')}"),
        *open_models,
        ("eval", "modelsSaved", f"[document.querySelector('#access-dialog input[value=groups]').checked,"
                                f" {group_box('#access-dialog', group)}.checked,"
                                " document.querySelector('#access-dialog .access-summary').textContent]"),
        ("click", "#access-dialog .dialog-close"), ("wait", f"!{is_open('#access-dialog')}"),
        ("click", "#shared-models-dialog .dialog-close"), ("wait", f"!{is_open('#shared-models-dialog')}"),
        # обычная учётка нового не видит
        ("click", "#avatar"), ("click", "#menu-logout"), ("wait", "location.pathname === '/login'"),
        *signin(login), ("wait", READY), *go("admin"), ("wait", "!location.hash.startsWith('#/admin')"),
        *go("settings/chat"), ("wait", "document.querySelector('#mcp-add')"),
        ("eval", "userSeesNothing", "!document.querySelector('#admin-groups, .mcp-access, .link-access, #group-list')")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["pageFits"] and values["mcpFits"] and values["accessFits"] and values["missing"]
    assert values["nobody"] == "Все пользователи" and values["adminsOnly"] == "Только администраторам"
    assert values["count"] == "Участников: 1"
    assert values["toolSaved"] == ["groups", True, f"Группам: {group}", "admins", "admins", True]
    assert values["modelsSaved"] == [True, True, f"Группам: {group}"]
    assert values["userSeesNothing"] is True
    with closing(db.connect(live.db_path)) as conn:
        one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]
        gid = one("SELECT id FROM access_groups WHERE name = ?", group)
        uid = one("SELECT id FROM users WHERE login = ?", login)
        sid = one("SELECT id FROM mcp_servers WHERE name = ? AND user_id IS NULL", mcp)
        cid = one("SELECT id FROM connections WHERE name = ? AND user_id IS NULL", models)
        assert [r[0] for r in conn.execute("SELECT user_id FROM access_group_members WHERE group_id = ?", (gid,))] == [uid]
        ours = "(r.kind = 'mcp' AND r.item_id = ?) OR (r.kind = 'connection' AND r.item_id = ?)"
        rules = {(r[0], r[1], r[2]): [] for r in conn.execute(
            f"SELECT r.kind, r.item_id, r.tool FROM access_rules r WHERE {ours}", (sid, cid))}
        for kind, item, name, group_id in conn.execute(
                "SELECT r.kind, r.item_id, r.tool, g.group_id FROM access_rules r JOIN access_rule_groups g"
                f" ON g.rule_id = r.id WHERE {ours}", (sid, cid)):
            rules[(kind, item, name)].append(group_id)
    assert rules == {("mcp", sid, "image_generate"): [gid], ("mcp", sid, "internal_action"): [],
                     ("mcp", sid, "retired_tool"): [], ("connection", cid, ""): [gid]}
    assert not problems(result), result.get("log")
