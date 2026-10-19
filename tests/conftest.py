"""Общее для тестов студии: пути проекта, окружение службы, приложение main.app."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
CLIENT = ROOT / "client"
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(APP))             # image_upload — рядом с server.py


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Все переменные config.REQUIRED: свежий ключ, база и медиа во временном каталоге."""
    from cryptography.fernet import Fernet
    db_path, media_dir = tmp_path / "studio.db", tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setenv("STUDIO_DATA_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("STUDIO_DB", str(db_path))
    monkeypatch.setenv("STUDIO_MEDIA_DIR", str(media_dir))
    monkeypatch.setenv("STUDIO_ORIGIN", "https://testserver")
    return SimpleNamespace(db_path=db_path, media_dir=media_dir)


T0 = 1_800_000_000.0


@pytest.fixture
def studio(env, monkeypatch):
    """main.app на свежей базе: часы — список `clock`, пауза двигает их и пишется в `sleeps`,
    шлюз — FakeGateway через httpx.MockTransport."""
    import httpx
    from fastapi.testclient import TestClient

    import auth
    import chat
    import clock
    import images
    import main
    import security
    import state
    from fakes import FakeGateway

    # scrypt N=16384 — ~0.1 с на хеш; здесь дешёвый (настоящие параметры сверяет test_foundation)
    monkeypatch.setattr(security, "_N", 16)
    monkeypatch.setattr(security, "DUMMY_HASH", security.hash_password("dummy"))
    gw = FakeGateway()
    now, sleeps = [T0], []
    monkeypatch.setattr(clock, "NOW", lambda: now[0])

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds                # пауза двигает подменённые часы: сроки честные
        await asyncio.sleep(0.001)

    monkeypatch.setattr(clock, "SLEEP", fast_sleep)
    monkeypatch.setattr(state, "make_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(gw.handler)))
    monkeypatch.setattr(state, "WATCHER", False)
    auth.FAILS.clear()
    images.JOBS.clear()
    chat.STREAMS.clear()
    with TestClient(main.app, base_url="https://testserver") as client:
        yield SimpleNamespace(c=client, clock=now, sleeps=sleeps, gw=gw)
    auth.FAILS.clear()
    images.JOBS.clear()
    chat.STREAMS.clear()


def make_user(s, login, password="password-123", is_admin=False) -> int:
    import clock
    import security
    import state
    cur = state.DB.execute("INSERT INTO users(login, name, pw_hash, is_admin, created) VALUES (?, ?, ?, ?, ?)",
                           (login, login, security.hash_password(password), int(is_admin), clock.NOW()))
    return cur.lastrowid


def login(s, login, password, ip="10.77.48.5", spoofed=None):
    """Apache дописывает настоящий адрес клиента В КОНЕЦ X-Forwarded-For; начало может прислать
    сам клиент (`spoofed`)."""
    forwarded = f"{spoofed}, {ip}" if spoofed else ip
    return s.c.post("/login", data={"login": login, "password": password},
                    headers={"X-Forwarded-For": forwarded}, follow_redirects=False)


def signin(s, login_name) -> str:
    """Вход с паролем make_user и csrf из GET /api/me; клиент дальше шлёт заголовок CSRF сам."""
    import auth
    assert login(s, login_name, "password-123").status_code == 303
    csrf = s.c.get("/api/me").json()["csrf"]
    s.c.headers[auth.CSRF_HEADER] = csrf
    return csrf


def add_connection(s, kind="openai", base_url="https://cloud.test/v1", token="cloud-key", sections=None,
                   name=None) -> int:
    """Подключение текущему пользователю через POST /api/connections (без входа — заводит
    администратора «owner» и входит им). sections=None — все разделы вида."""
    import auth
    import connections
    if auth.CSRF_HEADER not in s.c.headers:
        make_user(s, "owner", is_admin=True)
        signin(s, "owner")
    body = {"name": name or kind, "kind": kind, "base_url": base_url, "token": token,
            "sections": sorted(sections if sections is not None else connections.KINDS[kind])}
    r = s.c.post("/api/connections", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_gateway(s) -> int:
    """Шлюз srvllm со всеми разделами и ключом FakeGateway.TOKEN."""
    from fakes import FakeGateway
    return add_connection(s, kind="srvllm", base_url="http://10.77.1.15:4000", token=FakeGateway.TOKEN,
                          name="srvllm")
