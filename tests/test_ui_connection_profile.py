"""Профиль API-подключения на странице (connection-profile.js, спека 2026-11-21-studio-source-foundation-design §2):
своё подключение в «Настройках» и общее в панели администратора; текст общего профиля пользователю не виден."""
import pytest

from fakes import TOKEN as MODEL_KEY
from test_ui_browser import API, GATEWAY_URL, NO_OVERFLOW, READY, browser, go, is_open, live, problems, set_value, signin  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

DIALOG = "#conn-profile-dialog"
LOADED = f"{is_open(DIALOG)} && !document.querySelector('#profile-save').disabled"
SUGGESTION = "#profile-suggestions li:nth-child({0}) .profile-suggestion-{1}"


def card(scope: str, name: str) -> str:
    return f"[...document.querySelectorAll('{scope} .link')].find(e => e.querySelector('.link-name')?.textContent === {name!r})"


def open_profile(scope: str, name: str):
    """Меню «⋯» карточки подключения → «Профиль»; окно загрузило профиль."""
    return [("wait", f"{card(scope, name)}?.querySelector('.link-menu')"),
            ("eval", "menu", f"{card(scope, name)}.querySelector('.link-menu').click()"),
            ("eval", "profile", f"{card(scope, name)}.querySelector('.link-profile').click()"),
            ("wait", LOADED)]


def reload_page():
    return [("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}")]


