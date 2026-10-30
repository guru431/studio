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


@pytest.mark.parametrize("state, card", [
    ("online", ("На связи", "ok", "", "Отключиться")),
    ("offline", ("Нет связи — переподключаюсь", "warn", "", "Отключиться")),
    ("setup", ("Подключается…", "muted", "", "Отключиться")),
    ("revoked", ("Компьютер отозван в студии", "danger", "", "Войти заново")),
    ("replaced", ("Ключ используется другим мостом", "danger",
                  "Подключить снова — этот компьютер заберёт канал у другого моста", "Подключить снова")),
])
def test_card_of(state, card):
    words, token, hint, kind = window.card_of(state)
    assert (words, token, hint, window.MAIN_BUTTONS[kind][0]) == card


def test_replaced_does_not_erase_the_key():
    """Заменённый мост ключ не стирает (второй мост той же учётки Windows — с тем же key.bin и pin.bin): канал
    заново; стирание без запроса к студии — только у отозванного."""
    assert window.card_of("replaced")[3] == "reconnect"
    assert [s for s in window.STATUS if window.card_of(s)[3] == "relogin"] == ["revoked"]


@pytest.mark.parametrize("view, state, busy, action", [
    ("link", "online", True, "wait"),             # отвязка или подтверждение — не перестраивать
    ("login", "online", True, "wait"),
    ("link", "offline", False, "card"),           # тот же вид — карточка на месте
    ("link", "replaced", False, "card"),
    ("link", "setup", False, "render"),           # отвязан — вид «Вход»
    ("login", "connecting", False, "render"),
    ("login", "setup", False, "none"),            # форму входа с введённым не трогать
])
def test_state_action(view, state, busy, action):
    assert window.state_action(view, state, busy) == action


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


def test_backend_rows_show_their_own_probe_result():
    assert window.backend_row(None) == ("Проверка…", "muted")
    assert window.backend_row({"status": "checking", "models": 0}) == ("Проверка…", "muted")
    assert window.backend_row({"status": "ready", "models": 12}) == ("Доступен · моделей: 12", "ok")
    # Сохранённый каталог не делает последнюю неудачную проверку успешной.
    assert window.backend_row({"status": "unavailable", "models": 12}) == ("Недоступен", "warn")


def test_texts_are_the_spec_words():
    assert window.TITLE == "Studio Agent" and window.NOT_LINKED == "Не подключён"
    assert window.NOTE_PASSWORD == "Пароль нужен один раз и на компьютере не хранится"
    assert window.CONFIRM_OFF == ("Отключить компьютер от студии? Студия забудет этот компьютер, идущие ходы "
                                  "прервутся. Чаты останутся на компьютере.")
