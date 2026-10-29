"""Привязка к студии: `POST <адрес>/api/agent/enroll {login, password, name}` → (id компьютера, ключ устройства,
логин); отвязка: `DELETE <адрес>/api/agent/enroll` с ключом заголовком.

Пароль уходит только в тело запроса и нигде не остаётся; перенаправлениям не следуем (тело с паролем и ключ —
только тому адресу, что ввёл владелец)."""

import json
import urllib.error
import urllib.request

from . import VERSION, link

PATH = "/api/agent/enroll"
LOGIN = "Неверный логин или пароль"
LOCKED = "Вход на паузе после неудач — попробуйте позже"
NAME = "Имя компьютера — от 1 до 64 знаков"
URL = "Адрес студии — https://…"
UNAVAILABLE = "Студия недоступна ({})"
KEPT = "Студия недоступна — компьютер остался в её списке: отзовите его в «Настройки» → «Компьютеры»"
FORGOTTEN = (204, 401)  # отвязка: студия забыла компьютер (401 — ключа у неё уже нет)


class EnrollError(Exception):
    """Отказ привязки: `code` (`login`, `locked`, `name`, `url`, `unavailable`) и слова для окна."""

    def __init__(self, code: str, words: str):
        super().__init__(words)
        self.code = code
        self.words = words


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 30x — ошибкой HTTPError


def base_url(url: str) -> str:
    """Адрес студии по правилу канала (`link.ws_url`: https, http — только петля), без `/` в конце."""
    url = (url or "").strip()
    try:
        link.ws_url(url)
    except ValueError:
        raise EnrollError("url", URL) from None
    return url.rstrip("/")


def _send(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """Запрос без перенаправлений → (статус, тело); ответ студии с кодом ошибки — `HTTPError` наружу, сеть —
    `EnrollError("unavailable")`."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as e:
        reason = e.reason
        raise EnrollError("unavailable", UNAVAILABLE.format(
            type(reason).__name__ if isinstance(reason, BaseException) else type(e).__name__)) from None
    except (OSError, ValueError) as e:  # срок, обрыв, неверный ответ HTTP
        raise EnrollError("unavailable", UNAVAILABLE.format(type(e).__name__)) from None


def enroll(url: str, login: str, password: str, name: str, *, timeout: float = 30) -> tuple[int, str, str]:
    """→ (id компьютера, ключ устройства, логин учётки); прежняя студия логина не отдаёт — введённый `strip().lower()`."""
    body = json.dumps({"login": login, "password": password, "name": name}).encode("utf-8")
    request = urllib.request.Request(base_url(url) + PATH, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"studio-agent/{VERSION}"})
    try:
        status, raw = _send(request, timeout)
    except urllib.error.HTTPError as e:
        raise _refusal(e) from None
    try:
        data = json.loads(raw)
        agent_id, key = data["agent_id"], data["device_key"]
        answered = data.get("login")
    except (ValueError, TypeError, KeyError, AttributeError):
        agent_id = key = answered = None
    if status != 201 or type(agent_id) is not int or not (isinstance(key, str) and key):
        raise EnrollError("unavailable", UNAVAILABLE.format("неверный ответ"))
    return agent_id, key, answered if isinstance(answered, str) and answered else login.strip().lower()


def unenroll(url: str, key: str, *, timeout: float = 15) -> int:
    """Отвязка мостом: `DELETE` с ключом заголовком, без тела и `Origin` → код HTTP (204 — отвязан, 401 — ключа у
    студии нет, прочее — студия компьютер не забыла); сеть и неверный адрес — `EnrollError`."""
    request = urllib.request.Request(base_url(url) + PATH, method="DELETE", headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": f"studio-agent/{VERSION}"})
    try:
        status, _ = _send(request, timeout)
    except urllib.error.HTTPError as e:
        e.close()
        return e.code
    return status


def _refusal(e: urllib.error.HTTPError) -> EnrollError:
    """Отказ студии `{"detail": {"code", "message"}}` → слова окна."""
    try:
        code = json.loads(e.read())["detail"]["code"]
    except Exception:
        code = None
    if e.code == 401:
        return EnrollError("login", LOGIN)
    if e.code == 429:
        return EnrollError("locked", LOCKED)
    if e.code == 422 and code == "name":
        return EnrollError("name", NAME)
    return EnrollError("unavailable", UNAVAILABLE.format(f"HTTP {e.code}"))
