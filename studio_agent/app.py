"""Оболочка моста: `Controller` — ядро (Agent, Runner, Link) в своём потоке с циклом asyncio, без Tk и трея.

Публичные методы `Controller` зовутся из любого потока (окна Tk, консоли): сеть и SDK — только в цикле ядра."""

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient

from . import autostart, cli, enroll, setup, store
from .agent import Agent
from .link import Link
from .pin import PinStore
from .turns import Runner

log = logging.getLogger(__name__)

TITLE = "Studio Agent"
ALREADY = "Мост уже запущен"
WORDS = {"setup": "не настроен", "connecting": "подключается…", "online": "на связи", "offline": "нет связи",
         "revoked": "компьютер отозван", "replaced": "ключ используется другим мостом"}
PROBE_EVERY_SEC = 3600  # модели Claude Code — не чаще раза в час (решение плана 6)
QUIT_SEC = 60  # выход: ходы (turns.CLOSE_SEC × 2) и канал


def _configured(cfg: store.Config, key: str | None) -> bool:
    """Канал поднимается, только когда есть ключ, папка проектов и верный адрес."""
    if key is None or not cfg.projects_root:
        return False
    try:
        enroll.base_url(cfg.url)
    except enroll.EnrollError:
        return False
    return True


def _same_url(a: str, b: str) -> bool:
    return (a or "").strip().rstrip("/") == (b or "").strip().rstrip("/")


