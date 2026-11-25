"""Навыки «Агентов» в настоящем браузере (code-skills.js): меню «/» с навыками проекта и библиотеки, чип и текст
хода, старый мост без `hello.skills`, «Положить набор в проект» и «Проверить другим помощником»."""
import pytest

from test_codex import CATALOG
from test_ui_browser import (API, CODE_CHAT_A, NO_OVERFLOW, READY, browser, code_chat, code_free_machine,  # noqa: F401
                             code_online, code_requests, code_session, known_refusals, live, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

MINE = (f"(async () => (await {API}.api('POST', '/api/skills', "
        "{name: 'Мой навык', text: 'Текст навыка', scope: 'agents'})).id)()")
CHIP = "document.querySelector('#code-skill-chip')"
IDLE = "document.querySelector('#code-chat').dataset.running === '0'"
PURE = """(async () => {
  const { mergeItems, skillFile } = await import('/static/js/code-skills.js');
  return { merged: mergeItems([{ id: 's1', name: 'Приёмка', description: '', origin: 'acceptance' }],
      [{ name: 'acceptance', description: 'Приёмка', folder: '.agents/skills' }],
      [{ name: 'compact', description: 'Сжать' }]).map(item => [item.kind, item.id]),
    file: skillFile('acceptance', 'Приёмка', 'Сверить: всё', 'Текст') };
})()"""


def turns_finish(machine, count):
    """Первые `count` ходов отвечают и кончаются — следующий ход идёт ходом, а не уточнением."""
    original = machine._op_turn

    def turn(req, events):
        if len(code_requests(machine, "turn")) <= count:
            machine.script = [{"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
        return original(req, events)
    machine._op_turn = turn


def pick(word):
    return [("type", "#code-text", word), ("press", "Enter"), ("wait", f"!{CHIP}.hidden")]


@pytest.mark.parametrize("width", [320, 1280])
def test_slash_menu_calls_project_and_library_skills(browser, live, width):
    login = f"code-skills-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Навыки")})
    machine.skills = True
    machine.projects["alpha"]["skills"] = {(".agents/skills", "review-local"): {"description": "Ревью", "text": "Проверь diff"}}
    turns_finish(machine, 2)
    aid, server = code_online(live, login, machine)
    user = "[...document.querySelectorAll('#code-feed .code-msg.user')]"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "mine", MINE),
            *code_chat(aid, CODE_CHAT_A),
            *pick("/review"),
            ("eval", "chip", f"[{CHIP}.textContent, document.querySelector('#code-text').value]"),
            ("eval", "chipFits", f"(() => {{ const r = {CHIP}.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }})()"),
            ("type", "#code-text", "только тесты"), ("click", "#code-send"),
            ("wait", f"{user}.length === 1 && {IDLE} && document.querySelector('.code-msg.assistant .md')"),
            ("eval", "first", f"{user}.map(m => [m.querySelector('.code-user-skill')?.textContent, m.querySelector('.code-user-text').textContent, m.textContent.includes('Проверь diff')])"),
            ("eval", "chipGone", f"{CHIP}.hidden"),
            *pick("/Мой"), ("click", "#code-send"),
            ("wait", f"{user}.length === 2"),
            ("eval", "fits", NO_OVERFLOW),
            ("eval", "pure", PURE)], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["chip"] == ["Навык: review-local", ""]
    assert values["first"] == [["Навык: review-local", "только тесты", False]]
    assert values["chipGone"] and values["chipFits"] and values["fits"]
    assert [t["text"] for t in code_requests(machine, "turn")] == ["Проверь diff\n\nтолько тесты", "Текст навыка"]
    assert len(code_requests(machine, "skill")) == 1
    assert code_requests(machine, "skill")[0]["name"] == "review-local"
    assert values["pure"]["merged"] == [["project", ".agents/skills/acceptance"], ["command", "compact"]]   # проектный — один раз
    assert values["pure"]["file"] == ('---\nname: acceptance\ndescription: "Приёмка — Сверить: всё"\n'
                                      'disable-model-invocation: true\n---\n\nТекст\n')
    assert not problems(result), problems(result)


