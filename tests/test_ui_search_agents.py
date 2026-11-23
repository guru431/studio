"""Чип «Агенты» общего поиска: чаты всех проектов компьютеров на связи (мост 0.47.0, `chat_search_all`)."""
import pytest

from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, READY, browser, code_free_machine, code_loaded, code_machine, code_online,  # noqa: F401
                             code_session, go, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

READY_AGENTS = ("document.querySelector('#search-results')?.dataset.state === 'ready'"
                " && document.querySelectorAll('#search-results .search-agent').length === 2")


@pytest.mark.parametrize("width", [320, 1280])
def test_agents_chip_finds_chats_on_computers_and_opens_the_chat(browser, live, width, tmp_path):
    """Компьютер без PIN открывается сам и находит чат по слову из истории; компьютер с PIN — строка «Компьютер
    закрыт…»; фильтры модели и дат скрыты; нажатие на находку открывает чат в «Агентах»."""
    login = f"search-agents-{width}"
    live.add_user(login)
    free = code_free_machine({CODE_CHAT_A: code_session("Сборка релиза", messages=[
        {"role": "user", "text": "Почини сборку пингвина"}, {"role": "assistant", "text": "Готово"}])})
    locked = code_machine()
    free.chat_search_all = locked.chat_search_all = True
    one, sb1 = code_online(live, login, free)
    two, sb2 = code_online(live, login, locked, name="Закрытый ПК")
    groups = """[...document.querySelectorAll('#search-results .search-agent')].map((g) => [g.dataset.aid,
      g.querySelector('h2').textContent, g.querySelectorAll('.search-row').length,
      g.querySelector('.search-agent-state')?.textContent || ''])"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("search"),
            ("wait", "document.querySelector('#search-q')"), ("click", ".search-chips .chip[data-section=code]"),
            set_value("#search-q", "пингвин"), ("wait", READY_AGENTS),
            ("eval", "groups", groups),
            ("eval", "row", "document.querySelector('#search-results .search-row')?.textContent"),
            ("eval", "filters", "[document.querySelector('.search-filters').hidden,"
                                " document.querySelector('.search-periods').hidden]"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("screenshot", str(tmp_path / f"search-agents-{width}.png")),
            ("click", "#search-results .search-row"), code_loaded(CODE_CHAT_A),
            ("eval", "hash", "location.hash"),
            # «Назад» из чата — прежняя выдача без нового поиска; новое открытие поиска — тот же запрос заново
            ("eval", "back", "history.back(), true"), ("wait", READY_AGENTS),
            ("eval", "backHash", "location.hash"),
            *go("chat"), ("wait", "document.querySelector('#chat-input')"), *go("search"),
            ("wait", READY_AGENTS)], width=width, mobile=width < 400)
    finally:
        sb1.stop()
        sb2.stop()
    assert not result.get("failed"), result
    found = {int(row[0]): row[1:] for row in result["values"]["groups"]}
    assert found[one][:2] == ["Рабочий ПК", 1]
    assert found[two][:2] == ["Закрытый ПК", 0] and found[two][2].startswith("Компьютер закрыт")
    assert "Сборка релиза" in result["values"]["row"] and "alpha" in result["values"]["row"]
    assert result["values"]["filters"] == [True, True] and result["values"]["fits"]
    assert result["values"]["hash"] == f"#/code/{one}/{pid('alpha')}/{CODE_CHAT_A}"
    assert result["values"]["backHash"] == "#/search"
    assert result["sent"].count("POST /api/code/search") == 2, result["sent"]
    assert not any(row["op"] == "open" for row in locked.requests)   # компьютер с PIN сервер не открывает сам
    assert not problems(result), problems(result)
