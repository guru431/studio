"""Настройка моста (окно и консоль): проверка полей и запись — привязка, ключ, `config.json`, PIN.

Поля формы: `url`, `login`, `password`, `name`, `projects_root`, `pin`, `pin2`, `no_pin`, `autostart`.
Пароль студии уходит только в привязку и нигде не сохраняется."""

import unicodedata
from pathlib import Path

from . import enroll, store
from .pin import PIN_RE, PinStore

NAME_MAX = 64
NO_FOLDER = "Папки нет"
PIN_WORDS = "PIN — от 4 до 8 цифр"
PIN_MISMATCH = "PIN не совпадает"


def _text(form: dict, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def name_ok(name: str) -> bool:
    """Как студия: после `strip` 1–64 знака без управляющих."""
    name = name.strip()
    return 1 <= len(name) <= NAME_MAX and not any(unicodedata.category(c).startswith("C") for c in name)


def validate(form: dict, *, enrolled: bool) -> list[str]:
    """Ошибки формы словами окна; пусто — можно `apply`. Привязан — логин и пароль оба или ни одного."""
    errors = []
    try:
        enroll.base_url(_text(form, "url"))
    except enroll.EnrollError as e:
        errors.append(e.words)
    login, password = _text(form, "login").strip(), _text(form, "password")
    if (login or password or not enrolled) and not (login and password):
        errors.append(enroll.LOGIN)
    if not name_ok(_text(form, "name")):
        errors.append(enroll.NAME)
    root = _text(form, "projects_root").strip()
    if not (root and Path(root).is_dir()):
        errors.append(NO_FOLDER)
    if not form.get("no_pin"):
        errors += pin_errors(_text(form, "pin"), _text(form, "pin2"))
    return errors


def pin_errors(pin: str, pin2: str) -> list[str]:
    """Пусто (не трогать) или 4–8 цифр ASCII; повтор совпадает."""
    if pin and not (pin.isascii() and PIN_RE.fullmatch(pin)):
        return [PIN_WORDS]
    if pin != pin2:
        return [PIN_MISMATCH]
    return []


def apply(h: Path, form: dict, *, enroll_fn=enroll.enroll) -> store.Config:
    """Форма прошла `validate`: с логином — привязка (сбой — `EnrollError`, ничего не записано), ключ, id и имя;
    всегда — адрес, папка, автозапуск; PIN — убрать, задать или (пусто) не трогать."""
    cfg = store.load_config(h)
    url = enroll.base_url(_text(form, "url"))
    login = _text(form, "login").strip()
    if login:
        name = _text(form, "name").strip()
        agent_id, key = enroll_fn(url, login, _text(form, "password"), name)
        store.save_key(h, key)
        cfg.agent_id, cfg.name = agent_id, name
    cfg.url = url
    cfg.projects_root = _text(form, "projects_root").strip()
    cfg.autostart = bool(form.get("autostart"))
    store.save_config(h, cfg)
    pins = PinStore(h)
    if form.get("no_pin"):
        pins.set_pin(None)
    elif _text(form, "pin"):
        pins.set_pin(_text(form, "pin"))
    return cfg
