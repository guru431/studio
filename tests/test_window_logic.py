"""Вид окна — чистые функции `window` (Tk не поднимается)."""

import pytest

from studio_agent import theme, window


def test_view_of():
    assert window.view_of(False) == "login"
    assert window.view_of(True) == "link"


def test_status_words_and_colours():
    assert window.STATUS["online"] == ("На связи", "ok")
    assert window.STATUS["connecting"] == ("Подключается…", "muted")
    assert window.STATUS["offline"] == ("Нет связи — переподключаюсь", "warn")
    assert window.STATUS["revoked"] == ("Компьютер отозван в студии", "danger")
    assert window.STATUS["replaced"] == ("Ключ используется другим мостом", "danger")
    tokens = theme.palette(False).keys()
    assert all(token in tokens for _, token in window.STATUS.values())
    assert window.status_of("setup") == window.STATUS["connecting"]   # только что привязан — канал поднимается
    assert window.status_of("online") == ("На связи", "ok")


@pytest.mark.parametrize("status, words, buttons", [
    ("none", "PIN не задан", ("Задать PIN",)),
    ("set", "PIN задан", ("Изменить PIN", "Убрать PIN")),
    ("locked", "PIN заблокирован после 5 ошибок", ("Снять блок", "Изменить PIN")),
    (None, "", ()),
])
def test_pin_row(status, words, buttons):
    got_words, keys = window.pin_row(status)
    assert got_words == words and tuple(window.BUTTONS[k] for k in keys) == buttons


@pytest.mark.parametrize("pin, pin2, errors", [
    ("", "", ["PIN — от 4 до 8 цифр"]),
    ("12", "12", ["PIN — от 4 до 8 цифр"]),
    ("１２３４", "１２３４", ["PIN — от 4 до 8 цифр"]),
    ("1234", "1235", ["PIN не совпадает"]),
    ("2468", "2468", []),
])
def test_pin_dialog_errors(pin, pin2, errors):
    assert window.pin_dialog_errors(pin, pin2) == errors


def test_host_of():
    assert window.host_of("https://studio.example.com") == "studio.example.com"
    assert window.host_of("http://127.0.0.1:8151/") == "127.0.0.1"
    assert window.host_of("") == "—"


def test_texts_are_the_spec_words():
    assert window.TITLE == "Studio Agent" and window.NOT_LINKED == "Не подключён"
    assert window.NOTE_PASSWORD == "Пароль нужен один раз и на компьютере не хранится"
    assert window.CONFIRM_OFF == ("Отключить компьютер от студии? Студия забудет этот компьютер, идущие ходы "
                                  "прервутся. Чаты Claude Code останутся на компьютере.")
