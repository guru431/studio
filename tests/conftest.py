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
sys.path.insert(0, str(APP))             # модули службы импортируют друг друга по имени, как в main.py


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
    import transcripts
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
    transcripts.JOBS.clear()
    with TestClient(main.app, base_url="https://testserver") as client:
        yield SimpleNamespace(c=client, clock=now, sleeps=sleeps, gw=gw)
    auth.FAILS.clear()
    images.JOBS.clear()
    chat.STREAMS.clear()


@pytest.fixture
def unlinks(monkeypatch):
    """Удаления файлов по порядку: True — с цикла событий. Так нельзя: зависшая шара CIFS
    остановила бы всю службу; удалять — в asyncio.to_thread."""
    import pathlib
    calls, real = [], pathlib.Path.unlink

    def spy(self, missing_ok=False):
        try:
            asyncio.get_running_loop()
            calls.append(True)
        except RuntimeError:
            calls.append(False)
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", spy)
    return calls


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
                   name=None, chat_models=("local-gemma-abl",)) -> int:
    """Подключение текущему пользователю через POST /api/connections (без входа — заводит
    администратора «owner» и входит им). sections=None — srvllm во всех разделах, openai — в чате;
    у openai в чате отмечаются `chat_models` (при подключении у него не отмечено ничего)."""
    import auth
    import connections
    import sections as sections_
    import state
    if auth.CSRF_HEADER not in s.c.headers:
        make_user(s, "owner", is_admin=True)
        signin(s, "owner")
    chosen = sections if sections is not None else connections.KINDS[kind] if kind == "srvllm" else ["chat"]
    body = {"name": name or kind, "kind": kind, "base_url": base_url, "token": token, "sections": sorted(chosen)}
    r = s.c.post("/api/connections", json=body)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    if kind == "openai" and "chat" in chosen and chat_models:
        uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
        sections_.set_models(uid, "chat", cid, list(chat_models))
    return cid


def add_gateway(s) -> int:
    """Шлюз srvllm со всеми разделами и ключом FakeGateway.TOKEN."""
    from fakes import FakeGateway
    return add_connection(s, kind="srvllm", base_url="http://10.77.1.15:4000", token=FakeGateway.TOKEN,
                          name="srvllm")


@pytest.fixture
def fake_audio(monkeypatch):
    """audio без ffmpeg: длительность — `fake.duration`, сжатие и WAV — копия байтов, склейка —
    конкатенация; вызовы — в `fake.calls`."""
    from pathlib import Path as P

    import audio
    fake = SimpleNamespace(calls=[], duration=61.0)

    async def probe(path):
        fake.calls.append(("probe",))
        return fake.duration

    async def compress(src, dst):
        fake.calls.append(("compress",))
        P(dst).write_bytes(P(src).read_bytes())

    async def to_wav(src, dst, *, max_sec):
        fake.calls.append(("to_wav", max_sec))
        P(dst).write_bytes(P(src).read_bytes())
        return min(fake.duration, max_sec)

    async def mp3_of(parts, dst):
        fake.calls.append(("mp3_of", len(parts)))
        P(dst).write_bytes(b"".join(P(p).read_bytes() for p in parts))

    for name, fn in (("probe", probe), ("compress", compress), ("to_wav", to_wav), ("mp3_of", mp3_of)):
        monkeypatch.setattr(audio, name, fn)
    return fake