class Controller:
    """Состояние (`state`) — `setup`, `connecting`, `online`, `offline`, `revoked`, `replaced`; слова — `WORDS`.

    `revoked` и `replaced` — канал не поднимается до новой настройки (`restart_link`)."""

    def __init__(self, h: Path, *, link_factory=Link, sdk_factory=ClaudeSDKClient, find_cli=cli.find_cli,
                 cli_version=cli.cli_version, probe=cli.probe, enroll_fn=enroll.enroll, autostart=autostart,
                 sleep=asyncio.sleep):
        self.h = h
        self._link_factory = link_factory
        self._sdk_factory = sdk_factory
        self._find_cli = find_cli
        self._cli_version = cli_version
        self._probe = probe
        self._enroll_fn = enroll_fn
        self._autostart = autostart
        self._sleep = sleep
        self.state = "connecting" if _configured(store.load_config(h), store.load_key(h)) else "setup"
        self._subscribers = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.agent: Agent | None = None
        self.runner: Runner | None = None
        self._versioned = None  # путь `claude.exe`, чья версия — в `agent.cc_version`
        self._probed = False
        self._link = None
        self._link_task: asyncio.Task | None = None
        self._gen = 0  # поколение канала: состояния прежнего канала не в счёт
        self._relinks: set[asyncio.Task] = set()

    # --- из любого потока ---

    def on_state(self, callback) -> None:
        """Подписка на смену состояния; зовётся из потока ядра."""
        self._subscribers.append(callback)

    @property
    def enrolled(self) -> bool:
        return store.load_key(self.h) is not None

    @property
    def pin_locked(self) -> bool:
        try:
            return PinStore(self.h).locked
        except (OSError, ValueError):
            return False

    def config(self) -> store.Config:
        return store.load_config(self.h)

    def start(self) -> None:
        """Поток ядра: Claude Code, Agent, Runner, модели и канал. Возвращается сразу, когда цикл готов."""
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._built = asyncio.Event()
            self._quit = asyncio.Event()
            self._relink_lock = asyncio.Lock()
            self._loop = loop
            ready.set()
            try:
                loop.run_until_complete(self._main())
            except Exception as e:
                log.error("ядро: %s", type(e).__name__)
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="studio-agent-core", daemon=True)
        self._thread.start()
        ready.wait()

    def restart_link(self) -> concurrent.futures.Future:
        """Погасить канал и поднять новый с перечитанными ключом, настройками и PIN (новый `hello`)."""
        if self._loop is None or self._loop.is_closed():
            done = concurrent.futures.Future()
            done.set_result(None)
            return done
        return asyncio.run_coroutine_threadsafe(self._relink_task(), self._loop)

    def apply_setup(self, form: dict) -> list[str]:
        """Окно настройки (блокирует — звать не из потока Tk): проверка, привязка, запись, автозапуск, новый канал.

        «Привязан» — есть ключ **для этого адреса**: сменили адрес — нужна новая привязка (ключ чужой студии)."""
        cfg = store.load_config(self.h)
        enrolled = store.load_key(self.h) is not None and _same_url(form.get("url"), cfg.url)
        errors = setup.validate(form, enrolled=enrolled)
        if errors:
            return errors
        try:
            setup.apply(self.h, form, enroll_fn=self._enroll_fn)
        except enroll.EnrollError as e:
            log.info("привязка: %s", e.code)
            return [e.words]
        self._autostart.set_enabled(bool(form.get("autostart")))
        log.info("настройка сохранена")
        self.restart_link()
        return []

    def reset_pin(self) -> None:
        PinStore(self.h).reset()
        log.info("PIN: блок снят")

    def quit(self, timeout: float = QUIT_SEC) -> None:
        """Ходы — `close()` (студия узнает «ход прерван»), канал — `stop()`, цикл — конец; ждёт поток ядра."""
        if self._loop is None or self._thread is None:
            return
        with contextlib.suppress(RuntimeError):  # цикл уже закрыт
            self._loop.call_soon_threadsafe(self._quit.set)
        self._thread.join(timeout)

    # --- поток ядра ---

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        for callback in list(self._subscribers):
            try:
                callback(state)
            except Exception as e:
                log.warning("подписчик состояния: %s", type(e).__name__)

    async def _main(self) -> None:
        path = self._versioned = await asyncio.to_thread(self._find_cli)
        version = await asyncio.to_thread(self._cli_version, path)
        agent = None
        self.runner = Runner(lambda f: agent.emit(f), choices=store.Choices(self.h), cli_path=path,
                             factory=self._sdk_factory, find_cli=self._find_cli)
        agent = self.agent = Agent(store.load_config(self.h), PinStore(self.h), self.runner, cc_version=version,
                                   models=[], efforts=[])
        log.info("Claude Code: %s", "расширение VS Code" if path is not None else cli.BUILTIN)
        self._built.set()
        prober = asyncio.create_task(self._probe_loop())
        first = asyncio.create_task(self._relink_task())
        await self._quit.wait()
        prober.cancel()
        for task in [first, *self._relinks]:
            task.cancel()
        await asyncio.gather(prober, first, *self._relinks, return_exceptions=True)
        await self.runner.close()
        await self._drop_link()
        log.info("мост: выход")

    async def _relink_task(self) -> None:
        task = asyncio.current_task()
        self._relinks.add(task)
        try:
            await self._relink()
        finally:
            self._relinks.discard(task)

    async def _relink(self) -> None:
        await self._built.wait()
        async with self._relink_lock:
            await self._drop_link()
            if self._quit.is_set():
                return
            cfg, key = await asyncio.to_thread(lambda: (store.load_config(self.h), store.load_key(self.h)))
            self.agent.cfg = cfg
            if not _configured(cfg, key):
                self._set_state("setup")
                return
            self._set_state("connecting")
            await self._refresh_cli()
            if not self._probed:
                await self._refresh_models()
            gen = self._gen
            self._link = self._link_factory(cfg.url, key, self.agent, lambda s: self._on_link(gen, s))
            self._link_task = asyncio.create_task(self._run_link(self._link, gen))

    def _on_link(self, gen: int, state: str) -> None:
        if gen == self._gen and state != "stopped":
            self._set_state(state)

    async def _run_link(self, lk, gen: int) -> None:
        """Канал до конца; отозван (4401) или заменён (4409) — идущие ходы прерываются, открытые сессии прочь:
        отзыв — защита, а заменённому экземпляру просьбы студии больше не придут."""
        try:
            state = await lk.run()
        except Exception as e:
            log.error("канал: %s", type(e).__name__)
            if gen == self._gen:
                self._set_state("offline")
            return
        if state in ("revoked", "replaced") and gen == self._gen:
            await self.runner.close()
            self.agent.open.clear()

    async def _drop_link(self) -> None:
        lk, task = self._link, self._link_task
        self._link = self._link_task = None
        self._gen += 1
        if lk is not None:
            await lk.stop()
        if task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def _refresh_cli(self) -> None:
        """Путь `claude.exe` — прежний, пока файл есть, иначе новейший (`cli.current`); сменился — версия заново
        (студия увидит её в следующем `hello`)."""
        path = self.runner.cli_path = await asyncio.to_thread(cli.current, self.runner.cli_path, self._find_cli)
        if path != self._versioned:
            self.agent.cc_version = await asyncio.to_thread(self._cli_version, path)
            self._versioned = path
            log.info("Claude Code: сменился, версия %s", self.agent.cc_version)

    async def _refresh_models(self) -> None:
        """Модели и усилия Claude Code → следующий `hello`; пустой итог (сбой) прежние не стирает."""
        models, efforts = await self._probe(self.agent.cfg.projects_root, self.runner.cli_path,
                                            factory=self._sdk_factory)
        if models:
            self.agent.set_models(models, efforts)
            self._probed = True
        log.info("модели Claude Code: %d", len(models))

    async def _probe_loop(self) -> None:
        while True:
            await self._sleep(PROBE_EVERY_SEC)
            if self.agent.cfg.projects_root:
                await self._refresh_cli()
                await self._refresh_models()