def own_connection(name: str, cloud):
    """Своё подключение `name` к облачной заглушке с моделью «cloud-chat» в «Чате»."""
    return ("eval", "setup", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: {name!r}, kind: 'openai',
        base_url: {cloud.base_url!r}, token: {cloud.TOKEN!r}, sections: ['chat'] }});
      await m.api('PUT', '/api/sections/chat/links/' + c.id, {{ models: ['cloud-chat'] }});
      return c.id; }})()""")


def model_params_of(name: str) -> str:
    """`model_params` профиля своего подключения `name` — ответом сервера."""
    return f"""(async () => {{ const m = {API};
      const c = (await m.api('GET', '/api/connections')).items.find(c => c.name === {name!r});
      return (await m.api('GET', '/api/connections/' + c.id + '/profile')).model_params; }})()"""


#: значения окна: текст, параметры, подсказки по порядку, состояние top_k
VALUES = (f"""[document.querySelector('#profile-text').value,
  ['temperature', 'top-p', 'top-k', 'presence-penalty', 'max-tokens', 'max-images'].map(k => document.querySelector('#profile-' + k).value),
  [...document.querySelectorAll('#profile-suggestions li')].map(li => [li.querySelector('input').value,
    li.querySelector('textarea').value]),
  document.querySelector('#profile-top-k').disabled]""")


@pytest.mark.parametrize("width", [320, 1280])
def test_own_profile_is_saved_and_survives_reload(browser, live, width):
    login, name = f"profile-own-{width}", f"Своё {width}"
    live.add_user(login)
    steps = [*signin(login), ("wait", READY), own_connection(name, live.cloud), *go("settings/chat"), *open_profile("", name),
        ("type", "#profile-text", "Отвечай кратко"),
        ("eval", "count", "document.querySelector('#profile-text-count').textContent"),
        ("eval", "hint", "document.querySelector('[data-hint=top_k]').textContent"),
        set_value("#profile-temperature", "0.4"), set_value("#profile-top-p", "0.9"),
        set_value("#profile-top-k", "40"), set_value("#profile-presence-penalty", "-0.5"),
        set_value("#profile-max-tokens", "700"), set_value("#profile-max-images", "6"),
        # вне предела — слова под полем, профиль не уходит
        set_value("#profile-top-p", "0"), ("click", "#profile-save"),
        ("eval", "outOfRange", f"[document.querySelector('{DIALOG} .error[data-for=top_p]').textContent, {is_open(DIALOG)}]"),
        set_value("#profile-top-p", "0.9"),
        ("click", "#profile-suggestion-add"), ("click", "#profile-suggestion-add"),
        set_value(SUGGESTION.format(1, "title"), "Сводка"), set_value(SUGGESTION.format(1, "text"), "Сделай сводку"),
        set_value(SUGGESTION.format(2, "title"), "План"), set_value(SUGGESTION.format(2, "text"), "Составь план"),
        ("eval", "up", f"document.querySelector('{SUGGESTION.format(2, 'tools')} .profile-up').click()"),
        ("eval", "dialogFits", f"document.querySelector('{DIALOG}').scrollWidth <= innerWidth && {NO_OVERFLOW}"),
        ("click", "#profile-save"),
        ("wait", f"!{is_open(DIALOG)} && {card('', name)}?.querySelector('.link-profile-set')"),
        *reload_page(), *go("settings/chat"), *open_profile("", name),
        ("eval", "saved", VALUES),
        ("eval", "server", f"""(async () => {{ const m = {API};
          const c = (await m.api('GET', '/api/connections')).items.find(c => c.name === {name!r});
          return (await m.api('GET', '/api/connections/' + c.id + '/profile')).params; }})()""")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["count"] == "14 из 8000 знаков"
    assert values["hint"].endswith("только vLLM и совместимые")
    assert values["outOfRange"] == ["Больше 0 и не больше 1", True]
    assert values["dialogFits"] is True
    assert values["saved"] == ["Отвечай кратко", ["0.4", "0.9", "40", "-0.5", "700", "6"],
                               [["План", "Составь план"], ["Сводка", "Сделай сводку"]], False]
    assert values["server"] == {"temperature": 0.4, "top_p": 0.9, "top_k": 40, "presence_penalty": -0.5, "max_tokens": 700,
                                "max_images": 6}
    assert not problems(result), result.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_edits_shared_profile_and_user_sees_no_text(browser, live, width):
    login, name = f"profile-user-{width}", f"Общее {width}"
    secret = f"Тайный текст профиля {width}"
    live.add_user(login)
    dialog = "#shared-models-dialog"
    setup = ("eval", "setup", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/admin/shared/connections', {{ name: {name!r}, kind: 'openai',
        base_url: {GATEWAY_URL + '/v1'!r}, token: {MODEL_KEY!r}, sections: ['chat'] }});
      await m.api('PUT', '/api/admin/shared/sections/chat/links/' + c.id, {{ models: ['cloud-chat'] }});
      return c.id; }})()""")
    steps = [*signin(), ("wait", READY), setup, *go("admin"), ("wait", "document.querySelector('#admin-shared-models')"),
        ("click", "#admin-shared-models"), *open_profile(dialog, name),
        ("type", "#profile-text", secret), set_value("#profile-temperature", "0.2"),
        ("click", "#profile-suggestion-add"),
        set_value(SUGGESTION.format(1, "title"), "Перевод"), set_value(SUGGESTION.format(1, "text"), "Переведи на английский"),
        ("eval", "dialogFits", f"document.querySelector('{DIALOG}').scrollWidth <= innerWidth"),
        ("click", "#profile-save"), ("wait", f"!{is_open(DIALOG)}"),
        *open_profile(dialog, name), ("eval", "saved", VALUES),
        ("click", f"{DIALOG} .dialog-close"), ("wait", f"!{is_open(DIALOG)}"),
        ("click", f"{dialog} .dialog-close"), ("wait", f"!{is_open(dialog)}"),
        # обычная учётка: подсказки видны, текста и параметров нет, кнопки «Профиль» у общего нет
        ("click", "#avatar"), ("click", "#menu-logout"), ("wait", "location.pathname === '/login'"),
        *signin(login), ("wait", READY), *go("settings/chat"), ("wait", card("", name)),
        ("eval", "userCard", f"[!!{card('', name)}.querySelector('.link-profile'), document.body.innerText.includes({secret!r})]"),
        ("eval", "userApi", f"""(async () => {{ const m = {API};
          const c = (await m.api('GET', '/api/connections')).items.find(c => c.name === {name!r});
          let status = 0;
          try {{ await m.api('GET', '/api/admin/shared/connections/' + c.id + '/profile'); }} catch (e) {{ status = e.status; }}
          return [c.suggestions, 'system_text' in c, 'params' in c, JSON.stringify(c).includes({secret!r}), status]; }})()""")]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["dialogFits"] is True
    assert values["saved"] == [secret, ["0.2", "", "", "", "", ""], [["Перевод", "Переведи на английский"]], False]
    assert values["userCard"] == [False, False]
    suggestions, has_text, has_params, leaked, status = values["userApi"]
    assert suggestions == [{"title": "Перевод", "text": "Переведи на английский"}]
    assert not has_text and not has_params and not leaked and status in (403, 404)
    assert not [line for line in problems(result) if "/profile" not in str(line)], result.get("log")


