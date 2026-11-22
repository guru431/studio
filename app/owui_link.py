"""Соединение socket.io с Open WebUI — одно на учётку (спека 2026-11-22-studio-open-webui-design, «Соединение
socket.io», «Поток на страницу»).

Свой минимальный клиент: Engine.IO 4 только через websocket (пакет `websockets`), socket.io 5, пространство `/`.
- Подключение: `GET /api/config` (версия — в `owui_accounts.version`; ниже 0.13 или без `features.auth_sessions` —
  `unsupported`), проверка рабочего токена `GET /api/v1/auths/` (Open WebUI 0.13 с `always_connect=True` принимает
  сокет и с негодным токеном, только событий не шлёт — отозванную сессию видно лишь так; отвергнут и только что
  выданный токен — `relogin`, без новой ротации на каждой попытке), затем
  `0{…}` ← открытие, `40{"token": …}` →, `40{"sid": …}` ←. `sid` — `session_id` отправки (`ensure`).
- `2` → `3`. На каждом кадре сервера: рабочий токен (`owui_tokens.access` обновляет его за `REFRESH_AHEAD_SEC` до
  конца) сменился — `42["user-join", {"auth": {"token": …}}]`: так и обновление из REST-запроса доходит до сокета.
  Open WebUI пингует каждые `pingInterval` (25 с), этого хватает с запасом.
- `42["events", {...}]` → `owui_view.normalize` → во все очереди наблюдателей учётки (`watch`).
- Обратный вызов `42<n>[…]` → сразу `43<n>[{"error": REFUSAL}]`: иначе Open WebUI ждёт ответа 60–240 с.
- Обрыв → наблюдателям `link {state: down}`, переподключение с паузой `clock.SLEEP` 1, 2, 4 … `RECONNECT_MAX_SEC`;
  на связи — `link {state: up}` (страница перечитывает открытый чат). Сессию отвергли (`owui_tokens.Relogin`) —
  `link {state: relogin}`, соединение закрыто.
- Открыто, пока есть наблюдатель или идёт ответ на сообщение из Студии (`track`: до `done`, ошибки, паузы или конца
  ответа в чате); без причин дольше `IDLE_CLOSE_SEC` — закрывается на ближайшем кадре сервера (пинг, не позже
  `pingInterval`) или перед попыткой переподключения. Молчание дольше `pingInterval + pingTimeout` — обрыв.
- Итог, ошибка или пауза ответа из Студии — push владельцу (`push.notify_later`, `quiet`: сессиям на экране — нет):
  слова `push.CHAT_NOTICES` / `push.DECISION_NOTICE`, заголовок `push.OWUI_TITLE`, ссылка `owui/<учётка>/<чат>`.
- Адрес, сертификат, имя — через `upstream.ws_target`, как у REST; переадресации не выполняются.
- Журнал — только id учётки, коды отказов, числа и имена классов исключений: ни адреса, ни токенов, ни id чатов.
"""
import asyncio
import json
import logging
from dataclasses import replace
from typing import Awaitable, Callable, Protocol

from fastapi import HTTPException
from websockets.asyncio.client import connect as _ws_connect

import clock
import owui
import owui_tokens
import owui_view
import push
import state
import upstream

log = logging.getLogger("studio")

#: без наблюдателей и идущих ответов соединение живёт столько, с
IDLE_CLOSE_SEC = 60
#: `ensure` ждёт соединения не дольше, с; потом 503 `owui_offline`
CONNECT_WAIT_SEC = 10
#: наибольшая пауза между попытками переподключения, с
RECONNECT_MAX_SEC = 30
#: молчание сервера, после которого соединение считается оборванным, если пакет открытия не назвал своё, с
SILENCE_SEC = 60
#: путь socket.io Open WebUI от адреса учётки
SOCKET_PATH = "/ws/socket.io/?EIO=4&transport=websocket"
#: ответ на обратный вызов Open WebUI
REFUSAL = "client does not support this"
#: предел кадра, байт: итог ответа (`chat:completion` с `output`) бывает большим
FRAME_MAX = 16 * 2**20
#: предел очереди наблюдателя: отстающий наблюдатель теряет события, а не память службы
WATCH_MAX = 2000
USER_AGENT = "Studio"