def prepare_env() -> None:
    """Перед `Controller` (трей и `run --console`): окружение моста без привязки к сессии того, кто его запустил —
    `claude.exe` ходов наследует `os.environ`."""
    dropped = cli.drop_session_env()
    if dropped:
        log.info("окружение: убрано переменных сессии: %d", dropped)


def main(argv: list[str]) -> int:
    """Мост с треем: один экземпляр, главный поток — скрытый корень Tk и очередь команд трея и ядра."""
    import ctypes
    import os
    import queue
    import tkinter as tk
    from tkinter import messagebox

    from . import VERSION, journal, single, tray, window

    with contextlib.suppress(Exception):  # чёткие шрифты окна на экранах с масштабом
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    h = store.home()
    mutex = single.acquire()
    if mutex is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(TITLE, ALREADY, parent=root)
        root.destroy()
        return 1
    journal.setup(h)
    log.info("мост %s: старт", VERSION)
    threading.excepthook = lambda a: log.error("поток: %s", a.exc_type.__name__)
    prepare_env()
    ctrl = Controller(h)
    commands: queue.Queue = queue.Queue()
    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda exc, value, tb: log.error("окно: %s", exc.__name__)
    icon = tray.Tray(ctrl.state, commands.put, lambda: ctrl.pin_locked)
    ctrl.on_state(lambda state: commands.put(("state", state)))

    def poll() -> None:
        while True:
            try:
                cmd = commands.get_nowait()
            except queue.Empty:
                break
            if isinstance(cmd, tuple):
                icon.show(cmd[1])
            elif cmd == "setup":
                window.open_setup(root, ctrl)
            elif cmd == "reset_pin":
                ctrl.reset_pin()
            elif cmd == "log":
                with contextlib.suppress(OSError):
                    os.startfile(h / "agent.log")
            elif cmd == "quit":
                icon.stop()
                ctrl.quit()
                root.destroy()
                return
        root.after(100, poll)

    icon.start()
    ctrl.start()
    if ctrl.state == "setup":
        window.open_setup(root, ctrl)
    root.after(100, poll)
    root.mainloop()
    mutex.close()
    log.info("мост: остановлен")
    return 0