def test_old_bridge_has_library_skills_only(browser, live):                       # Review Focus 1
    login = "code-skills-old-bridge"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Старый мост")})   # мост 0.47: без hello.skills
    machine.file_operations = machine.file_upload = True
    machine.script = [{"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "mine", MINE),
            *code_chat(aid, CODE_CHAT_A),
            ("type", "#code-text", "/"), ("wait", "!!document.querySelector('#slash-menu .slash-option')"),
            ("eval", "kinds", "[...document.querySelectorAll('#slash-menu .slash-option')].map(o => o.dataset.kind)"),
            ("press", "Escape"), ("eval", "clear", "(() => { const t = document.querySelector('#code-text'); t.value = '';"
                                 " t.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"),
            *pick("/Мой"), ("eval", "chip", f"{CHIP}.textContent"), ("click", "#code-send"),
            ("wait", f"document.querySelector('#code-feed .code-msg.user') && {IDLE}"),
            ("eval", "install", "!document.querySelector('#code-skills-install-item')")])
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert "project" not in values["kinds"] and "skill" in values["kinds"]
    assert values["chip"] == "Навык: Мой навык" and values["install"]
    assert [t["text"] for t in code_requests(machine, "turn")] == ["Текст навыка"]
    assert not code_requests(machine, "skills") and not code_requests(machine, "skill")
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_set_goes_into_the_project_once_and_review_opens_another_helper(browser, live, width):
    login = f"code-skills-set-{width}"
    live.add_user(login)
    messages = [{"id": "m0", "role": "user", "text": "Сделай", "steps": [], "time": 1},
                {"id": "m1", "role": "assistant", "text": "Сделал", "steps": [], "time": 2,
                 "progress": {"changes": {"files": [{"path": "src/app.py", "added": 2, "removed": 1}]}}}]
    machine = code_free_machine({CODE_CHAT_A: code_session("Работа", messages=messages)})
    machine.skills = machine.file_operations = machine.file_upload = True
    machine.codex = CATALOG
    machine.projects["alpha"]["files"] = {".agents/skills/spec/SKILL.md": "своё".encode()}
    aid, server = code_online(live, login, machine)
    menu = [*([("click", "#code-status-more")] if width == 320 else []), ("click", "#code-branch-actions")]
    states = "Object.fromEntries([...document.querySelectorAll('#code-skills-install li')].map(li => [li.dataset.key, li.dataset.state]))"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "document.querySelector('#code-chat').dataset.loaded === '1'"),
            *menu, ("click", "#code-skills-install-item"), ("wait", "!!document.querySelector('#code-skills-install')?.open"),
            ("eval", "folder", "document.querySelector('input[name=\"code-skills-folder\"]:checked').value"),
            ("click", "#code-skills-install-go"),
            ("wait", "document.querySelector('#code-skills-install-note').textContent === 'Готово'"),
            ("eval", "states", states), ("eval", "dialogFits", NO_OVERFLOW),
            ("press", "Escape"), ("wait", "!document.querySelector('#code-skills-install')?.open"),
            *menu, ("click", "[data-verify=\"codex\"]"),
            ("wait", "document.querySelector('#code-chat')?.dataset.engine === 'codex'"
                     f" && document.querySelector('#code-chat').dataset.loaded === '1' && !{CHIP}?.hidden"),
            ("eval", "verify", f"[{CHIP}.textContent, document.querySelector('#code-text').value]"),
            ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    files = machine.projects["alpha"]["files"]
    made = files[".agents/skills/acceptance/SKILL.md"]
    assert values["folder"] == ".agents/skills"
    assert values["states"]["spec"] == "exists" and values["states"]["acceptance"] == "written"
    assert set(values["states"].values()) == {"exists", "written"}
    assert files[".agents/skills/spec/SKILL.md"] == "своё".encode()               # data-state="exists": не перезаписан
    assert made.startswith(b"---\nname: acceptance\ndescription: ") and b"disable-model-invocation: true" in made
    assert not any("/parts/" in f or "/minutes/" in f for f in files)              # навыки «Чата» в проект не кладутся
    assert values["verify"][0] == "Навык: Приёмка"
    assert "src/app.py" in values["verify"][1]
    assert code_requests(machine, "turn") == []                                     # отправляет человек
    assert values["fits"] and values["dialogFits"]
    assert not known_refusals(problems(result), 409), problems(result)