MODEL = "#profile-models .profile-model:nth-child(1)"


@pytest.mark.parametrize("width", [320, 1280])
def test_model_parameters_are_edited_and_survive_reload(browser, live, width):
    """Окно контекста подключения и параметры модели (п. 31(б), 35 спеки 2026-11-25): ручное окно вне предела —
    слова под полем блока, профиль не уходит; сохранённое переживает перезагрузку; убранный блок уходит с сервера."""
    login, name = f"profile-model-{width}", f"Модели {width}"
    live.add_user(login)
    steps = [*signin(login), ("wait", READY), own_connection(name, live.cloud), *go("settings/chat"),
        *open_profile("", name),
        set_value("#profile-context-window", "32768"),
        set_value("#profile-model-name", "cloud-chat"), ("click", "#profile-model-add"),
        ("wait", f"document.querySelector('{MODEL}')"),
        set_value(f"{MODEL} [data-param=temperature]", "0.9"),
        set_value(f"{MODEL} [data-param=context_window]", "100"),
        ("click", "#profile-save"),
        ("eval", "outOfRange", f"[document.querySelector('{MODEL} .error[data-for$=context_window]').textContent,"
                               f" {is_open(DIALOG)}]"),
        set_value(f"{MODEL} [data-param=context_window]", "16384"),
        # повтор имени — тот же блок, второго нет
        set_value("#profile-model-name", "cloud-chat"), ("click", "#profile-model-add"),
        ("eval", "blocks", "document.querySelectorAll('#profile-models .profile-model').length"),
        ("eval", "dialogFits", f"document.querySelector('{DIALOG}').scrollWidth <= innerWidth && {NO_OVERFLOW}"),
        ("click", "#profile-save"),
        ("wait", f"!{is_open(DIALOG)} && {card('', name)}?.querySelector('.link-profile-set')"),
        *reload_page(), *go("settings/chat"), *open_profile("", name),
        ("eval", "saved", """[document.querySelector('#profile-context-window').value,
          [...document.querySelectorAll('#profile-models .profile-model')].map(b => [b.dataset.model,
            b.querySelector('[data-param=temperature]').value, b.querySelector('[data-param=context_window]').value])]"""),
        ("eval", "server", model_params_of(name)),
        ("click", f"{MODEL} .profile-model-remove"), ("click", "#profile-save"), ("wait", f"!{is_open(DIALOG)}"),
        ("eval", "removed", model_params_of(name))]
    result = browser.drive(live.url + "/", steps, width=width, mobile=width == 320)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["outOfRange"] == ["Целое от 8192 до 4000000", True]      # слова под полем, профиль не ушёл
    assert values["blocks"] == 1
    assert values["dialogFits"] is True
    assert values["saved"] == ["32768", [["cloud-chat", "0.9", "16384"]]]
    assert values["server"] == {"cloud-chat": {"temperature": 0.9, "context_window": 16384}} and values["removed"] == {}
    assert not problems(result), result.get("log")
