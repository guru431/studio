"""Профиль прямого подключения (спека 2026-11-21-studio-source-foundation-design §2, §7a п. 6): страница
подмешивает параметры профиля из ответа хода в запрос к сервису, а отказ 400 с именем отправленного параметра
показывает словами `profiles.REFUSED`, а не общим «Сервис ответил ошибкой»."""
import pytest

import profiles
from test_ui_browser import (API, LAST_ANSWER, READY, browser, go, known_refusals, live, open_new_chat,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


def cloud_with_profile(cloud, name):
    """Облако в «Чате» с отмеченной cloud-chat и профилем с `temperature`."""
    return ("eval", "cloud", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: {name!r}, kind: 'openai',
        base_url: {cloud.base_url!r}, token: {cloud.TOKEN!r}, sections: ['chat'] }});
      await m.api('PUT', '/api/sections/chat/links/' + c.id, {{ models: ['cloud-chat'] }});
      await m.api('PUT', '/api/connections/' + c.id + '/profile', {{ params: {{ temperature: 0.3 }} }});
      await m.loadMe(); return c.id; }})()""")


@pytest.mark.parametrize("width", [320, 1280])
def test_direct_turn_refused_param_is_named_in_words(browser, live, width):
    login = f"refused-{width}"
    live.add_user(login)
    mobile = width == 320
    meta = f"{LAST_ANSWER}?.querySelector('.msg-meta.error')?.textContent || ''"
    live.cloud.chat_error = (400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported with"
                                                       " this model.", "type": "invalid_request_error"}})
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), cloud_with_profile(live.cloud, "Облако"),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("click", "#model-chip"), ("wait", "document.querySelector('.model-option[data-model=\"cloud-chat\"]')"),
            ("click", ".model-option[data-model=\"cloud-chat\"]"),
            ("type", "#chat-input", "Ты здесь?"), ("press", "Enter"),
            ("wait", f"({meta}).includes('temperature')"),
            ("eval", "words", meta),
            ("eval", "saved", f"""(async () => {{ const m = {API}; const id = location.hash.split('/')[2];
              const last = (await m.api('GET', '/api/chats/' + id)).messages.at(-1);
              return [last.status, last.error]; }})()""")],
            width=width, mobile=mobile)
    finally:
        live.cloud.chat_error = None
    assert not r.get("failed"), r
    words = profiles.REFUSED.format("temperature")
    words = words[0].upper() + words[1:]                           # слова ошибки страница пишет с заглавной
    assert r["values"]["words"] == words, r["values"]
    assert r["values"]["saved"] == ["error", words]
    assert live.cloud.chat_requests[-1]["temperature"] == 0.3
    assert live.cloud.chat_requests[-1]["messages"][-1] == {"role": "user", "content": "Ты здесь?"}
    assert not known_refusals(problems(r), 400), problems(r)