class Transport(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class _NoRedirects(_ws_connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc                             # переадресация — отказ, как у REST


async def _connect(target: upstream.WsTarget, headers: dict[str, str]) -> Transport:
    """WebSocket к проверенному адресу `target`: соединение на `host:port`, `Host` и SNI — имя адреса, без прокси."""
    kw = {"host": target.host, "port": target.port}
    if target.ssl is not None:
        kw["ssl"] = target.ssl
    if target.server_hostname:
        kw["server_hostname"] = target.server_hostname
    scheme = "wss" if target.ssl is not None else "ws"
    return await _NoRedirects(f"{scheme}://{target.host_header}{target.path}", additional_headers=headers,
                              user_agent_header=None, proxy=None, open_timeout=CONNECT_WAIT_SEC, close_timeout=2,
                              max_size=FRAME_MAX, **kw)


CONNECT: Callable[[upstream.WsTarget, dict[str, str]], Awaitable[Transport]] = _connect


class _Dropped(Exception):
    """Сервер закрыл соединение или нарушил протокол."""


class Link:
    """Соединение учётки: наблюдатели, идущие ответы, задача подключения."""

    def __init__(self, acc):
        self.acc = acc
        self.watchers: list[asyncio.Queue] = []
        self.turns: dict[str, str] = {}        # id ответа → id чата: ответ из Студии идёт
        self.since = clock.NOW()               # с какого момента нет причин держать соединение
        self.state = "connecting"              # connecting | up | down | unsupported | relogin | closed
        self.shown: str | None = None          # последнее `link`, отправленное наблюдателям
        self.sid: str | None = None
        self.refusal: tuple[int, dict] = (503, owui.OFFLINE)
        self.transport: Transport | None = None
        self.sent_token: str | None = None
        self.silence = SILENCE_SEC
        self.settled = asyncio.Event()         # срабатывает в конце каждой попытки (и в конце работы)
        self.wake = asyncio.Event()            # не ждать паузу: соединение нужно сейчас
        self.task = asyncio.create_task(self._run())

    # ---- причины ----

    def _idle(self) -> bool:
        return not self.watchers and not self.turns and clock.NOW() - self.since >= IDLE_CLOSE_SEC

    def _released(self) -> None:
        if not self.watchers and not self.turns:
            self.since = clock.NOW()

    def hurry(self) -> None:
        """Соединение нужно сейчас: прервать паузу перед следующей попыткой (идущей попытке это не нужно)."""
        if self.state in ("down", "unsupported"):
            self.wake.set()

    # ---- наблюдатели ----

    def publish(self, event: dict) -> None:
        for q in list(self.watchers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _set(self, value: str) -> None:
        self.state = value
        shown = "down" if value == "unsupported" else value
        if shown in ("up", "down", "relogin") and shown != self.shown:
            self.shown = shown
            self.publish({"type": "link", "state": shown})

    # ---- работа ----

    async def _run(self) -> None:
        pause = 1
        try:
            while True:
                self.state = "connecting"
                self.wake.clear()
                try:
                    await self._open()
                    pause = 1
                    if await self._pump():
                        log.info("owui %d: соединение закрыто по простою", self.acc.id)
                        return
                except HTTPException as e:
                    code = e.detail.get("code") if isinstance(e.detail, dict) else None
                    self.refusal = (e.status_code, e.detail)
                    if code == "relogin":
                        self._set("relogin")
                        log.info("owui %d: соединение закрыто — нужен вход заново", self.acc.id)
                        return
                    self._set("unsupported" if code == "unsupported" else "down")
                    log.info("owui %d: нет соединения (%s)", self.acc.id, code)
                except owui_tokens.Relogin:
                    self.refusal = (409, owui.RELOGIN)
                    self._set("relogin")
                    log.info("owui %d: соединение закрыто — нужен вход заново", self.acc.id)
                    return
                except Exception as e:                 # обрыв, отказ рукопожатия, нарушение протокола
                    self.refusal = (503, owui.OFFLINE)
                    self._set("down")
                    log.info("owui %d: соединение оборвано (%s)", self.acc.id, type(e).__name__)
                finally:
                    self.sid = None
                    await self._close_transport()
                    settled, self.settled = self.settled, asyncio.Event()
                    settled.set()
                if self._idle():
                    log.info("owui %d: соединение закрыто по простою", self.acc.id)
                    return
                await self._pause(pause)
                pause = min(pause * 2, RECONNECT_MAX_SEC)
        finally:
            if self.state != "relogin":
                self.state = "closed"
            if LINKS.get(self.acc.id) is self:
                del LINKS[self.acc.id]
            self.settled.set()

    async def _pause(self, seconds: float) -> None:
        if self.wake.is_set():
            self.wake.clear()
            return
        sleeper = asyncio.ensure_future(clock.SLEEP(seconds))
        waker = asyncio.ensure_future(self.wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
            self.wake.clear()

    async def _close_transport(self) -> None:
        transport, self.transport = self.transport, None
        if transport is not None:
            try:
                await transport.close()
            except Exception:                          # соединение и так мертво
                pass

    async def _recv(self, timeout: float) -> str:
        while True:
            frame = await asyncio.wait_for(self.transport.recv(), timeout)
            if isinstance(frame, str):
                return frame

    async def _open(self) -> None:
        """Проверить сервер и токен, открыть сокет и войти в пространство `/`."""
        acc = self.acc
        async with owui._errors(acc):
            version = await owui._config(acc)
            if version != acc.version:
                state.DB.execute("UPDATE owui_accounts SET version = ? WHERE id = ?", (version, acc.id))
                self.acc = acc = replace(acc, version=version)
            check = await owui.call(acc, "GET", owui.CHECK_PATH)
            if check.status_code == 401:               # отвергнут и только что выданный токен: новых ротаций не тратить
                raise owui_tokens.relogin(acc.id, "токен после обновления отвергнут")
            if check.status_code != 200:
                log.info("owui %d: проверка токена — код %d", acc.id, check.status_code)
                raise HTTPException(503, owui.OFFLINE)
            token = await owui_tokens.access(acc)
            target = await upstream.ws_target(acc, acc.base_url + SOCKET_PATH)
            self.transport = await CONNECT(target, {"User-Agent": USER_AGENT})
        opening = await self._recv(CONNECT_WAIT_SEC)
        if not opening.startswith("0"):
            raise _Dropped("open")
        try:
            info = json.loads(opening[1:])
            self.silence = (info["pingInterval"] + info["pingTimeout"]) / 1000
        except (ValueError, KeyError, TypeError):
            self.silence = SILENCE_SEC
        await self.transport.send("40" + json.dumps({"token": token}))
        while True:
            frame = await self._recv(CONNECT_WAIT_SEC)
            if frame == "2":
                await self.transport.send("3")
            elif frame.startswith("40"):
                break
            elif frame.startswith(("44", "41", "1")):
                raise _Dropped("connect")
        try:
            sid = json.loads(frame[2:])["sid"]
        except (ValueError, KeyError, TypeError):
            raise _Dropped("sid") from None
        if not isinstance(sid, str) or not sid:
            raise _Dropped("sid")
        self.sid, self.sent_token = sid, token
        self._set("up")
        log.info("owui %d: соединение установлено", acc.id)
        settled, self.settled = self.settled, asyncio.Event()
        settled.set()

    async def _pump(self) -> bool:
        """Кадры сервера до обрыва (исключение) или простоя (`True`)."""
        while True:
            frame = await self._recv(self.silence)
            if frame == "2":
                await self.transport.send("3")
            elif frame.startswith("42"):
                await self._event(frame[2:])
            elif frame.startswith(("41", "44", "1")):
                raise _Dropped("closed")
            await self._rejoin()
            if self._idle():
                return True

    async def _rejoin(self) -> None:
        """Рабочий токен сменился (обновление здесь или в REST-запросе) — сокету новый токен."""
        try:
            token = await owui_tokens.access(self.acc)
        except owui_tokens.Unavailable as e:       # токен ещё жив; следующий кадр попробует снова
            log.info("owui %d: токен не обновлён (код %s)", self.acc.id, e.code)
            return
        if token != self.sent_token:
            await self.transport.send("42" + json.dumps(["user-join", {"auth": {"token": token}}]))
            self.sent_token = token

    async def _event(self, body: str) -> None:
        if body.startswith("/"):
            namespace, _, body = body.partition(",")
            if namespace != "/":
                return
        digits = len(body) - len(body.lstrip("0123456789"))
        if digits:                                     # обратный вызов: ответ сразу, иначе Open WebUI ждёт минуты
            await self.transport.send(f"43{body[:digits]}" + json.dumps([{"error": REFUSAL}]))
            log.info("owui %d: отказ обратному вызову", self.acc.id)
            return
        try:
            args = json.loads(body)
        except ValueError:
            return
        if not isinstance(args, list) or len(args) < 2 or args[0] != "events":
            return
        for event in owui_view.normalize(args[1]):
            self._release(event)
            self.publish(event)

    def _release(self, event: dict) -> None:
        """Ответ из Студии кончился (итог, ошибка, пауза или конец ответа в чате) — он больше не держит сокет.
        Итог, ошибка и пауза такого ответа — push владельцу (вне экрана, без текста ответа и вопроса модели)."""
        kind = event["type"]
        if kind in ("done", "pending") and event.get("message_id") in self.turns:
            message_id = event["message_id"]
            del self.turns[message_id]
            words = push.DECISION_NOTICE if kind == "pending" else push.CHAT_NOTICES[0 if event["error"] is None else 1]
            push.notify_later(self.acc.user_id, words, f"owui/{self.acc.id}/{event['chat_id']}", message_id,
                              title=push.OWUI_TITLE, quiet=True)
        elif kind == "active" and not event["active"]:
            for message_id in [m for m, chat_id in self.turns.items() if chat_id == event["chat_id"]]:
                del self.turns[message_id]
        else:
            return
        self._released()

    async def close(self) -> None:
        if not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except BaseException:                      # отмена и любой отказ задачи — она закрыта
                pass
        await self._close_transport()


#: id учётки Open WebUI → её соединение
LINKS: dict[int, Link] = {}


def _link(acc) -> Link:
    link = LINKS.get(acc.id)
    if link is None or link.task.done():
        link = LINKS[acc.id] = Link(acc)
    else:
        link.acc = acc
    return link


async def watch(acc) -> asyncio.Queue:
    """Очередь событий учётки для SSE; соединение поднимается и держится, пока очередь не снята `unwatch`."""
    link = _link(acc)
    q: asyncio.Queue = asyncio.Queue(WATCH_MAX)
    link.watchers.append(q)
    if link.shown:
        q.put_nowait({"type": "link", "state": link.shown})
    link.hurry()
    return q


def unwatch(aid: int, q: asyncio.Queue) -> None:
    link = LINKS.get(aid)
    if link is not None and q in link.watchers:
        link.watchers.remove(q)
        link._released()


async def ensure(acc) -> str:
    """`sid` живого сокета учётки (для `session_id` отправки); нет — поднять, ждать не дольше `CONNECT_WAIT_SEC`.
    Отказы: 409 `relogin`, 422 `unsupported`, 503 `owui_offline` и прочие отказы `owui.call`."""
    link = _link(acc)
    link.since = max(link.since, clock.NOW())         # отправка идёт: не закрывать по простою до `track`
    if link.state == "up" and link.sid:
        return link.sid
    if link.state != "relogin":
        settled = link.settled
        link.hurry()
        try:
            await asyncio.wait_for(settled.wait(), CONNECT_WAIT_SEC)
        except TimeoutError:
            log.info("owui %d: соединение не поднялось за %d с", acc.id, CONNECT_WAIT_SEC)
            raise HTTPException(503, owui.OFFLINE) from None
    if link.state == "up" and link.sid:
        return link.sid
    raise HTTPException(*link.refusal)


def track(acc, chat_id: str, message_id: str) -> None:
    """Ответ на сообщение из Студии идёт: держать соединение до его итога, ошибки или паузы."""
    _link(acc).turns[message_id] = chat_id


def rekey(aid: int, message_id: str, chat_id: str) -> None:
    """Ответ из Студии узнал свой чат (новый чат — из ответа POST). Ответ, чей итог, ошибка или пауза пришли раньше
    ответа POST, уже снят `_release` — его не возвращать: иначе он держал бы соединение до следующего ответа в чате."""
    link = LINKS.get(aid)
    if link is not None and message_id in link.turns:
        link.turns[message_id] = chat_id


def untrack(aid: int, message_id: str) -> None:
    """Ответа не будет (отправку отвергли, сообщение в очереди, решение не принято) — он не держит соединение."""
    link = LINKS.get(aid)
    if link is not None and link.turns.pop(message_id, None) is not None:
        link._released()


async def drop_account(aid: int) -> None:
    """Закрыть соединение учётки (удаление учётки, учётка Студии удалена или отключена)."""
    link = LINKS.pop(aid, None)
    if link is not None:
        await link.close()


async def stop() -> None:
    for aid in list(LINKS):
        try:
            await drop_account(aid)
        except Exception:                              # задача чужого, уже закрытого цикла событий
            pass
