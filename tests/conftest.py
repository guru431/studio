"""Общее для тестов студии: пути проекта, окружение службы, приложение main.app."""
import asyncio
import math
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


@pytest.fixture(autouse=True)
def no_fsync(monkeypatch):
    """Базы тестов — без fsync (`synchronous=OFF`): на нагруженном диске каждый коммит WAL ждал его
    десятки миллисекунд. Видимость записей другим соединениям та же — теряется только стойкость к сбою ОС."""
    import db
    real = db.connect

    def connect(path):
        conn = real(path)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(db, "connect", connect)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Браузерные, PowerShell и живая приёмка — одной группой xdist: один процесс, по очереди
    (браузер на тест и карта прода не делятся). Раньше хука xdist: он дописывает группу к id теста
    в своём хуке того же имени, и метка, поставленная после, до раскладки не доходит."""
    for item in items:
        if item.get_closest_marker("integration") or item.get_closest_marker("manual"):
            item.add_marker(pytest.mark.xdist_group("serial"))


T0 = 1_800_000_000.0


@pytest.fixture(scope="session")
def schema_db(tmp_path_factory):
    """Пустая база текущей схемы — одна на процесс xdist: SCHEMA на новый файл — ~0,2 с, а тестов
    с `studio` — почти весь набор. Закрыта — WAL слит в файл, копия файла — целая база."""
    import db
    path = tmp_path_factory.mktemp("schema") / "studio.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


@pytest.fixture
def studio(env, schema_db, monkeypatch):
    """main.app на свежей базе: часы — список `clock`, пауза двигает их и пишется в `sleeps`,
    шлюз — FakeGateway через httpx.MockTransport. База — копия `schema_db`, если тест не положил
    свою заранее (её lifespan доведёт до VERSION сам)."""
    import shutil

    import httpx
    from fastapi.testclient import TestClient

    import auth
    import chat
    import clock
    import images
    import main
    import media
    import screenlock
    import security
    import state
    import speeches
    import takeout
    import transcripts
    import videos
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
    # пинги чата двигают часы на PING_SEC за миллисекунду: удержанный шлюз (chat_hold) «шёл» бы
    # дольше срока ответа; срок сверяет test_server_answer_is_capped_by_time
    monkeypatch.setattr(chat, "STREAM_TOTAL_SEC", math.inf)
    monkeypatch.setattr(state, "make_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(gw.handler)))
    monkeypatch.setattr(state, "WATCHER", False)
    auth.FAILS.clear()
    screenlock.CHALLENGES.clear()
    images.JOBS.clear()
    chat.STREAMS.clear()
    chat.LIMITS.clear()
    transcripts.JOBS.clear()
    speeches.JOBS.clear()
    videos.WATCHING.clear()
    videos.PAUSED.clear()
    # поток записи, переживший цикл событий прошлого теста, свой id из SAVING уже не снимет
    media.SAVING.clear()
    media.ARCHIVES.clear()
    takeout.EXPORTS.clear()
    takeout.RUNNING.clear()
    if not env.db_path.exists():
        shutil.copyfile(schema_db, env.db_path)
    with TestClient(main.app, base_url="https://testserver") as client:
        yield SimpleNamespace(c=client, clock=now, sleeps=sleeps, gw=gw)
    auth.FAILS.clear()
    screenlock.CHALLENGES.clear()
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


def stored_passport(cid: int) -> "dict | None":
    """Паспорт подключения прямо из базы (как он записан)."""
    import json
    import state
    row = state.DB.execute("SELECT passport FROM connections WHERE id = ?", (cid,)).fetchone()
    return json.loads(row[0]) if row is not None and row[0] else None


def set_passport(cid: int, passport: dict) -> None:
    """Записать паспорт прямо в базу — исходное состояние теста."""
    import json
    import state
    state.DB.execute("UPDATE connections SET passport = ? WHERE id = ?", (json.dumps(passport), cid))


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
