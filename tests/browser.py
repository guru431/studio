"""Headless-браузер под DevTools Protocol для браузерных проверок страницы.

Не тестовый файл, а помощник `test_ui_browser.py`. Урезанная копия
`dashboard/tests/browser_stand.py` из llm_routers (без стенда панели): браузер один на модуль,
на каждый тест — изолированный контекст, на каждый запуск `cdp_driver.mjs` — вкладка.
Сценарий передаётся драйверу в JSON.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

DRIVER = Path(__file__).resolve().parent / "cdp_driver.mjs"
#: `fetch` и `WebSocket` без зависимостей — с node 22.
NODE_FLOOR = 22
DEVTOOLS_TIMEOUT = 30

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

# Fresh Chromium/Edge profiles otherwise probe the Windows account with a blank
# password on every launch (MicrosoftEdge/WebView2Feedback#5722).
CHROMIUM_ARGS = ["--disable-features=AutofillAiWalletPrivatePasses"]


@contextmanager
def run_playwright(pw):
    """Use the test loop's Windows I/O recovery for the synchronous driver as well."""
    with ExitStack() as stack:
        if sys.platform == 'win32':
            from tools.pytest_support import new_event_loop

            with patch.object(asyncio, 'new_event_loop', new_event_loop):
                driver = stack.enter_context(pw.sync_playwright())
        else:
            driver = stack.enter_context(pw.sync_playwright())
        yield driver


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).exists():
            return path
    return (shutil.which("msedge") or shutil.which("google-chrome")
            or shutil.which("chromium") or shutil.which("chrome"))


def find_node() -> str | None:
    """Путь к node не ниже `NODE_FLOOR` либо None."""
    node = shutil.which("node")
    if not node:
        return None
    major = subprocess.run([node, "-p", "process.versions.node"], capture_output=True,
                           encoding="utf-8", timeout=20).stdout.strip().split(".")[0]
    return node if major.isdigit() and int(major) >= NODE_FLOOR else None


def _command(socket, method, **params):
    socket.send(json.dumps({"id": 1, "method": method, "params": params}))
    deadline = time.monotonic() + DEVTOOLS_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        reply = json.loads(socket.recv(timeout=remaining))
        if reply.get("id") == 1:
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']['message']}")
            return reply["result"]
    raise TimeoutError(f"No response to {method}")


class BrowserProcess:
    def __init__(self, node, port, address):
        self.node, self.port, self.address = node, port, address

    @contextmanager
    def isolated(self):
        from websockets.sync.client import connect

        with connect(self.address, open_timeout=DEVTOOLS_TIMEOUT, close_timeout=2, proxy=None) as socket:
            context = _command(socket, "Target.createBrowserContext", disposeOnDetach=True)
            yield Browser(self.node, self.port, socket, context["browserContextId"])


class Browser:
    def __init__(self, node, port, socket, context):
        self.node, self.port, self.socket, self.context = node, port, socket, context

    def drive(self, url: str, steps=(), *, width: int = 1280, height: int = 900,
              mobile: bool = False, touch: bool = False, authenticator: bool = False, init: str | None = None,
              timeout: int = 30000, timezone: str | None = None) -> dict:
        """Открыть адрес во вкладке, пройти шаги, вернуть ответ драйвера. touch — касание вместо мыши
        (`pointer: coarse`), authenticator — виртуальный ключ WebAuthn вкладки (Face ID «проходит» сам),
        init — сценарий до скриптов каждой страницы вкладки. timezone — часовой пояс браузера (IANA).
        timeout — срок одного шага: 15 с не хватило
        входу в длинном прогоне при занятой машине (2026-10-21; под нагрузкой сценарии идут в 3–4 раза
        дольше); отказ шага приносит `where` — где стояла страница."""
        target = _command(self.socket, "Target.createTarget", url="about:blank", browserContextId=self.context)
        page = target["targetId"]
        job = {"port": self.port, "page": page, "url": url, "width": width, "height": height, "mobile": mobile,
               "touch": touch, "authenticator": authenticator, "init": init, "timezone": timezone,
               "timeout": timeout, "steps": [list(step) for step in steps]}
        # весь сценарий — до 180 с: в длинном прогоне отдельный сценарий шёл 76 с (обычно 4 с) — машина
        # на миг тормозит запуск и файлы браузера; шаг ждёт до timeout, сценарий — с запасом на несколько
        out = subprocess.run([self.node, str(DRIVER)], input=json.dumps(job), capture_output=True,
                             encoding="utf-8", timeout=180)
        assert out.returncode == 0, f"драйвер упал: {out.stderr[-800:]}"
        _command(self.socket, "Target.closeTarget", targetId=page)
        return json.loads(out.stdout)


def _devtools_address(profile: Path) -> tuple[int, str] | None:
    """Порт и сокет DevTools из файла, который браузер кладёт в профиль."""
    try:
        words = (profile / "DevToolsActivePort").read_text(encoding="utf-8").split()
    except OSError:
        # Файла нет либо браузер его пишет прямо сейчас: Windows на это время отказывает в чтении.
        return None
    if len(words) == 2 and words[0].isdigit() and words[1].startswith('/devtools/browser/'):
        return int(words[0]), words[1]
    return None


def launch(profile: Path):
    """Генератор для фикстуры: браузер с портом DevTools и своим профилем в `profile`."""
    path = find_browser()
    if not path:
        pytest.skip("нет headless Edge/Chrome — ставить браузер ради теста незачем")
    node = find_node()
    if not node:
        pytest.skip(f"нет node {NODE_FLOOR}+ — драйверу нужны встроенные fetch и WebSocket")
    # Журнал браузера — файлом рядом с профилем, а не в трубу: трубу никто не читает,
    # и браузер встал бы на полном буфере.
    errlog = profile.with_name(profile.name + ".stderr.txt")
    with errlog.open("wb") as err:
        proc = subprocess.Popen(
            [path, "--headless", "--disable-gpu", "--no-sandbox",
             # Edge 157 из повышенного процесса перезапускает себя без повышения уже без наших флагов
             # (не headless, без порта DevTools) и выходит с кодом 0; флаг запрещает перезапуск.
             "--do-not-de-elevate",
             # Без поиска прокси и фоновых служб браузера: с ними отдельные запуски ждали по 15 с.
             "--no-proxy-server", "--no-first-run", "--disable-extensions",
             "--disable-background-networking", "--disable-component-update", "--disable-sync",
             "--enable-logging=stderr",
             # Порт выбирает браузер и записывает в профиль: занятым он не окажется.
             "--remote-debugging-port=0", f"--user-data-dir={profile}", *CHROMIUM_ARGS, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=err)
    try:
        for _attempt in range(DEVTOOLS_TIMEOUT * 10):
            endpoint = _devtools_address(profile)
            if endpoint:
                break
            if proc.poll() is not None:
                tail = errlog.read_text(encoding="utf-8", errors="replace").strip()[-600:]
                raise RuntimeError(f"браузер завершился (код {proc.returncode}), не открыв порт "
                                   f"DevTools; {path}; профиль {profile}; stderr: {tail or 'пусто'}")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"браузер не открыл порт DevTools за {DEVTOOLS_TIMEOUT} с")
        port, path = endpoint
        yield BrowserProcess(node, port, f"ws://127.0.0.1:{port}{path}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        # Профиль — свой на каждый тест: без удаления прогон копил их в %TEMP% (~1,2 ГБ) и кончал место на C:.
        # Дочерние процессы браузера отпускают файлы не сразу — недоудалённое не повод валить тест.
        shutil.rmtree(profile, ignore_errors=True)
