"""Headless-браузер под DevTools Protocol для браузерных проверок страницы.

Не тестовый файл, а помощник `test_ui_browser.py`. Урезанная копия
`dashboard/tests/browser_stand.py` из llm_routers (без стенда панели): браузер один на модуль,
на каждый сценарий — своя вкладка, её открывает и закрывает драйвер `cdp_driver.mjs`,
которому сценарий уходит заданием в JSON.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parent / "cdp_driver.mjs"
#: `fetch` и `WebSocket` без зависимостей — с node 22.
NODE_FLOOR = 22

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


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


class Browser:
    def __init__(self, node: str, port: int):
        self.node, self.port = node, port

    def drive(self, url: str, steps=(), *, width: int = 1280, height: int = 900,
              mobile: bool = False, touch: bool = False, authenticator: bool = False, init: str | None = None,
              timeout: int = 30000, timezone: str | None = None) -> dict:
        """Открыть адрес во вкладке, пройти шаги, вернуть ответ драйвера. touch — касание вместо мыши
        (`pointer: coarse`), authenticator — виртуальный ключ WebAuthn вкладки (Face ID «проходит» сам),
        init — сценарий до скриптов каждой страницы вкладки. timezone — часовой пояс браузера (IANA).
        timeout — срок одного шага: 15 с не хватило
        входу в длинном прогоне при занятой машине (2026-10-21; под нагрузкой сценарии идут в 3–4 раза
        дольше); отказ шага приносит `where` — где стояла страница."""
        job = {"port": self.port, "url": url, "width": width, "height": height, "mobile": mobile,
               "touch": touch, "authenticator": authenticator, "init": init, "timezone": timezone,
               "timeout": timeout, "steps": [list(step) for step in steps]}
        # весь сценарий — до 180 с: в длинном прогоне отдельный сценарий шёл 76 с (обычно 4 с) — машина
        # на миг тормозит запуск и файлы браузера; шаг ждёт до timeout, сценарий — с запасом на несколько
        out = subprocess.run([self.node, str(DRIVER)], input=json.dumps(job), capture_output=True,
                             encoding="utf-8", timeout=180)
        assert out.returncode == 0, f"драйвер упал: {out.stderr[-800:]}"
        return json.loads(out.stdout)


def _devtools_port(profile: Path) -> int | None:
    """Порт из файла, который браузер кладёт в профиль; None — файла ещё нет."""
    try:
        words = (profile / "DevToolsActivePort").read_text(encoding="utf-8").split()
    except OSError:
        # Файла нет либо браузер его пишет прямо сейчас: Windows на это время отказывает в чтении.
        return None
    return int(words[0]) if words else None


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
             "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=err)
    try:
        for _attempt in range(300):
            port = _devtools_port(profile)
            if port:
                break
            if proc.poll() is not None:
                tail = errlog.read_text(encoding="utf-8", errors="replace").strip()[-600:]
                raise RuntimeError(f"браузер завершился (код {proc.returncode}), не открыв порт "
                                   f"DevTools; {path}; профиль {profile}; stderr: {tail or 'пусто'}")
            time.sleep(0.1)
        else:
            raise RuntimeError("браузер не открыл порт DevTools за 30 с")
        yield Browser(node, port)
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
