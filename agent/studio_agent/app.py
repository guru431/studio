"""Оболочка моста: `Controller` — ядро (Agent, Runner, Link) в своём потоке с циклом asyncio, без Tk и трея.

Публичные методы `Controller` зовутся из любого потока (окна Tk, консоли): сеть и SDK — только в цикле ядра."""

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import logging
import threading
from pathlib import Path

from . import autostart, cli, enroll, setup, store, theme, update_helper, updater
from .claude_sdk import ClaudeSDKClient
from .codex import CodexRunner
from .opencode import OpenCodeRunner
from .agent import Agent, read_has_pin
from .link import Link
from .pin import PinStore
from .turns import Runner

log = logging.getLogger(__name__)

TITLE = "Studio Agent"
ALREADY = "Мост уже запущен"
WORDS = {"setup": "не настроен", "connecting": "подключается…", "online": "на связи", "offline": "нет связи",
         "revoked": "компьютер отозван", "replaced": "ключ используется другим мостом"}
PROBE_EVERY_SEC = 3600  # модели Claude Code — не чаще раза в час (решение плана 6)
ACTIVITY_SEC = 0.25  # локальная индикация и штатный выход после завершения работы
_UNKNOWN = object()  # версия `claude.exe` ещё не спрошена (`_main` её не ждёт)
QUIT_SEC = 60  # выход: ходы (turns.CLOSE_SEC × 2) и канал
UNENROLL_SEC = 120  # «Отключиться»: ходы, канал и запрос к студии (15 с) — не дольше
ERASE_TRIES, ERASE_PAUSE_SEC = 10, 0.05  # стирание ключа: файл читает другой поток (окно, трей) — WinError 5/32
ERASE_FAILED = "Не удалось стереть ключ компьютера — закройте окно моста и нажмите «Отключиться» снова"
PIN_LEFT = ("Компьютер отключён, но прежний PIN стереть не удалось — после новой привязки смените или уберите его в "
            "окне моста")


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
                 cli_version=cli.cli_version, probe=cli.probe, enroll_fn=enroll.enroll, unenroll_fn=enroll.unenroll,
                 autostart=autostart, sleep=asyncio.sleep, codex_factory=CodexRunner, opencode_factory=OpenCodeRunner):
        self.h = h
        self._link_factory = link_factory
        self._sdk_factory = sdk_factory
        self._find_cli = find_cli
        self._cli_version = cli_version
        self._probe = probe
        self._enroll_fn = enroll_fn
        self._unenroll_fn = unenroll_fn
        self._autostart = autostart
        self._sleep = sleep
        self._codex_factory = codex_factory
        self._opencode_factory = opencode_factory
        self.codex = None
        self.opencode = None
        self.state = "connecting" if _configured(store.load_config(h), store.load_key(h)) else "setup"
        self._subscribers = []
        self._login_subscribers = []
        self._backend_subscribers = []
        self._activity_subscribers = []
        self._activity = {"tasks": 0, "waiting": 0, "desktop_active": False, "quit_when_idle": False}
        self._quit_when_idle = False
        self._idle_quit_posted = False
        self._backends = {name: {"status": "checking", "models": 0} for name in ("claude", "codex", "opencode")}
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
        self._background: set[asyncio.Task] = set()
        self._cfg_lock = threading.Lock()  # config.json — прочесть, поправить, записать: welcome, окно, отвязка
        self.fresh = False  # новая привязка: первый `online` — уведомление трея
        self.on_pin_changed = None  # блок PIN из студии, снятие блока, новый PIN — меню трея и окно (любой поток)
        self.on_update_quit = None  # штатный выход Tk после подготовки обновления и завершения заданий
        self.on_idle_quit = None  # штатный выход Tk по локальному выбору «Выйти после завершения»

    # --- из любого потока ---

    def on_state(self, callback) -> None:
        """Подписка на смену состояния; зовётся из потока ядра."""
        self._subscribers.append(callback)

    def on_login(self, callback) -> None:
        """Подписка на новый логин из `welcome` (`callback(login)`, после записи в config.json); зовётся из потока
        ядра."""
        self._login_subscribers.append(callback)

    def on_backends(self, callback) -> None:
        """Статусы помощников после проверки каталога; callback вызывается из потока ядра."""
        self._backend_subscribers.append(callback)

    def backend_status(self) -> dict:
        """Снимок последних проверок, без диска и сети: окно читает его из потока Tk."""
        return {name: dict(value) for name, value in self._backends.items()}

    def on_activity(self, callback) -> None:
        """Локальные счётчики и состояние рабочего стола, без содержимого заданий или экрана."""
        self._activity_subscribers.append(callback)

    def activity_status(self) -> dict:
        """Последний снимок ядра; окно и трей не читают изменяемые Runner из своего потока."""
        return dict(self._activity)

    def set_quit_when_idle(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("quit_when_idle")
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._set_quit_when_idle, enabled)
        else:
            self._set_quit_when_idle(enabled)

    def _set_quit_when_idle(self, enabled):
        self._quit_when_idle = enabled
        self._idle_quit_posted = False
        if self.agent is not None:
            self.agent.local_exit_pending = False
        self._refresh_activity()

    def stop_desktop_session(self) -> None:
        """Локальная остановка отбирает токен и отпускает ввод, сохраняя постоянное разрешение."""
        if self.agent is not None:
            self.agent.desktop.revoke()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._refresh_activity)

    def request_quit(self) -> None:
        """Команда локального окна: тот же штатный выход, что и меню трея."""
        if self.on_idle_quit is not None:
            self.on_idle_quit()
        elif self._loop is not None:
            self._loop.call_soon_threadsafe(self._quit.set)

    def _set_backend(self, name: str, status: str, models: int = 0) -> None:
        value = {"status": status, "models": models}
        if self._backends[name] == value:
            return
        self._backends = {**self._backends, name: value}
        for callback in list(self._backend_subscribers):
            try:
                callback(self.backend_status())
            except Exception as e:
                log.warning("подписчик помощников: %s", type(e).__name__)

    @property
    def enrolled(self) -> bool:
        return store.load_key(self.h) is not None

    @property
    def pin_locked(self) -> bool:
        try:
            return PinStore(self.h).locked
        except (OSError, ValueError):
            return False

    @property
    def has_pin(self) -> bool:
        """Задан ли PIN (DPAPI — не из потока Tk); нечитаемый `pin.bin` — «задан», как в `hello`."""
        return read_has_pin(PinStore(self.h))

    def pin_status(self) -> str:
        """`none`, `set` или `locked` — для окна (DPAPI — не из потока Tk); нечитаемый `pin.bin` — `set`."""
        try:
            pins = PinStore(self.h)
            if not pins.has_pin:
                return "none"
            return "locked" if pins.locked else "set"
        except (OSError, ValueError):
            return "set"

    @property
    def login(self) -> str:
        """Логин учётки студии (ответ привязки, `welcome`); нет — пусто."""
        return self.config().login

    def config(self) -> store.Config:
        with self._cfg_lock:
            return store.load_config(self.h)

    def autostart_enabled(self) -> bool:
        """Автозапуск — по реестру (значение указывает на эту копию), а не по `config.json`; чтение быстрое."""
        return self._autostart.enabled()

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
        return asyncio.run_coroutine_threadsafe(self._relink_task(reset_access=True), self._loop)

    def apply_setup(self, form: dict) -> list[str]:
        """Окно настройки (блокирует — звать не из потока Tk): проверка, привязка, запись, автозапуск, новый канал.

        «Привязан» — есть ключ **для этого адреса**: сменили адрес — нужна новая привязка (ключ чужой студии)."""
        cfg = store.load_config(self.h)
        enrolled = store.load_key(self.h) is not None and _same_url(form.get("url"), cfg.url)
        errors = setup.validate(form, enrolled=enrolled)
        if errors:
            return errors
        try:
            with self._cfg_lock:
                setup.apply(self.h, form, enroll_fn=self._enroll_fn)
        except enroll.EnrollError as e:
            log.info("привязка: %s", e.code)
            return [e.words]
        if (form.get("login") or "").strip():
            self.fresh = True
        self._autostart.set_enabled(bool(form.get("autostart")))
        log.info("настройка сохранена")
        self.restart_link()
        return []

    def unenroll(self, *, ask_studio: bool = True) -> tuple[bool, str]:
        """«Отключиться» (блокирует — не из потока Tk) → (ключ стёрт, слова для окна).

        Порядок: ходы прерываются (`done` уходит в канал) и новые не начинаются до нового канала (`close(hold)`),
        канал гаснет, затем — `DELETE` к студии (`ask_studio`; «Войти заново» после отзыва — без запроса), и ключ,
        PIN, `agent_id`, `login` стираются в любом случае: адрес, имя, папка и автозапуск остаются. Стёрто — состояние
        `setup`, слова — `""` или `enroll.KEPT` (студия компьютер не забыла). Файлы не стёрлись и после повторов —
        `(False, ERASE_FAILED)` и канал заново (`setup`, если ключа уже нет)."""
        if self._loop is None or self._loop.is_closed():
            result = asyncio.run(self._forget(ask_studio))
            if result[0]:
                self._set_state("setup")
            return result
        return asyncio.run_coroutine_threadsafe(self._unenroll(ask_studio), self._loop).result(UNENROLL_SEC)

    def set_pin(self, pin: str | None) -> None:
        """Задать (4–8 цифр) или убрать (`None`) PIN; блокирует (scrypt, DPAPI). Канал заново: студия узнаёт
        `has_pin` в новом `hello`, открытые сессии сбрасываются."""
        PinStore(self.h).set_pin(pin)
        log.info("PIN: %s", "задан" if pin is not None else "убран")
        self._pin_changed()
        self._relink_live()

    def set_folder(self, path: str) -> list[str]:
        """Совместимость прежнего окна: заменить список одной папкой."""
        return self.set_folders([path])

    def set_folders(self, paths: list[str]) -> list[str]:
        """Разрешённые папки: проверить все, записать целиком и заново открыть канал."""
        roots = setup.folders({"projects_roots": paths})
        if errors := setup.folder_errors(roots):
            return errors
        with self._cfg_lock:
            cfg = store.load_config(self.h)
            cfg.set_project_roots(roots)
            store.save_config(self.h, cfg)
        log.info("папки проектов сменены: %d", len(cfg.projects_roots))
        self._relink_live()
        return []

    def set_autostart(self, on: bool) -> None:
        """Автозапуск при входе в Windows — реестр (`OSError` наружу) и `config.json`."""
        self._autostart.set_enabled(on)
        with self._cfg_lock:
            cfg = store.load_config(self.h)
            cfg.autostart = bool(on)
            store.save_config(self.h, cfg)
        log.info("автозапуск: %s", "включён" if on else "выключен")

    def set_theme(self, choice: str) -> None:
        """Тема окна — настройка этой копии моста; канал и открытые сессии не меняются."""
        if choice not in ("light", "dark"):
            raise ValueError("theme")
        with self._cfg_lock:
            cfg = store.load_config(self.h)
            cfg.theme = choice
            store.save_config(self.h, cfg)

    def set_desktop_enabled(self, enabled: bool) -> None:
        """Локальное окно: сохранить выключатель. Ни настройки протокола, ни команды CLI для него нет."""
        if type(enabled) is not bool:
            raise ValueError("desktop_enabled")
        # Выключение действует до записи на диск, даже если диск недоступен.
        if not enabled and self.agent is not None:
            self.agent.desktop.set_enabled(False)
        with self._cfg_lock:
            cfg = store.load_config(self.h)
            cfg.desktop_enabled = enabled
            store.save_config(self.h, cfg)
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._refresh_desktop_enabled(), self._loop).result()
        elif self.agent is not None:
            self.agent.desktop.set_enabled(enabled)
            self.agent.cfg.desktop_enabled = enabled
        log.info("рабочий стол: %s", "включён локально" if enabled else "выключен локально")

    async def _refresh_desktop_enabled(self):
        await self._built.wait()
        cfg = await asyncio.to_thread(self.config)
        self.agent.desktop.set_enabled(cfg.desktop_enabled)
        self.agent.cfg.desktop_enabled = cfg.desktop_enabled

    def set_max_turns(self, value: int) -> None:
        """Сохранить общий предел и применить в цикле ядра без переподключения и остановки заданий."""
        if type(value) is not int or value < 1:
            raise ValueError("max_turns")
        with self._cfg_lock:
            cfg = store.load_config(self.h)
            cfg.max_turns = value
            store.save_config(self.h, cfg)
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._refresh_max_turns(), self._loop).result()
        log.info("лимит одновременных заданий: %d", value)

    async def _refresh_max_turns(self) -> None:
        await self._built.wait()
        cfg = await asyncio.to_thread(self.config)
        self.agent.set_max_turns(cfg.max_turns)

    def reset_pin(self) -> None:
        PinStore(self.h).reset()
        log.info("PIN: блок снят")
        self._pin_changed()

    def _pin_changed(self) -> None:
        if self.on_pin_changed is not None:
            self.on_pin_changed()

    def quit(self, timeout: float = QUIT_SEC) -> None:
        """Ходы — `close()` (студия узнает «ход прерван»), канал — `stop()`, цикл — конец; ждёт поток ядра."""
        if self._loop is None or self._thread is None:
            return
        with contextlib.suppress(RuntimeError):  # цикл уже закрыт
            self._loop.call_soon_threadsafe(self._quit.set)
        self._thread.join(timeout)

    def _relink_live(self) -> None:
        """Канал заново, если студия его не погасила: отозванный или заменённый мост ждёт новой привязки (заменённый
        иначе отнял бы ключ у моста, что его заменил)."""
        if self.state not in ("revoked", "replaced"):
            self.restart_link()

    async def _forget(self, ask_studio: bool) -> tuple[bool, str]:
        """Отвязка без канала: запрос к студии (в потоке) и стирание ключа, PIN, `agent_id`, `login` → как `unenroll`."""
        kept = ask_studio and not await asyncio.to_thread(self._ask_studio)
        for attempt in range(ERASE_TRIES):
            try:
                await asyncio.to_thread(self._erase)
                break
            except OSError as e:  # файл держит другой поток (WinError 5/32): пауза и снова
                if attempt + 1 == ERASE_TRIES:
                    gone = not (self.h / store.KEY).exists()  # ключ ушёл, остался PIN — окно уже «Вход»
                    log.error("отвязка: %s не стёрт: %s", "PIN" if gone else "ключ", type(e).__name__)
                    return False, PIN_LEFT if gone else ERASE_FAILED
                await self._sleep(ERASE_PAUSE_SEC)
        self.fresh = False
        log.info("отвязка: ключ и PIN стёрты")
        return True, enroll.KEPT if kept else ""

    def _ask_studio(self) -> bool:
        """`DELETE` к студии (блокирует) → забыла ли она компьютер; любой сбой — не забыла."""
        cfg, key = store.load_config(self.h), store.load_key(self.h)
        if key is None:
            log.info("отвязка: ключ не читается")
            return False
        try:
            status, forgotten = self._unenroll_fn(cfg.url, key)
        except enroll.EnrollError as e:
            log.info("отвязка: %s", e.words)
            return False
        except Exception as e:
            log.warning("отвязка: %s", type(e).__name__)
            return False
        log.info("отвязка: %d", status)
        return forgotten

    def _erase(self) -> None:
        """Ключ первым (канал без него не поднимется), затем PIN и `config.json`; повтор безопасен."""
        with self._cfg_lock:
            (self.h / store.KEY).unlink(missing_ok=True)
            PinStore(self.h).set_pin(None)
            cfg = store.load_config(self.h)
            cfg.agent_id, cfg.login = None, ""
            store.save_config(self.h, cfg)

    # --- поток ядра ---

    async def _unenroll(self, ask_studio: bool) -> tuple[bool, str]:
        await self._built.wait()
        async with self._relink_lock:  # канал не поднимется, пока ключ не стёрт
            await self._close_runners(hold=True)  # просьба turn в пути хода не начнёт: держится до `_relink`
            await self._drop_link()
            self.agent.open.clear()
            try:
                result = await self._forget(ask_studio)
            except Exception as e:
                log.error("отвязка: %s", type(e).__name__)
                result = None
            if result is not None and result[0]:
                self.agent.has_pin = False
                self.agent.cfg = await asyncio.to_thread(store.load_config, self.h)
                self._set_state("setup")
                return result
        await self._relink_task()  # сбой: не «на связи» без канала — канал заново или `setup` (ключа уже нет)
        if result is None:
            raise RuntimeError("отвязка не удалась")
        return result

    def _on_welcome(self, gen: int, frame: dict) -> None:
        """`welcome` (в цикле): логин учётки и имя компьютера → `config.json` в потоке, если сменились."""
        login, name = frame.get("login"), frame.get("name")
        login = login if isinstance(login, str) and login else None
        name = name.strip() if isinstance(name, str) and setup.name_ok(name) else None
        if gen != self._gen or (login is None and name is None):
            return
        task = asyncio.create_task(asyncio.to_thread(self._remember, gen, login, name))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _remember(self, gen: int, login: str | None, name: str | None) -> None:
        """В потоке: запись в config.json; логин сменился — подписчикам `on_login` (окно и трей читают его из файла,
        их смена состояния могла прийти раньше записи)."""
        with self._cfg_lock:
            if gen != self._gen:  # канал уже погашен (отвязка стёрла логин) — не возвращать
                return
            cfg = store.load_config(self.h)
            new = dataclasses.replace(cfg, login=login or cfg.login, name=name or cfg.name)
            if new == cfg:
                return
            store.save_config(self.h, new)
            log.info("вход: логин и имя компьютера — из welcome")
        if new.login != cfg.login:
            for callback in list(self._login_subscribers):
                try:
                    callback(new.login)
                except Exception as e:
                    log.warning("подписчик логина: %s", type(e).__name__)

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
        # Версию `claude --version` (до 30 с при первом запуске) спросит первый канал (`_refresh_cli`) — до своего
        # `hello`: готовность ядра (`_built`, метка самообновления) её не ждёт.
        path = await asyncio.to_thread(self._find_cli)
        self._versioned = _UNKNOWN
        version = ""
        agent = None
        self.runner = Runner(lambda f: agent.emit(f), choices=store.Choices(self.h), cli_path=path,
                             factory=self._sdk_factory, find_cli=self._find_cli)
        if self._codex_factory is not None:
            self.codex = self._codex_factory(lambda f: agent.emit(f), home=self.h, choices=self.runner.choices)
        if self._opencode_factory is not None:
            choices_home = self.h / "opencode"
            await asyncio.to_thread(choices_home.mkdir, parents=True, exist_ok=True)
            self.opencode = self._opencode_factory(lambda f: agent.emit(f), home=self.h,
                                                   choices=store.Choices(choices_home))
        pins = PinStore(self.h)
        has_pin = await asyncio.to_thread(read_has_pin, pins)  # DPAPI — не на цикле
        agent = self.agent = Agent(store.load_config(self.h), pins, self.runner, cc_version=version,
                                   models=[], efforts=[], has_pin=has_pin, codex=self.codex, opencode=self.opencode)
        if self.on_update_quit is not None:
            agent.updater = updater.Updater(self.h, self._update_active, self.on_update_quit)
        agent.on_pin_locked = self._pin_changed
        await asyncio.to_thread(update_helper.acquire_runtime_lease, updater.REPO)
        log.info("Claude Code: %s", "расширение VS Code" if path is not None else cli.BUILTIN)
        self._built.set()
        prober = asyncio.create_task(self._probe_loop())
        activity = asyncio.create_task(self._activity_loop())
        cleanup = asyncio.create_task(updater.cleanup_releases())
        first = asyncio.create_task(self._relink_task())
        await self._quit.wait()
        update_task = agent.updater.task if agent.updater is not None else None
        if update_task is not None and not update_task.done():
            update_task.cancel()
            await asyncio.gather(update_task, return_exceptions=True)
        prober.cancel()
        activity.cancel()
        cleanup.cancel()
        for task in [first, *self._relinks]:
            task.cancel()
        await asyncio.gather(prober, activity, cleanup, first, *self._relinks, return_exceptions=True)
        await self._close_runners(final=True)  # и после: канал ещё досылает очередь, новый ход погиб бы без `done`
        await self._drop_link()
        await agent.desktop.close()
        await asyncio.gather(*self._background, return_exceptions=True)  # welcome: потоки — до закрытия цикла
        log.info("мост: выход")

    def _update_active(self) -> bool:
        return (self.agent is not None and self.agent.desktop.status()["active"]
                or any(backend is not None and backend.live() for backend in (self.runner, self.codex, self.opencode))
                or any(not task.done() for task in getattr(self._link, '_requests', ())))

    def _refresh_activity(self):
        live = [waiting for backend in (self.runner, self.codex, self.opencode) if backend is not None
                for waiting in backend.live().values()]
        active = self.agent is not None and self.agent.desktop.status()["active"]
        value = {"tasks": len(live), "waiting": sum(bool(waiting) for waiting in live),
                 "desktop_active": bool(active), "quit_when_idle": self._quit_when_idle}
        if value != self._activity:
            self._activity = value
            for callback in list(self._activity_subscribers):
                try:
                    callback(self.activity_status())
                except Exception as error:
                    log.warning("подписчик активности: %s", type(error).__name__)
        # Проверка и постановка выхода идут в одном такте ядра. Ожидания карточек — тоже live(),
        # desktop.status() истекает по lease; запросы канала включены даже без активного Runner.
        if self._quit_when_idle and not self._idle_quit_posted and not self._update_active():
            self._idle_quit_posted = True
            if self.agent is not None:
                self.agent.local_exit_pending = True
            self.request_quit()

    async def _activity_loop(self):
        while True:
            self._refresh_activity()
            await asyncio.sleep(ACTIVITY_SEC)

    async def _relink_task(self, *, reset_access: bool = False) -> None:
        task = asyncio.current_task()
        self._relinks.add(task)
        try:
            await self._built.wait()
            if reset_access:
                self.agent.new_link()
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
            self.agent.set_max_turns(cfg.max_turns)
            if not _configured(cfg, key):
                self._set_state("setup")
                return
            self._set_state("connecting")
            await self.agent.refresh_pin()
            await self._refresh_cli()
            if not self._probed:
                await self._refresh_models()
            self.runner.reopen()  # после отвязки, отзыва, замены — новый канал снова принимает ходы
            for backend in (self.codex, self.opencode):
                if backend is not None:
                    backend.reopen()
            gen = self._gen
            self._link = self._link_factory(cfg.url, key, self.agent, lambda s: self._on_link(gen, s),
                                            on_welcome=lambda f: self._on_welcome(gen, f))
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
            await self._close_runners(hold=True)  # и после: просьба turn в пути хода не начнёт — до нового канала
            self.agent.open.clear()

    async def _close_runners(self, **kwargs):
        await asyncio.gather(*(backend.close(**kwargs) for backend in (self.runner, self.codex, self.opencode)
                               if backend is not None))

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
            first = self._versioned is _UNKNOWN
            self.agent.cc_version = await asyncio.to_thread(self._cli_version, path)
            self._versioned = path
            if not first:
                log.info("Claude Code: сменился, версия %s", self.agent.cc_version)

    async def _refresh_models(self) -> bool:
        """Модели и усилия Claude Code → следующий `hello`; пустой итог (сбой) прежние не стирает. Итог — сменился ли
        набор."""
        self._set_backend("claude", "checking")
        models, efforts = await self._probe(self.agent.cfg.projects_root, self.runner.cli_path,
                                            factory=self._sdk_factory)
        self._set_backend("claude", "ready" if models else "unavailable", len(models))
        changed = bool(models) and (models, efforts) != (self.agent.models, self.agent.efforts)
        if models:
            self.agent.set_models(models, efforts)
            self._probed = True
        for name in ("codex", "opencode"):
            backend = getattr(self, name)
            if backend is None:
                self._set_backend(name, "unavailable")
                continue
            self._set_backend(name, "checking")
            previous = backend.catalog
            ready = await backend.probe(self.agent.cfg.projects_root)
            self._set_backend(name, "ready" if ready else "unavailable", len(backend.catalog["models"]) if ready else 0)
            changed = changed or previous != backend.catalog
        log.info("модели Claude Code: %d", len(models))
        return changed

    async def _probe_loop(self) -> None:
        """Раз в час — путь `claude.exe` и модели; набор моделей или усилий сменился — канал заново: обновить
        знакомство протокол v1 не умеет, а студия сверяет выбор со списком своего `hello`. Канал, что не поднят
        (`setup`) или погашен студией (`revoked`, `replaced`), проба не поднимает."""
        while True:
            await self._sleep(PROBE_EVERY_SEC)
            if self.agent.cfg.projects_root:
                await self._refresh_cli()
                if await self._refresh_models() and self.state in ("connecting", "online", "offline"):
                    log.info("модели Claude Code сменились: %d — канал заново", len(self.agent.models))
                    await self._relink_task()


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
    window.prepare(root, theme.dark_for(ctrl.config().theme))
    icon = tray.Tray(ctrl.state, commands.put, lambda: ctrl.pin_locked, ctrl.login)
    ctrl.on_state(lambda state: commands.put(("state", state)))
    ctrl.on_login(lambda login: commands.put(("login", login)))
    ctrl.on_backends(lambda status: commands.put(("backends", status)))
    ctrl.on_activity(lambda status: commands.put(("activity", status)))
    ctrl.on_pin_changed = lambda: commands.put("pin")  # меню трея и окно — «Снять блок PIN»
    ctrl.on_update_quit = lambda: commands.put("quit")
    ctrl.on_idle_quit = lambda: commands.put("quit")
    ready_file = os.environ.pop('STUDIO_AGENT_UPDATE_READY', '')

    def poll() -> None:
        nonlocal ready_file
        if ready_file and ctrl._built.is_set():
            candidate = Path(ready_file)
            if candidate == Path(__file__).resolve().parent.parent / '_update_ready':
                candidate.touch()
            ready_file = ''
        while True:
            try:
                cmd = commands.get_nowait()
            except queue.Empty:
                break
            if isinstance(cmd, tuple) and cmd[0] == "login":
                icon.show(ctrl.state, cmd[1])
                window.login_changed(cmd[1])
            elif isinstance(cmd, tuple) and cmd[0] == "backends":
                window.backends_changed()
            elif isinstance(cmd, tuple) and cmd[0] == "activity":
                icon.show_activity(cmd[1])
                window.activity_changed()
            elif isinstance(cmd, tuple):
                state = cmd[1]
                icon.show(state, ctrl.login)
                window.update(state)
                text = tray.notice(state, ctrl.fresh)
                if state == "online":
                    ctrl.fresh = False
                if text:
                    icon.notify(text)
            elif cmd == "open":
                window.open_main(root, ctrl)
            elif cmd == "reset_pin":  # DPAPI — не в потоке Tk
                threading.Thread(target=ctrl.reset_pin, daemon=True).start()  # сам шлёт `pin` (on_pin_changed)
            elif cmd == "pin":
                icon.refresh()
                window.pin_changed()
            elif cmd == "log":
                with contextlib.suppress(OSError):
                    os.startfile(h / "agent.log")
            elif cmd == "stop_desktop":
                ctrl.stop_desktop_session()
            elif cmd == "quit_idle":
                ctrl.set_quit_when_idle(not ctrl.activity_status()["quit_when_idle"])
            elif cmd == "quit":
                icon.stop()
                ctrl.quit()
                root.destroy()
                return
        root.after(100, poll)

    icon.start()
    ctrl.start()
    if ctrl.state == "setup":
        window.open_main(root, ctrl)
    root.after(100, poll)
    root.mainloop()
    mutex.close()
    log.info("мост: остановлен")
    return 0
