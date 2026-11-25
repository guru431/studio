"""Общее для тестов студии: пути проекта, окружение службы, приложение main.app."""
import asyncio
import math
import os
import sys
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.pytest_support import bind_children, new_event_loop, strict_skip

APP = ROOT / "app"
CLIENT = ROOT / "client"
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(APP))             # модули службы импортируют друг друга по имени, как в main.py

# Осознанные пропуски: при STUDIO_TESTS_STRICT=1 остаются пропусками (`strict_skip`), остальные — отказ.
DELIBERATE_SKIPS = (
    "Playwright не эмулирует мобильный Firefox",
    "сценарии «Агентов» — Chromium и WebKit",
    "forced-colors и contrast эмулирует только Chromium",
    "Windows WebKit cannot play an independent PCM WAV Blob",
    "нет прав на символические ссылки",
    # живая приёмка без реквизитов владельца
    "GATEWAY_KEY_OWNER_IMAGES is required",
    "STUDIO_ADMIN_LOGIN and STUDIO_ADMIN_PASSWORD are required",
    "STUDIO_LIVE_VOICE (запись",
    "PowerShell and studio-batch.local.ps1 are required",
    "OWUI_LIVE_URL, OWUI_LIVE_EMAIL and OWUI_LIVE_PASSWORD are required",
    "the model answered without asking a question (ask_user)",
)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    return strict_skip((yield), DELIBERATE_SKIPS, item)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    return strict_skip((yield), DELIBERATE_SKIPS)       # importorskip на уровне модуля


def elevated_pdf_browser(root: Path) -> None:
    """Сессия администратора Windows: Edge и Chrome снимают повышение и перезапускаются, md2pdf печатает пусто
    (503 `pdf_failed`, docs/architecture/sharing-export.md). Только для тестов — обёртка браузера md2pdf
    с `--do-not-de-elevate`; служба и общий md2pdf не меняются, заданный `MD2PDF_BROWSER` не трогается."""
    if os.name != "nt" or os.environ.get("MD2PDF_BROWSER"):
        return
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        return
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            exe = Path(base) / rel if base else None
            if exe is not None and exe.is_file() and str(exe).isascii():
                root.mkdir(parents=True, exist_ok=True)
                wrapper = root / f"md2pdf-browser-{os.getpid()}.cmd"      # свой у каждого процесса xdist
                wrapper.write_bytes(f'@echo off\r\n"{exe}" --do-not-de-elevate %*\r\n'.encode("ascii"))
                os.environ["MD2PDF_BROWSER"] = str(wrapper)
                return


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Каталоги/очистка pytest Studio не пересекаются с остальными проектами; дети процесса (браузеры,
    драйверы) не переживают его аварийное завершение (`bind_children`)."""
    bind_children()
    if config.option.basetemp is None and not os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        root = ROOT / ".tmp" / ".pytest_cache" / "main"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)
    elevated_pdf_browser(Path(os.environ.get("PYTEST_DEBUG_TEMPROOT") or ROOT / ".tmp"))


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
def sqlite_test_settings(monkeypatch):
    """Базы тестов без fsync и кэша SQL-выражений, который может давать неверные результаты
    при чтении общего соединения из потока pytest и цикла TestClient (CPython #118172)."""
    import db
    real = db.connect

    def connect(path):
        conn = real(path, cached_statements=0)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(db, "connect", connect)


def pytest_ignore_collect(collection_path, config):
    # Browser modules belong to integration; skip even importing them in each fast worker.
    if config.option.markexpr == "not integration and not manual" and collection_path.match("test_ui_*.py"):
        return True
    return None


@pytest.fixture(scope="module")
def cdp_process(tmp_path_factory):
    from browser import launch

    yield from launch(tmp_path_factory.mktemp("studio-ui-browser"))


@pytest.fixture
def browser(cdp_process):
    with cdp_process.isolated() as context:
        yield context


#: Сколько процессов xdist одновременно ведут файлы integration (браузер, живая служба, PowerShell).
LANES = 3
#: Группа integration-файла: `lane:<имя файла>` — файл целиком в одном процессе, файлы — только на дорожках.
LANE = "lane:"


def _lane_scope(scope: str) -> bool:
    return scope == "serial" or scope.startswith(LANE)


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config, items):
    """Группы xdist: живая приёмка (`manual`) — `serial`, один процесс по очереди (карта прода не делится);
    файл integration — своя группа `lane:<файл>`: модульные браузер и живая служба остаются в одном процессе,
    а планировщик ведёт такие файлы не больше чем в `LANES` процессах сразу. Метка ставится до хука xdist:
    он дописывает группу к id теста. pytest 9.2 хранит id в `_id`, и запись xdist в `_nodeid` теряется —
    тогда суффикс дописывается здесь же, после всех хуков, тем же форматом `@группа`."""
    for item in items:
        if item.get_closest_marker("manual"):
            item.add_marker(pytest.mark.xdist_group("serial"))
        elif item.get_closest_marker("integration") and not item.get_closest_marker("xdist_group"):
            item.add_marker(pytest.mark.xdist_group(LANE + item.path.name))
    result = yield
    if getattr(config.option, "loadgroup", False):
        for item in items:
            groups = sorted({str(m.args[0] if m.args else m.kwargs.get("name", "default"))
                             for m in item.iter_markers("xdist_group")})
            suffix = "@" + "_".join(groups)
            if groups and not item.nodeid.endswith(suffix):
                from _pytest.nodeid import NodeId
                item._id = NodeId.parse(item.nodeid + suffix)
    return result


