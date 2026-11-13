"""Живая студия для браузерных тестов: uvicorn в потоке на свободном порту `localhost`.

Не тестовый файл, а помощник `test_ui_browser.py` и браузерных тестов разделов. Окружение — как у
фикстуры `env` (база и медиа во временном каталоге), шлюз — `FakeGateway` через
httpx.MockTransport, облачный сервис — `FakeCloud` (настоящий HTTP: его зовёт сам браузер).
Администратор `owner`/`password-123` с подключением «srvllm» заводится через manage.create_admin.
Наблюдатель роликов включён и опрашивает шлюз раз в 0.2 с. Часы настоящие.

Cookie сессии — `__Host-` и Secure: Chromium принимает такие и с `http://localhost` (безопасный
контекст).
"""
from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn

from fakes import FakeCloud, FakeGateway

PASSWORD = "password-123"
POLL_SEC = 0.2


@dataclass
class LiveApp:
    url: str
    gw: FakeGateway
    cloud: FakeCloud
    db_path: Path
    _stop: list = field(default_factory=list, repr=False)

    def add_user(self, login: str, password: str = PASSWORD, *, is_admin: bool = False, theme: str = "auto") -> int:
        """Пользователь без подключений; своим соединением с базой (WAL), а не соединением службы."""
        import db
        import security
        conn = db.connect(self.db_path)
        try:
            return conn.execute("INSERT INTO users(login, name, pw_hash, is_admin, theme, created, email)"
                                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (login, login, security.hash_password(password), int(is_admin), theme,
                                 time.time(), login + '@example.com')).lastrowid
        finally:
            conn.close()

    def set_theme(self, login: str, theme: str) -> None:
        import db
        conn = db.connect(self.db_path)
        try:
            conn.execute("UPDATE users SET theme = ? WHERE login = ?", (theme, login))
        finally:
            conn.close()

    def stop(self) -> None:
        while self._stop:
            self._stop.pop()()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_live_app(tmp_path: Path, *, tls: tuple[Path, Path] | None = None) -> LiveApp:
    from cryptography.fernet import Fernet

    import auth
    import main
    import manage
    import state
    import videos

    port = _free_port()
    url = f"{'https' if tls else 'http'}://localhost:{port}"
    db_path, media_dir = tmp_path / "studio.db", tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    env = {"STUDIO_DATA_KEY": Fernet.generate_key().decode(), "STUDIO_DB": str(db_path),
           "STUDIO_MEDIA_DIR": str(media_dir), "STUDIO_ORIGIN": url}
    saved_env = {k: os.environ.get(k) for k in env}
    saved = (state.make_http, state.WATCHER, videos.POLL_SEC)
    gw, cloud = FakeGateway(), FakeCloud()
    live = LiveApp(url=url, gw=gw, cloud=cloud, db_path=db_path)

    def restore():
        state.make_http, state.WATCHER, videos.POLL_SEC = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        auth.FAILS.clear()

    live._stop.append(restore)
    live._stop.append(cloud.stop)
    os.environ.update(env)
    state.make_http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(gw.handler))
    state.WATCHER = True
    videos.POLL_SEC = POLL_SEC
    auth.FAILS.clear()
    assert manage.create_admin({"STUDIO_ADMIN_LOGIN": "owner", "STUDIO_ADMIN_PASSWORD": PASSWORD,
                                "STUDIO_ADMIN_EMAIL": "owner@example.com",
                                "GATEWAY_KEY_OWNER_IMAGES": FakeGateway.TOKEN}) == 0

    ssl_options = {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])} if tls else {}
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False, lifespan="on", **ssl_options))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    def shutdown():
        server.should_exit = True
        thread.join(10)

    live._stop.append(shutdown)
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            live.stop()
            raise RuntimeError("студия не поднялась")
        time.sleep(0.02)
    return live
