import logging
import uuid

import pytest

from studio_agent import __main__ as cli_main
from studio_agent import enroll, journal, setup, single, store
from studio_agent.pin import PinStore

PASSWORD = "пароль-студии-42"


@pytest.fixture
def form(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return {"url": "https://studio.example.com", "login": "owner", "password": PASSWORD, "name": "Рабочий",
            "projects_root": str(root), "pin": "", "pin2": "", "no_pin": False, "autostart": True}


def test_valid_form_has_no_errors(form):
    assert setup.validate(form, enrolled=False) == []


@pytest.mark.parametrize("change, words", [
    ({"projects_root": ""}, "Папки нет"),
    ({"projects_root": "c:/нет/такой/папки"}, "Папки нет"),
    ({"pin": "12", "pin2": "12"}, "PIN — от 4 до 8 цифр"),
    ({"pin": "１２３４", "pin2": "１２３４"}, "PIN — от 4 до 8 цифр"),
    ({"pin": "1234", "pin2": "1235"}, "PIN не совпадает"),
    ({"pin": "", "pin2": "1234"}, "PIN не совпадает"),
    ({"name": "я" * 65}, "Имя компьютера — от 1 до 64 знаков"),
    ({"name": "  "}, "Имя компьютера — от 1 до 64 знаков"),
    ({"name": "a\tb"}, "Имя компьютера — от 1 до 64 знаков"),
    ({"url": "http://studio.example.com"}, "Адрес студии — https://…"),
    ({"login": ""}, "Неверный логин или пароль"),
    ({"password": ""}, "Неверный логин или пароль"),
])
def test_validate_errors(form, change, words):
    assert setup.validate({**form, **change}, enrolled=False) == [words]


def test_enrolled_needs_no_login(form):
    assert setup.validate({**form, "login": "", "password": ""}, enrolled=True) == []
    assert setup.validate({**form, "password": ""}, enrolled=True) == ["Неверный логин или пароль"]


def test_no_pin_ignores_pin_fields(form):
    assert setup.validate({**form, "pin": "12", "pin2": "", "no_pin": True}, enrolled=False) == []


def test_apply_enrolls_and_keeps_no_password(home, form):
    calls = []

    def fake_enroll(url, login, password, name):
        calls.append((url, login, password, name))
        return 7, "k", "owner"

    cfg = setup.apply(home, {**form, "url": "https://studio.example.com/", "pin": "2468", "pin2": "2468",
                             "autostart": False}, enroll_fn=fake_enroll)
    assert calls == [("https://studio.example.com", "owner", PASSWORD, "Рабочий")]
    assert store.load_key(home) == "k"
    assert (cfg.agent_id, cfg.name, cfg.url, cfg.autostart, cfg.login) == (7, "Рабочий", "https://studio.example.com",
                                                                            False, "owner")
    assert store.load_config(home) == cfg
    assert PinStore(home).has_pin
    for f in home.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert PASSWORD.encode("utf-8") not in data and PASSWORD.encode("utf-16-le") not in data, f.name


def test_email_code_is_passed_to_enrollment_and_never_saved(home, form):
    supplied = []

    def fake_enroll(url, login, password, name, *, code):
        supplied.append(code)
        return 7, "device-key", "owner"

    setup.apply(home, {**form, "email_code": "123456"}, enroll_fn=fake_enroll)
    assert supplied == ["123456"]
    assert all(b"123456" not in path.read_bytes() for path in home.rglob("*") if path.is_file())


def test_apply_without_login_keeps_key_and_pin(home, form, tmp_path):
    setup.apply(home, {**form, "pin": "2468", "pin2": "2468"}, enroll_fn=lambda *a: (7, "k", "owner"))
    other = tmp_path / "other"
    other.mkdir()

    def no_enroll(*a):
        raise AssertionError("привязка без логина")

    cfg = setup.apply(home, {**form, "login": "", "password": "", "projects_root": str(other)}, enroll_fn=no_enroll)
    assert store.load_key(home) == "k"
    assert (cfg.agent_id, cfg.projects_root) == (7, str(other))
    assert PinStore(home).has_pin
    setup.apply(home, {**form, "login": "", "password": "", "no_pin": True}, enroll_fn=no_enroll)
    assert not PinStore(home).has_pin


def test_apply_refused_saves_nothing(home, form):
    def refused(*a):
        raise enroll.EnrollError("login", "Неверный логин или пароль")

    with pytest.raises(enroll.EnrollError):
        setup.apply(home, form, enroll_fn=refused)
    assert [f.name for f in home.iterdir() if f.is_file()] == []


def test_second_acquire_of_the_same_mutex_fails():
    name = f"Local\\StudioAgentTest-{uuid.uuid4().hex}"
    first = single.acquire(name)
    try:
        assert first is not None
        assert single.acquire(name) is None
    finally:
        first.close()
    again = single.acquire(name)
    assert again is not None
    again.close()


def test_journal_never_lets_websockets_debug(home):
    logging.getLogger("websockets").setLevel(logging.DEBUG)
    log = journal.setup(home, console=True)
    try:
        assert logging.getLogger("websockets").getEffectiveLevel() >= logging.INFO
        assert logging.getLogger("websockets.client").getEffectiveLevel() >= logging.INFO
    finally:
        logging.getLogger("websockets").setLevel(logging.NOTSET)
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)


def test_status_shows_no_key(home, form, capsys):
    setup.apply(home, form, enroll_fn=lambda *a: (7, "секретный-ключ-устройства", "owner"))
    assert cli_main.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "секретный-ключ-устройства" not in out
    assert "7" in out and "Рабочий" in out