def pytest_xdist_make_scheduler(config, log):
    """`--dist loadgroup` без повтора упавшего процесса. Срок pytest-timeout на Windows — только `thread`: по
    сроку он печатает стеки и делает `os._exit` процесса (xdist — «node down: Not properly terminated», тест —
    «crashed while running»). Штатный LoadScopeScheduling.remove_node возвращает в очередь и сам упавший тест: он
    валит процесс снова и снова до предела перезапусков (4 × число процессов), по 30 с каждый, — так быстрый набор
    шёл дольше 15 минут и «не прерывался» сроком (опыт 2026-10-29: четыре зависших теста — 18 падений и 46 с
    против четырёх и 5 с). Здесь упавший — один раз, прочие недоделанные — снова в очередь, как у xdist.
    Группы `serial` и `lane:<файл>` берут только дорожки — не больше `LANES` процессов; остальные процессы
    ведут быстрые файлы и, когда подходящей им работы не осталось, завершаются."""
    if config.getoption("dist") != "loadgroup":
        return None
    from xdist.scheduler import LoadGroupScheduling

    class Once(LoadGroupScheduling):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.lanes = set()

        def _split_scope(self, nodeid):
            scope = super()._split_scope(nodeid)
            # Keep explicit groups (serial/docs/lane); batch the other tests by file.
            return nodeid.split("::", 1)[0] if scope == nodeid else scope

        def _suitable(self, node):
            # Дорожка сперва берёт integration-файлы: они держат срок прогона; быстрые — остальным процессам.
            lane = node in self.lanes or len(self.lanes) < LANES
            ordered = sorted(self.workqueue, key=lambda scope: not _lane_scope(scope)) if lane else self.workqueue
            return next((scope for scope in ordered if lane or not _lane_scope(scope)), None)

        def _assign_work_unit(self, node):
            scope = self._suitable(node)
            if scope is None:
                return
            if _lane_scope(scope):
                self.lanes.add(node)
            self.workqueue.move_to_end(scope, last=False)
            super()._assign_work_unit(node)

        def _reschedule(self, node):
            # Замена уже есть в assigned_work, но её коллекция может прийти после других событий.
            if node not in self.registered_collections or node.shutting_down:
                return
            if self.workqueue and self._suitable(node) is None:
                if not self._pending_of(self.assigned_work[node]):
                    node.shutdown()
                return
            super()._reschedule(node)

        def remove_node(self, node):
            self.lanes.discard(node)
            workload = self.assigned_work.pop(node)
            pending = [(scope, nid) for scope, unit in workload.items() for nid, done in unit.items() if not done]
            if not pending:
                return None
            scope, crashed = pending[0]                 # первый недоделанный — тот, на котором процесс упал
            del workload[scope][crashed]
            self.workqueue.update((s, unit) for s, unit in workload.items() if not all(unit.values()))
            for other in self.assigned_work:
                self._reschedule(other)
            return crashed

    return Once(config, log)


T0 = 1_800_000_000.0
LOGIN_IP = "10.77.48.5"


@pytest.fixture(scope="session")
def schema_db(tmp_path_factory):
    """Пустая база текущей схемы — один снимок в памяти на процесс xdist.

    SQLite сливает WAL при закрытии соединения. Каждый тест получает отдельный файл
    полной базы без повторного открытия и проверок пути файла шаблона.
    """
    import db
    import tempfile
    with tempfile.TemporaryDirectory(prefix='schema-', dir=tmp_path_factory.getbasetemp()) as directory:
        path = Path(directory) / 'studio.db'
        conn = db.connect(path)
        try:
            conn.execute('PRAGMA synchronous=OFF')
            db.migrate(conn)
        finally:
            conn.close()
        return path.read_bytes()


