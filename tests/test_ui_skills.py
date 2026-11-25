"""«Настройки → Навыки» (skills.js, спека 2026-11-25-studio-skills-status-local-models, п. 1, 5): посев языком страницы,
создать, изменить, порядок, удалить, «Добавить навыки по умолчанию»; блока «Быстрые действия» в «Агентах» больше нет;
умолчания навыков в панели администратора. 320 и 1280 px, ещё 320 px на английском."""
import json

import pytest

import skills
from test_ui_browser import API, NO_OVERFLOW, READY, browser, go, is_open, live, problems, set_value, signin  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

NAMES = "[...document.querySelectorAll('#skills-settings .skill-item-name')].map(e => e.textContent)"
ENGLISH = "localStorage.setItem('studio-language', 'en')"


def item(scope: str, name: str) -> str:
    return (f"[...document.querySelectorAll('{scope} .skill-item')]"
            f".find(e => e.querySelector('.skill-item-name').textContent === {name!r})")


def press(scope: str, name: str, control: str) -> tuple:
    return ("eval", "press", f"{item(scope, name)}.querySelector('{control}').click()")


@pytest.mark.parametrize("width, lang", [(320, "ru"), (1280, "ru"), (320, "en")])
def test_skills_page_seeds_creates_edits_orders_and_restores(browser, live, width, lang):
    login = f"skills-{width}-{lang}"
    live.add_user(login)
    builtin = skills.BUILTIN[lang]
    first, acceptance = builtin[0], builtin[3].name
    text = "  Мой навык: проверь всё.\n\nБез сокращений.  "
    steps = [*signin(login), ("wait", READY), *go("settings/skills"),
        ("wait", f"{NAMES}.length === 9"), ("eval", "seeded", NAMES),
        ("eval", "nav", "document.querySelector('.settings-nav a[data-section=skills]').textContent"),
        # создать: «Агенты», кнопкой в пустом чате
        ("click", "#skills-settings-add"), ("wait", is_open("#skill-dialog")),
        set_value("#skill-name", "Мой навык"), set_value("#skill-description", "Одна фраза"),
        set_value("#skill-text", text), set_value("#skill-scope", "agents"),
        ("eval", "partsOff", "document.querySelector('#skill-parts').disabled"),
        ("click", "#skill-quick"), ("click", "#skill-save"),
        ("wait", f"!{is_open('#skill-dialog')} && {NAMES}.length === 10"),
        ("eval", "created", f"""(async () => {{ const m = {API};
          const id = document.querySelector('#skills-settings .skill-item:last-child').dataset.skill;
          return await m.api('GET', '/api/skills/' + id); }})()"""),
        # изменить первый: окно читает полный текст по id
        press("#skills-settings", first.name, ".skill-edit"), ("wait", is_open("#skill-dialog")),
        ("eval", "fullText", "document.querySelector('#skill-text').value"),
        set_value("#skill-name", "Переименованный"), ("click", "#skill-save"),
        ("wait", f"!{is_open('#skill-dialog')} && {NAMES}[0] === 'Переименованный'"),
        # выше у последнего — он девятый, и после перезагрузки тоже
        ("eval", "up", "document.querySelector('#skills-settings .skill-item:last-child .skill-up').click()"),
        ("wait", f"{NAMES}[8] === 'Мой навык'"),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"
                 f" && {NAMES}.length === 10"),
        ("eval", "afterReload", NAMES),
        # удалить «Приёмку» с подтверждением, вернуть её умолчаниями
        press("#skills-settings", acceptance, ".skill-delete"), ("wait", is_open("#skill-remove")),
        ("click", "#skill-remove-confirm"), ("wait", f"{NAMES}.length === 9"),
        ("eval", "deleted", NAMES),
        ("click", "#skill-defaults"), ("wait", f"{NAMES}.length === 10"),
        ("eval", "restored", NAMES), ("eval", "fits", NO_OVERFLOW),
        # кнопки пустого чата «Агентов» читают профиль, который страница обновила после записи
        ("eval", "quick", f"(async () => (await {API}.api('GET', '/api/me')).code_quick_actions.map(a => a.title))()"),
        # «Настройки → Агенты»: прежнего редактора быстрых действий нет
        *go("settings/code"), ("wait", "document.querySelector('.section-panel[data-section=code]')"),
        ("eval", "noQuickEditor", "!document.querySelector('.code-quick-actions-settings, #code-quick-action-rows')")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320,
                           **({"init": ENGLISH} if lang == "en" else {}))
    assert not result.get("failed"), result
    values = result["values"]
    names = [b.name for b in builtin]
    assert values["seeded"] == names
    assert values["nav"] == ("Skills" if lang == "en" else "Навыки")
    assert values["partsOff"] is True
    created = values["created"]
    assert created["quick"] is True and created["parts"] is False and created["scope"] == "agents"
    assert created["text"] == text and created["description"] == "Одна фраза" and created["origin"] is None
    assert values["fullText"] == first.text
    assert values["afterReload"] == ["Переименованный", *names[1:8], "Мой навык", names[8]]
    assert acceptance not in values["deleted"] and len(values["deleted"]) == 9
    assert values["restored"][-1] == acceptance and values["restored"][:-1] == values["deleted"]
    assert values["fits"] is True
    assert values["quick"] == [b.name for b in builtin if b.quick] + ["Мой навык"]
    assert values["noQuickEditor"] is True
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_edits_a_default_skill(browser, live, width):
    text = f"Сверь сделанное с критериями, вариант {width}."
    steps = [*signin(), ("wait", READY), *go("admin"),
        ("wait", item("#admin-skill-defaults", "Подготовить ТЗ")),
        set_value("#skill-default-lang", "en"), ("wait", item("#admin-skill-defaults", "Prepare the spec")),
        ("eval", "english", "[...document.querySelectorAll('#admin-skill-defaults .skill-item-name')].map(e => e.textContent)"),
        press("#admin-skill-defaults", "Acceptance", ".skill-edit"), ("wait", is_open("#skill-dialog")),
        ("eval", "fullText", "document.querySelector('#skill-text').value"),
        set_value("#skill-text", text), ("click", "#skill-save"), ("wait", f"!{is_open('#skill-dialog')}"),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "server", f"""(async () => {{ const m = {API};
          const en = (await m.api('GET', '/api/admin/skills?lang=en')).items;
          return en.find(s => s.builtin === 'acceptance'); }})()""")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["english"] == [b.name for b in skills.BUILTIN["en"]]
    # прогоны идут по порядку параметров на одной службе: 1280 видит правку прогона 320
    assert values["fullText"] == (skills.BUILTIN["en"][3].text if width == 320 else "Сверь сделанное с критериями, вариант 320.")
    assert values["server"]["text"] == text and values["server"]["name"] == "Acceptance", json.dumps(values["server"])
    assert values["fits"] is True
    assert not problems(result), result.get("log")
