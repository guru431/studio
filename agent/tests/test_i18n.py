"""Язык моста: выбор по системе и `STUDIO_AGENT_LANG`, словарь `i18n.EN` покрывает всё, что видит человек."""

import ast
import re
from pathlib import Path

import pytest

from studio_agent import i18n

PACKAGE = Path(i18n.__file__).parent
# Окно, трей, меню и консоль, их слова в `app`, `enroll`, `setup`; отказы `Refused` (их переводит страница студии) и
# журнал (`log.*`) — не сюда.
SHOWN = ("window.py", "tray.py", "linux_menu.py", "__main__.py", "app.py", "enroll.py", "setup.py")
CYRILLIC = re.compile("[А-Яа-яЁё]")


def shown_literals(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body and \
                isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            skip.add(id(body[0].value))  # докстринг
        if isinstance(node, ast.Call):
            func = node.func
            journal = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "log"
            refused = isinstance(func, ast.Name) and func.id == "Refused"
            if journal or refused:
                skip.update(id(n) for arg in [*node.args, *(k.value for k in node.keywords)] for n in ast.walk(arg))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skip and CYRILLIC.search(node.value)]


@pytest.fixture
def language(monkeypatch):
    """Язык процесса на время теста; после — снова тот, что задал conftest."""
    def choose(value):
        monkeypatch.setenv("STUDIO_AGENT_LANG", value)
        i18n.language.cache_clear()
    yield choose
    i18n.language.cache_clear()


@pytest.mark.parametrize("name", SHOWN)
def test_every_shown_russian_literal_has_an_english_translation(name):
    """Сторож словаря: новая подпись окна, трея или меню без перевода — отказ; f-строки с русским текстом сюда
    тоже попадают кусками — их пишут шаблоном `tr("… {0}", значение)`."""
    missing = [text for text in shown_literals(PACKAGE / name) if text not in i18n.EN]
    assert not missing, missing


def test_catalog_values_are_english_and_keep_parameters():
    tree = ast.parse((PACKAGE / "i18n.py").read_text(encoding="utf-8"))
    [catalog] = [n.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "EN"]
    keys = [ast.literal_eval(k) for k in catalog.keys]
    assert len(keys) == len(set(keys)), "повтор ключа словаря"
    for source, english in i18n.EN.items():
        assert english and not CYRILLIC.search(english), source
        assert sorted(re.findall(r"\{\d+\}", source)) == sorted(re.findall(r"\{\d+\}", english)), source


def test_language_follows_override_then_system(language, monkeypatch):
    language("en")
    assert i18n.tr("Сохранить") == "Save"
    assert i18n.tr("Заданий: {0} · ждут ответа: {1}", 3, "{1}") == "Tasks: 3 · awaiting reply: {1}"
    assert i18n.tr("Не из словаря {0}", "x") == "Не из словаря x"
    language("ru")
    assert i18n.tr("Сохранить") == "Сохранить"
    for system, expected in (("ru_RU.UTF-8", "ru"), ("ru", "ru"), ("en_US.UTF-8", "en"), ("C", "en"), ("", "en"),
                             ("de-DE", "en")):
        monkeypatch.setattr(i18n, "_system", lambda: system)
        language("")
        assert i18n.language() == expected, system


def test_posix_locale_variables_decide_before_locale_module(monkeypatch):
    monkeypatch.setattr(i18n.os, "name", "posix")
    monkeypatch.setattr(i18n.host, "MACOS", False)
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    assert i18n._system() == "ru_RU.UTF-8"


def test_english_tray_window_and_errors(language):
    from studio_agent import enroll, setup, tray, window

    language("en")
    assert tray.title("online", "ivanov") == "Studio Agent — online · ivanov"
    assert tray.status_line("offline") == "● Offline"
    assert tray.notice("revoked", False) == "Computer revoked in Studio"
    assert window.card_of("replaced")[0] == "Key is used by another bridge"
    assert window.pin_row("locked")[0] == "PIN locked after 5 failed attempts"
    assert window.backend_row({"status": "ready", "models": 4}) == ("Available · models: 4", "ok")
    assert window.memory_row({"available": 1.5 * 1024 ** 3, "total": 8 * 1024 ** 3}) == "1.5 of 8.0 GB available"
    assert setup.pin_errors("12", "12") == ["PIN must be 4 to 8 digits"]
    with pytest.raises(enroll.EnrollError) as refused:
        enroll.base_url("ftp://x")
    assert refused.value.words == "Studio address must be https://…"