@pytest.fixture(scope="session")
def studio_db():
    """Снимок пустой базы после одноразовой инициализации при старте службы."""
    import clock
    import db
    import roles
    import search
    import sections
    import state

    with closing(db.connect(":memory:")) as conn, pytest.MonkeyPatch.context() as patch:
        db.migrate(conn)
        patch.setattr(state, 'DB', conn)
        patch.setattr(clock, 'NOW', lambda: T0)
        sections.adopt('translate', 'chat')
        roles.seed_defaults()
        search.compose_once(conn)
        return conn.serialize()


@pytest.fixture
def studio(env, studio_db, monkeypatch, request):
    """main.app на свежей базе: часы — список `clock`, пауза двигает их и пишется в `sleeps`,
    шлюз — FakeGateway через httpx.MockTransport. Обычный тест получает свою копию `studio_db` в памяти.
    Для перезапуска или второго соединения нужен `file_db`; заранее подготовленный файл тоже используется."""
    import httpx
    from fastapi.testclient import TestClient

    import auth
    import card
    import chat
    import claude_code
    import clock
    import db
    import images
    import main
    import media
    import ocr
    import screenlock
    import security
    import state
    import speech
    import speeches
    import takeout
    import transcripts
    import translate
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
    chat.NO_TOOLS.clear()
    chat.NO_USAGE.clear()
    transcripts.JOBS.clear()
    card.TYPICAL.clear()                 # медианы прошлого теста (часы теста стоят)
    speech.LIMITS.clear()                # пределы речи прошлого теста (ключ — id подключения той базы)
    ocr.JOBS.clear()
    ocr.RUNNERS.clear()
    ocr.LEASED.clear()
    ocr.SETTLING.clear()
    ocr.EXTERNAL.clear()
    translate.JOBS.clear()
    translate.RUNNERS.clear()
    speeches.JOBS.clear()
    videos.WATCHING.clear()
    videos.PAUSED.clear()
    # поток записи, переживший цикл событий прошлого теста, свой id из SAVING уже не снимет
    media.SAVING.clear()
    media.ARCHIVES.clear()
    takeout.EXPORTS.clear()
    takeout.RUNNING.clear()
    claude_code.PDFS.clear()
    claude_code.code_visualizations.VIEWS.clear()
    claude_code.NEW_CHATS.clear()
    clear_bridge()
    clear_push()
    import mcp_sources
    mcp_sources.CACHE.clear()            # каталоги MCP прошлого теста (ключ — id сервера той базы)
    import owui_tokens
    owui_tokens.TOKENS.clear()           # рабочие токены Open WebUI и блокировки обновления (ключ — id учётки той базы)
    owui_tokens.LOCKS.clear()
    import owui
    owui.FAILS.clear()                   # неудачи входа в Open WebUI (ключ — uid той базы)
    clear_owui_links()
    with ExitStack() as stack:
        if not env.db_path.exists():
            if any(request.node.get_closest_marker(mark) for mark in ("file_db", "integration", "manual")):
                env.db_path.write_bytes(studio_db)
            else:
                connect = db.connect
                conn = stack.enter_context(closing(connect(":memory:")))
                conn.deserialize(studio_db)
                opened = False

                def connect_studio(path):
                    nonlocal opened
                    if path != env.db_path:
                        return connect(path)
                    if opened:
                        raise RuntimeError("reopening the Studio database requires @pytest.mark.file_db")
                    opened = True
                    return conn

                patch = stack.enter_context(pytest.MonkeyPatch.context())
                patch.setattr(db, "connect", connect_studio)
        client = stack.enter_context(TestClient(main.app, base_url="https://testserver",
                                                backend_options={"loop_factory": new_event_loop}))
        yield SimpleNamespace(c=client, clock=now, sleeps=sleeps, gw=gw)
    auth.FAILS.clear()
    screenlock.CHALLENGES.clear()
    images.JOBS.clear()
    chat.STREAMS.clear()
    claude_code.PDFS.clear()
    claude_code.code_visualizations.VIEWS.clear()
    claude_code.NEW_CHATS.clear()
    clear_bridge()
    clear_push()
    clear_owui_links()


def clear_owui_links() -> None:
    """Соединения Open WebUI — модульные (ключ — id учётки той базы); задачи прошлого цикла событий отменяются,
    как у моста."""
    import owui_link
    for link in list(owui_link.LINKS.values()):
        if not link.task.done():
            try:
                link.task.cancel()
            except RuntimeError:
                pass
    owui_link.LINKS.clear()


