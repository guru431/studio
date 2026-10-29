import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from studio_agent import enroll


class Studio:
    """Подставная студия: отвечает заданным статусом и телом, запоминает запросы."""

    def __init__(self, status=201, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests = []
        studio = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                studio.requests.append((self.path, dict(self.headers), self.rfile.read(size)))
                self._answer()

            def do_DELETE(self):
                size = int(self.headers.get("Content-Length", 0))
                studio.requests.append(("DELETE " + self.path, dict(self.headers), self.rfile.read(size)))
                self._answer()

            def _answer(self):
                data = json.dumps(studio.body).encode() if studio.body is not None else b""
                self.send_response(studio.status)
                for k, v in studio.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def studio():
    made = []

    def make(*args, **kw):
        made.append(Studio(*args, **kw))
        return made[-1]

    yield make
    for s in made:
        s.close()


def test_created_gives_id_key_and_login(studio):
    s = studio(201, {"agent_id": 7, "device_key": "k", "login": "owner"})
    assert enroll.enroll(s.url + "/", " Owner ", "пароль-123", "Рабочий") == (7, "k", "owner")
    path, headers, body = s.requests[0]
    assert path == "/api/agent/enroll"
    assert json.loads(body) == {"login": " Owner ", "password": "пароль-123", "name": "Рабочий"}
    assert "origin" not in {k.lower() for k in headers}
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("status, code, words", [
    (401, "login", "Неверный логин или пароль"),
    (429, "locked", "Вход на паузе после неудач — попробуйте позже"),
    (422, "name", "Имя компьютера — от 1 до 64 знаков"),
    (403, "unavailable", "Студия недоступна (HTTP 403)"),
    (413, "unavailable", "Студия недоступна (HTTP 413)"),
])
def test_refusals_have_words(studio, status, code, words):
    detail = {"code": {401: "login", 429: "locked", 422: "name"}.get(status, "origin"), "message": "…"}
    s = studio(status, {"detail": detail})
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll(s.url, "owner", "x", "Рабочий")
    assert (e.value.code, e.value.words) == (code, words)


def test_invalid_fields_are_not_a_name_error(studio):
    s = studio(422, {"detail": {"code": "invalid", "message": "неверные данные запроса"}})
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll(s.url, "owner", "x", "Рабочий")
    assert (e.value.code, e.value.words) == ("unavailable", "Студия недоступна (HTTP 422)")


def test_redirect_is_not_followed(studio):
    target = studio(201, {"agent_id": 7, "device_key": "k"})
    s = studio(307, {}, headers={"Location": target.url + "/api/agent/enroll"})
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll(s.url, "owner", "x", "Рабочий")
    assert e.value.words == "Студия недоступна (HTTP 307)"
    assert target.requests == []


def test_strange_answer_is_unavailable(studio):
    s = studio(201, {"agent_id": "7", "device_key": "k"})
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll(s.url, "owner", "x", "Рабочий")
    assert e.value.code == "unavailable"


@pytest.mark.parametrize("url", ["http://example.com", "ftp://h", "https://h/path", "https://u:p@h", ""])
def test_bad_address_is_refused_before_network(url):
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll(url, "owner", "x", "Рабочий")
    assert (e.value.code, e.value.words) == ("url", "Адрес студии — https://…")


def test_older_studio_without_login_gives_the_typed_one(studio):
    s = studio(201, {"agent_id": 7, "device_key": "k"})
    assert enroll.enroll(s.url, "  example-user ", "x", "Рабочий") == (7, "k", "example-user")


@pytest.mark.parametrize("status", [204, 401, 403, 405, 307])
def test_unenroll_sends_the_key_in_a_header_and_gives_the_status(studio, status):
    s = studio(status, {"detail": {"code": "key"}} if status == 401 else None)
    assert enroll.unenroll(s.url + "/", "секрет-не-в-адресе".encode().hex()) == status
    path, headers, body = s.requests[0]
    assert path == "DELETE /api/agent/enroll" and body == b""
    assert headers["Authorization"] == "Bearer " + "секрет-не-в-адресе".encode().hex()
    assert "origin" not in {k.lower() for k in headers}


def test_unenroll_without_studio_is_unavailable(monkeypatch):
    def refused(self, request, *a, **kw):  # отказ соединения на Windows ждёт ~2 с повторов — без сети
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refused)
    with pytest.raises(enroll.EnrollError) as e:
        enroll.unenroll("https://studio.example", "k")
    assert (e.value.code, e.value.words) == ("unavailable", "Студия недоступна (ConnectionRefusedError)")
