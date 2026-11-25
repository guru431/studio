"""Кнопки под ответом «Агентов» (code-answer-tools.js) в настоящем браузере: «Копировать» — весь текст ответа
разметкой, модель — когда помощник её сообщает; «Прочитать» нет (текст агентского чата в базу не пишется)."""
import pytest

from test_ui_browser import (CODE_CHAT_A, CODE_IDLE_AFTER, COPY_SPY, NO_OVERFLOW, READY, browser, code_chat,
                             code_free_machine, code_online, code_send, code_session, live, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

MODELS = "[...document.querySelectorAll('#code-feed .code-msg.assistant .msg-model')].map((n) => n.textContent)"


def copy_answer(index):
    """«Копировать» ответа номер index (с конца, если отрицательный): что легло в буфер и надпись кнопки."""
    return ("(async () => { window.copied = null; const all = [...document.querySelectorAll('#code-feed .code-msg.assistant')];"
            " const button = all.at(%d).querySelector('.msg-copy'); button.click();"
            " for (let i = 0; i < 50 && button.textContent === 'Копировать'; i += 1) await new Promise(r => setTimeout(r, 20));"
            " return [window.copied, button.textContent]; })()" % index)


@pytest.mark.parametrize("width", [320, 1280])
def test_copy_and_model_under_the_answer(browser, live, width):
    from fake_bridge import T0
    login = f"answer-{width}"
    live.add_user(login)
    history = [{"id": "m-0", "role": "user", "text": "Сделай", "steps": [], "time": T0},
               {"id": "m-1", "role": "assistant", "text": "**Готово**", "model": "claude-opus-5-5", "steps": [], "time": T0 + 1},
               {"id": "m-2", "role": "user", "text": "А ещё?", "steps": [], "time": T0 + 2},
               {"id": "m-3", "role": "assistant", "text": "Без модели", "steps": [], "time": T0 + 3}]
    machine = code_free_machine({CODE_CHAT_A: code_session("Ответы", messages=history)})
    machine.script = [{"k": "settings", "model": "sonnet"}, {"k": "text", "text": "Сделано"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "models", MODELS), ("eval", "spy", COPY_SPY), ("eval", "first", copy_answer(0)),
            *code_send("Дальше"),
            ("wait", CODE_IDLE_AFTER.format(n=3) + " && document.querySelector('#code-feed .code-msg.assistant:last-child .msg-copy')"),
            ("eval", "live", MODELS), ("eval", "last", copy_answer(-1)),
            ("eval", "read", "document.querySelectorAll('#code-feed .msg-read').length"),
            ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert values["models"] == ["claude-opus-5-5"], values               # у m-3 модели нет — и строки нет
    assert values["first"] == ["**Готово**", "Скопировано"], values
    assert values["live"] == ["claude-opus-5-5", "sonnet"], values
    assert values["last"] == ["Сделано", "Скопировано"], values
    assert values["read"] == 0 and values["fits"], values
    assert not [p for p in r["sent"] if "/api/speeches" in p]
    assert not problems(r), problems(r)