def clear_bridge() -> None:
    """Каналы, наблюдатели и фоновые отправки моста — модульные; незаконченные задачи прошлого цикла
    событий отменяются (цикл уже закрыт — отмена ему не нужна)."""
    import bridge
    for task in list(bridge.TASKS):
        if not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass
    bridge.TASKS.clear()
    bridge.LINKS.clear()
    bridge.WATCHERS.clear()
    bridge.INITIATORS.clear()
    bridge.TURN_REQUESTS.clear()
    bridge.CARDS.clear()


def clear_push() -> None:
    """Ключ VAPID и фоновые отправки push — модульные (как у моста)."""
    import chat
    import push
    push.KEY = None
    push.ON_SCREEN.clear()
    chat.ANSWERED.clear()
    for task in list(push.TASKS):
        if not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass
    push.TASKS.clear()


@pytest.fixture
def no_pings(monkeypatch):
    """Фиктивные часы в тестах сроков сдвигает сценарий, а не фоновые пинги SSE."""
    import chat

    async def idle(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(chat.StreamTask, "_pings", idle)


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
    cur = state.DB.execute("INSERT INTO users(login, name, pw_hash, is_admin, created, email) VALUES (?, ?, ?, ?, ?, ?)",
                           (login, login, security.hash_password(password), int(is_admin), clock.NOW(), login + '@example.com'))
    return cur.lastrowid


def erase_history(s, password="password-123"):
    """«Удалить всю историю» — паролем учётки (по умолчанию — пароль make_user)."""
    return s.c.request("DELETE", "/api/history", json={"password": password})


def login(s, login, password, ip=LOGIN_IP, spoofed=None):
    """Apache дописывает настоящий адрес клиента В КОНЕЦ X-Forwarded-For; начало может прислать
    сам клиент (`spoofed`)."""
    forwarded = f"{spoofed}, {ip}" if spoofed else ip
    return s.c.post("/login", data={"login": login, "password": password},
                    headers={"X-Forwarded-For": forwarded}, follow_redirects=False)


def signin(s, login_name) -> str:
    """Вход с паролем make_user; CSRF выдаёт созданная маршрутом сессия."""
    import auth

    response = login(s, login_name, "password-123")
    assert response.status_code == 303
    session = auth.session_user(response.cookies.get(auth.COOKIE), renew=False)
    assert session is not None
    _, csrf = session
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
    """Подготавливает подключение текущему пользователю (без входа — заводит
    администратора «owner» и входит им). sections=None — srvllm во всех разделах, openai — в чате;
    у openai в чате отмечаются `chat_models` (при подключении у него не отмечено ничего)."""
    import auth
    import connections
    import sections as sections_
    if auth.CSRF_HEADER not in s.c.headers:
        make_user(s, "owner", is_admin=True)
        signin(s, "owner")
    session = auth.session_user(s.c.cookies.get(auth.COOKIE), renew=False)
    assert session is not None
    user, _ = session
    chosen = sections if sections is not None else connections.KINDS[kind] if kind == "srvllm" else ["chat"]
    body = connections.ConnIn(name=name or kind, kind=kind, base_url=base_url, token=token, sections=sorted(chosen))
    cid = connections.create(user.id, body)["id"]
    if kind == "openai" and "chat" in chosen and chat_models:
        sections_.set_models(user.id, "chat", cid, list(chat_models))
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

    async def cut(src, start, length, dst):
        fake.calls.append(("cut", start, length))
        P(dst).write_bytes(P(src).read_bytes())

    async def to_wav(src, dst, *, max_sec, start=0):
        fake.calls.append(("to_wav", max_sec))
        if start:
            fake.calls.append(("wav_start", start))
        P(dst).write_bytes(P(src).read_bytes())
        return min(max(0, fake.duration - start), max_sec)

    async def mp3_of(parts, dst):
        fake.calls.append(("mp3_of", len(parts)))
        P(dst).write_bytes(b"".join(P(p).read_bytes() for p in parts))

    async def join_mp3(parts, dst):
        fake.calls.append(("join_mp3", len(parts)))
        P(dst).write_bytes(b"".join(P(p).read_bytes() for p in parts))

    for name, fn in (("probe", probe), ("compress", compress), ("cut", cut), ("to_wav", to_wav), ("mp3_of", mp3_of),
                     ("join_mp3", join_mp3)):
        monkeypatch.setattr(audio, name, fn)
    return fake
