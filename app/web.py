"""Инструменты чата «Интернет» (спека 2026-10-21-studio-chat-internet): поиск и страницы Exa, субтитры
YouTube, определения инструментов для модели. Ключ Exa — у учётки (`users.exa_key`, шифром
security.encrypt, как токены подключений); сохраняет и убирает его profile.py, наружу он не уходит и в
журнал не пишется. Неудача инструмента — ToolError со словами для человека.

Exa — постоянный адрес `EXA_URL` через общий клиент state.HTTP, а не upstream: адрес выбирает не
пользователь и не модель (адрес страницы уходит в Exa данными, сервер по нему не ходит; и он — только из
переписки или найденный поиском, `known` у call). Наружу — только поисковый запрос или адрес страницы и ключ
заголовком `x-api-key`. Шаг инструмента — dict: поиск
`{"kind": "search", "query", "results": [{"title", "url", "published"}], "text"}`, страница
`{"kind": "page", "url", "title", "text"}`; `text` — ровно то, что получит модель.

YouTube — yt-dlp образа Docker (`/usr/local/bin/yt-dlp`, версия закреплена в deploy/docker/Dockerfile;
обновляется сменой закрепления и выкладкой — ночного обновления нет), списком аргументов без оболочки. Адрес разбирает студия (`youtube_id`), yt-dlp получает только канонический
`https://www.youtube.com/watch?v=<id>`: у него есть общий извлекатель, и чужой адрес сервер прочитал бы. Ролик —
только тот, чей id есть у адреса из переписки или найденного поиском (`known` у call, как у страниц): id,
составленный моделью, был бы каналом вывода переписки.
Одна дорожка — язык оригинала (`pick_track`; автопереводы не берутся: третий подряд запрос дорожки YouTube
отвечал 429). Файлы — `<uuid>.*` в state.CFG.tmp_dir (файлы, не каталог: audio.clean_tmp удаляет файлы),
убираются в finally. stderr yt-dlp — только для выбора слов ошибки, в журнал не идёт. Шаг —
`{"kind": "youtube", "id", "url", "title", "channel", "duration", "lang", "text"}`."""
import asyncio
import html
import json
import os
import re
import uuid
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from cryptography.fernet import InvalidToken

import security
import state
from connections import Connection

EXA_URL = "https://api.exa.ai"
EXA_TIMEOUT_SEC = 30
SEARCH_RESULTS = 10          # базовая цена поиска Exa — до 10 результатов
PAGE_MAX = 40_000            # знаков текста страницы: ~10 тыс. токенов из 131 072 контекста gemma-abl
PAGE_CUT = "\n\n[страница обрезана]"
# пределы аргументов модели: запрос — единственное, что из переписки уходит в Exa, — не больше абзаца
QUERY_MAX = 500
URL_MAX = 2048
NOT_KNOWN = "читаю только ссылки из переписки или найденные поиском"
_LINK = re.compile(r"https?://[^\s<>\"'«»`]+", re.I)
_LINK_TAIL = ".,;:!?)]}"
_TRACKING = re.compile(r"(?:utm_[^=&]*|fbclid|gclid|yclid)(?:=[^&]*)?", re.I)   # метки рекламы в запросе адреса

YTDLP = "yt-dlp"
YTDLP_TIMEOUT_SEC = 60       # на каждый из двух вызовов; дорожка 1,5-часового ролика скачивалась за ~1 с
# yt-dlp сразу на всю службу: каждый — процесс Python в MemoryMax службы, и YouTube отвечает 429 одному
# адресу на частые запросы
YT_SLOTS = asyncio.Semaphore(2)
SUBS_MAX = 120_000           # знаков субтитров: 1,5 ч — ~94 тыс., ~30 тыс. токенов из 131 072 контекста
PARAGRAPH_SEC = 30           # абзац с отметкой времени — не чаще раза в 30 с ролика
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_PATH_ID = re.compile(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?")
_TAG = re.compile(r"<[^>]*>")
_YT_CLOSED = ("Private video", "Sign in", "unavailable", "has been removed")


KINDS = ("search", "page", "youtube")
NAMES = {"web_search": "search", "read_page": "page", "youtube_subtitles": "youtube"}   # имя функции → вид
_SPECS = {
    "search": ("web_search", "Найти в интернете свежие или внешние сведения. Вызывай, только когда для ответа "
               "нужны свежие данные или сведения, которых нет в разговоре, или пользователь просит поискать.",
               "query", "поисковый запрос"),
    "page": ("read_page", "Прочитать веб-страницу по адресу: тот, что дал пользователь, или из результатов "
             "поиска.", "url", "адрес страницы, http или https"),
    "youtube": ("youtube_subtitles", "Прочитать содержание ролика YouTube по его субтитрам: того, что дал "
                "пользователь, или из результатов поиска.",
                "url", "адрес ролика YouTube"),
}


class ToolError(Exception):
    """Неудача инструмента; args[0] — слова для человека (без ключей и ответов сервиса)."""


def tools_for(conn: "Connection | None", has_key: bool) -> list[str]:
    """Виды инструментов, которые ход диалога даст модели: только путь через сервер (`conn.proxied`, как
    `via` в chat.create_turn) и только с ключом Exa — без ключа ни одного, и YouTube тоже (решение владельца
    2026-10-21)."""
    if conn is None or not conn.proxied or not has_key:
        return []
    return list(KINDS)


def specs(kinds: list[str]) -> list[dict]:
    """Определения инструментов OpenAI (`tools` запроса) для видов `kinds` в их порядке."""
    out = []
    for kind in kinds:
        name, about, param, param_about = _SPECS[kind]
        out.append({"type": "function", "function": {
            "name": name, "description": about,
            "parameters": {"type": "object", "properties": {param: {"type": "string", "description": param_about}},
                           "required": [param]}}})
    return out


def has_exa_key(user_id: int) -> bool:
    row = state.DB.execute("SELECT exa_key IS NOT NULL FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def exa_key(user_id: int) -> "str | None":
    """Ключ Exa учётки; нет — None. Не расшифровался (сменили STUDIO_DATA_KEY) — ToolError."""
    row = state.DB.execute("SELECT exa_key FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return security.decrypt(row[0])
    except InvalidToken:
        raise ToolError(NO_KEY) from None


def _exa_error(r: httpx.Response) -> str:
    """Слова для неудачного ответа Exa — по `tag` тела, затем по коду (exa.ai/docs/reference/error-codes).
    Не JSON — HTML Cloudflare: Exa закрыт для выхода сервера студии."""
    try:
        data = r.json()
    except ValueError:
        return "Exa недоступен с сервера студии"
    tag = data.get("tag") if isinstance(data, dict) else None
    tag = tag if isinstance(tag, str) else ""
    if tag.endswith("_BUDGET_EXCEEDED"):             # API_KEY_… и TEAM_…: предел расходов, а не баланс
        return "Exa: исчерпан лимит расходов ключа — см. dashboard.exa.ai"
    if tag in ("NO_CONTENT_FOUND", "FETCH_DOCUMENT_ERROR"):
        return "страница не прочиталась"
    if tag == "ROBOTS_FILTER_FAILED":
        return "сайт запрещает чтение (robots.txt)"
    if r.status_code == 401:
        return "ключ Exa не подходит — проверьте в Настройках"
    if r.status_code == 402:                         # NO_MORE_CREDITS
        return "Exa: баланс исчерпан (бесплатный обновится 1-го числа)"
    if r.status_code == 429:
        return "Exa: слишком часто — попробуйте позже"
    return f"Exa ответил {r.status_code}"


async def _exa(key: str, path: str, body: dict) -> dict:
    try:
        r = await state.HTTP.post(f"{EXA_URL}{path}", json=body, headers={"x-api-key": key},
                                  timeout=EXA_TIMEOUT_SEC)
    except httpx.TimeoutException:
        raise ToolError(f"Exa не ответил за {EXA_TIMEOUT_SEC} с") from None
    except httpx.HTTPError as e:
        raise ToolError(f"Exa недоступен: {type(e).__name__}") from None
    if r.status_code != 200:
        raise ToolError(_exa_error(r))
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ToolError("Exa недоступен с сервера студии")
    return data


async def search(key: str, query: str) -> dict:
    data = await _exa(key, "/search", {"query": query, "type": "auto", "numResults": SEARCH_RESULTS,
                                       "contents": {"highlights": True}})
    results, blocks = [], []
    for n, res in enumerate((data.get("results") or [])[:SEARCH_RESULTS], 1):
        title, url = res.get("title") or "", res.get("url") or ""
        published = (res.get("publishedDate") or "")[:10]
        results.append({"title": title, "url": url, "published": published})
        lines = [f"{n}. {title or url}", url]
        if published:
            lines.append(published)
        lines += [h.strip() for h in res.get("highlights") or [] if isinstance(h, str) and h.strip()]
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks) if blocks else "Поиск ничего не нашёл."
    return {"kind": "search", "query": query, "results": results, "text": text}


async def page(key: str, url: str) -> dict:
    try:
        parts = urlsplit(url)
    except ValueError:                   # «http://[::1» — адрес составляет модель
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ToolError("адрес страницы — только http или https")
    data = await _exa(key, "/contents", {"urls": [url], "text": {"maxCharacters": PAGE_MAX}})
    statuses = data.get('statuses')
    if statuses is not None and (not isinstance(statuses, list) or any(not isinstance(row, dict) for row in statuses)):
        raise ToolError('страница не прочиталась')
    status = (statuses or [{}])[0]
    if status.get("status") == "error":
        error = status.get('error')
        tag = (error.get('tag') if isinstance(error, dict) else None) or '?'
        raise ToolError("страница не найдена" if tag == "CRAWL_NOT_FOUND" else f"страница не прочиталась ({tag})")
    results = data.get("results") or []
    if not results:
        raise ToolError("страница не прочиталась")
    title, text = results[0].get("title") or "", results[0].get("text") or ""
    if len(text) >= PAGE_MAX:                    # Exa резал по maxCharacters (или не резал вовсе)
        text = text[:PAGE_MAX] + PAGE_CUT
    return {"kind": "page", "url": url, "title": title, "text": f"{title}\n{url}\n\n{text}"}


def _env() -> dict:
    """Окружение yt-dlp: без окружения службы (там STUDIO_DATA_KEY); Windows (тесты) без SYSTEMROOT
    процессы не запускает. HOME — не tmp_dir: каталог, что там завёл бы yt-dlp или его JS-рантайм
    (`.cache/` deno; в образе deno нет), уронил бы старт службы — audio.clean_tmp удаляет файлы `unlink`-ом."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


async def _run(argv: list[str], timeout: float) -> "tuple[int, bytes, bytes]":
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, env=_env())
    except OSError:
        raise ToolError("чтение YouTube на сервере недоступно") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:                        # срок, «Стоп», остановка службы
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out, err


RUN: Callable[[list[str], float], Awaitable[tuple[int, bytes, bytes]]] = _run    # тесты подменяют


def youtube_id(url: str) -> "str | None":
    """id ролика из адреса YouTube (хвост запроса и фрагмент не важны); не разобралось — None."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = parts.hostname or ""                  # hostname — уже в нижнем регистре
    if host == "youtu.be":
        m = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parts.path)
    elif host in YOUTUBE_HOSTS:
        if parts.path.rstrip("/") == "/watch":
            v = (parse_qs(parts.query).get("v") or [""])[0]
            return v if _ID.fullmatch(v) else None
        m = _PATH_ID.fullmatch(parts.path)
    else:
        return None
    return m.group(1) if m else None


def pick_track(info: dict) -> "tuple[str, bool] | None":
    """(ключ дорожки, автоматическая): своя на языке ролика, затем автоматическая оригинала, затем любая
    своя; автопереводы (`en` у русского ролика) — нет."""
    lang = info.get("language")
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    if lang and lang in subs:
        return lang, False
    if lang and f"{lang}-orig" in auto:
        return f"{lang}-orig", True
    if lang and lang in auto:
        return lang, True
    orig = [k for k in auto if k.endswith("-orig")]
    if orig:
        return orig[0], True
    if subs:
        return next(iter(subs)), False
    return None


def _seconds(stamp: str) -> float:
    """«00:01:02.500» или «01:02.500» → секунды."""
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(stamp.split(":"))))


def _clock(sec: float) -> str:
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def vtt_text(vtt: str) -> str:
    """Текст субтитров: без заголовка, строк времени и тегов; строка, равная последней выведенной, —
    повтор «бегущих» автосубтитров; абзац с отметкой `[м:сс]` — не чаще раза в PARAGRAPH_SEC."""
    paragraphs, start, last, in_cue, cue = [], None, None, False, 0.0
    for line in vtt.splitlines():
        if "-->" in line:
            try:
                cue, in_cue = _seconds(line.split("-->")[0].strip()), True
            except ValueError:
                in_cue = False
        elif not line:                           # реплика кончается пустой строкой; « » — внутри неё
            in_cue = False
        elif in_cue:
            text = " ".join(html.unescape(_TAG.sub("", line)).split())
            if not text or text == last:
                continue
            last = text
            if start is None or cue >= start + PARAGRAPH_SEC:
                start = cue
                paragraphs.append([f"[{_clock(cue)}]"])
            paragraphs[-1].append(text)
    return "\n\n".join(" ".join(p) for p in paragraphs)


def _yt_words(err: bytes) -> str:
    text = err.decode("utf-8", "replace")
    if "HTTP Error 429" in text:
        return "YouTube временно ограничил запросы — попробуйте позже"
    if any(s in text for s in _YT_CLOSED):
        return "ролик закрыт или недоступен"
    return "субтитры не получены"


async def _yt(argv: list[str]) -> "tuple[int, bytes, bytes]":
    try:
        return await RUN(argv, YTDLP_TIMEOUT_SEC)
    except TimeoutError:
        raise ToolError(f"YouTube не ответил за {YTDLP_TIMEOUT_SEC} с") from None


async def youtube(url: str) -> dict:
    vid = youtube_id(url)
    if vid is None:
        raise ToolError("это не адрес ролика YouTube")
    canon = f"https://www.youtube.com/watch?v={vid}"
    base = [YTDLP, "--ignore-config", "--no-playlist", "--no-cache-dir"]
    folder, name = state.CFG.tmp_dir, uuid.uuid4().hex
    async with YT_SLOTS:
        try:
            code, out, err = await _yt([*base, "-J", canon])
            if code != 0:
                raise ToolError(_yt_words(err))
            try:                                 # -J — мегабайты (форматы, дорожки всех языков)
                info = await asyncio.to_thread(json.loads, out)
            except ValueError:
                info = None
            if not isinstance(info, dict):
                raise ToolError("субтитры не получены")
            track = pick_track(info)
            if track is None:
                raise ToolError("у ролика нет субтитров")
            key, auto = track
            code, _out, err = await _yt([*base, "--skip-download", "--write-auto-subs" if auto else "--write-subs",
                                         "--sub-langs", key, "--sub-format", "vtt",
                                         "-o", f"{folder}/{name}.%(ext)s", canon])
            path = Path(f"{folder}/{name}.{key}.vtt")
            if code != 0 or not path.is_file():
                raise ToolError(_yt_words(err))
            body = await asyncio.to_thread(lambda: vtt_text(path.read_text(encoding="utf-8", errors="replace")))
        finally:
            for f in folder.glob(f"{name}.*"):
                f.unlink(missing_ok=True)
    if len(body) > SUBS_MAX:
        body = body[:SUBS_MAX] + "\n\n[субтитры обрезаны]"
    title, channel = info.get("title") or "", info.get("channel") or info.get("uploader") or ""
    duration, lang = int(info.get("duration") or 0), key.removesuffix("-orig")
    return {"kind": "youtube", "id": vid, "url": canon, "title": title, "channel": channel,
            "duration": duration, "lang": lang,
            "text": f"{title} — {channel}, {_clock(duration)}, язык {lang}\n{canon}\n\n{body}"}


# ---- вызов инструмента моделью -----------------------------------------------------------

LABEL_MAX = 200
NO_KEY = "ключ Exa не читается — введите его заново в Настройках"
# к ошибке шага для модели: без него gemma-abl после «Exa недоступен» писала курс доллара «из интернета» в 12
# прогонах из 12, с ним — 0 из 12 (проба 2026-10-22)
NOTHING = ("Из интернета ничего не получено — не выдумывай данные и не пиши, что нашёл их; скажи пользователю, "
           "что не получилось.")


def args_of(arguments: str) -> "dict | None":
    """Аргументы вызова модели: JSON-объект; иначе None."""
    try:
        args = json.loads(arguments)
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def asked(kind: str, arguments: str) -> dict:
    """Что модель просила — `{query}` или `{url}` в пределах QUERY_MAX / URL_MAX — для шага без вызова
    (chat.TOO_MANY, chat.WEB_OFF); не разобралось — {}."""
    if kind not in _SPECS:
        return {}
    param = _SPECS[kind][2]
    value = (args_of(arguments) or {}).get(param)
    if not isinstance(value, str) or not value.strip():
        return {}
    return {param: value.strip()[:QUERY_MAX if kind == "search" else URL_MAX]}


def label(kind: str, args: dict) -> str:
    """Что показать, пока идёт инструмент: запрос, адрес или «субтитры <адрес>», до LABEL_MAX знаков."""
    value = args.get(_SPECS[kind][2]) if kind in _SPECS else None
    if not isinstance(value, str) or not value.strip():
        return ""
    value = " ".join(value.split())
    return (f"субтитры {value}" if kind == "youtube" else value)[:LABEL_MAX]


def for_model(step: dict) -> str:
    """Результат шага, как его получает модель (`role: tool`); ошибка — с запретом выдумывать (NOTHING)."""
    return f"Ошибка инструмента: {step['error']}\n{NOTHING}" if "error" in step else step["text"]


def history_block(steps: list[dict]) -> str:
    """Шаги прежнего ответа для следующих ходов: блоком `<web kind>` на шаг, текст — как его получила модель
    (for_model). Без ролей `tool` — шаблоны Gemma требуют чередования ролей (chat.compose)."""
    return "".join(f'<web kind="{s["kind"]}">\n{for_model(s)}\n</web>\n\n' for s in steps)


def public(step: dict) -> dict:
    """Шаг для страницы — без текста для модели."""
    return {k: v for k, v in step.items() if k != "text"}


# ---- какие адреса можно читать (read_page, youtube_subtitles) -----------------------------
# Страница уходит в Exa, а для незнакомого адреса Exa обходит сайт сам: адрес, который составила модель
# (по просьбе текста страницы или выдержки поиска), унёс бы переписку на чужой сайт. Читаются только адреса из
# переписки — сообщений пользователя и вложенных документов — и найденные поиском (`results[].url`). В Exa
# уходит адрес так, как он записан в переписке или выдаче (без фрагмента), а не вариант модели: иначе фрагмент,
# регистр букв или %-коды адреса стали бы каналом, которым модель вынесет кусок переписки на сайт.
# Ролик YouTube — так же: id из 11 знаков, составленный моделью, сервер отнёс бы YouTube запросом — кусок
# переписки в журнал чужого сервиса. Читается только ролик, чей id совпал с id адреса из переписки или выдачи
# (любая форма: watch, youtu.be, shorts, live, embed, music, m.); yt-dlp получает канонический адрес из id.

def _bare(url: str) -> str:
    return url.strip().split("#", 1)[0]


def norm(url: str) -> str:
    """Адрес для сравнения: без пробелов по краям, фрагмента, меток рекламы (`utm_*`, `fbclid`…) и одного «/» в
    конце; схема и хост — в нижнем регистре, %-коды раскрыты (модель пишет «/wiki/Москва» вместо
    «/wiki/%D0%9C…» и наоборот, снимает метки)."""
    url = _bare(url)
    head, sep, rest = url.partition("://")
    if sep:
        cut = min((i for i in (rest.find("/"), rest.find("?")) if i >= 0), default=len(rest))
        url = f"{head.lower()}://{rest[:cut].lower()}{rest[cut:]}"
    base, mark, query = url.partition("?")
    if mark:
        kept = [p for p in query.split("&") if p and not _TRACKING.fullmatch(p)]
        url = base + ("?" + "&".join(kept) if kept else "")
    url = unquote(url)
    return url[:-1] if url.endswith("/") else url


def links(text: str) -> dict[str, str]:
    """Адреса http(s) в тексте: norm → адрес, как записан (без фрагмента); хвостовая пунктуация («…/a).») — и с
    ней, и без неё: скобка бывает частью адреса."""
    out = {}
    for m in _LINK.finditer(text or ""):
        url = _bare(m.group(0))
        out.setdefault(norm(url), url)
        while url and url[-1] in _LINK_TAIL:
            url = url[:-1]
            out.setdefault(norm(url), url)
    return out


def found_links(step: dict) -> dict[str, str]:
    """Адреса результатов шага поиска: norm → адрес выдачи (без фрагмента)."""
    return {norm(r["url"]): _bare(r["url"]) for r in step.get("results") or []
            if isinstance(r, dict) and isinstance(r.get("url"), str)}


async def call(name: str, arguments: str, key: "str | None", kinds: "list[str] | None" = None,
               known: "dict[str, str] | None" = None) -> dict:
    """Шаг инструмента по вызову модели: имя функции и аргументы JSON. Неизвестное имя (или вид не из `kinds`,
    которые модели давали) и битые аргументы — шаг `unknown` с ошибкой; неудача инструмента — шаг с `error`.
    Запрос длиннее QUERY_MAX, адрес длиннее URL_MAX, страница не из `known` (norm → адрес переписки и поиска,
    links; None — без проверки) и ролик, чей id (youtube_id) не совпал с id ни одного адреса `known`, — шаг-ошибка
    без вызова. Страница читается по адресу из `known`, без него — по адресу модели без фрагмента.
    Не бросает ToolError (и сбой инструмента — тоже шаг); отмена («Стоп») идёт насквозь."""
    kind = NAMES.get(name)
    if kind is None or (kinds is not None and kind not in kinds):
        return {"kind": "unknown", "error": "модель вызвала инструмент, которого у неё нет"}
    args, param = args_of(arguments), _SPECS[kind][2]
    value = args.get(param) if args is not None else None
    if not isinstance(value, str) or not value.strip():
        return {"kind": "unknown", "error": f"модель вызвала {name} с неверными аргументами"}
    value = value.strip()
    if kind == "search" and len(value) > QUERY_MAX:
        return {"kind": kind, param: value[:QUERY_MAX], "error": "запрос слишком длинный"}
    if kind != "search" and len(value) > URL_MAX:
        return {"kind": kind, param: value[:URL_MAX], "error": "адрес слишком длинный"}
    if kind == "page":
        if known is not None and norm(value) not in known:
            return {"kind": kind, param: value, "error": NOT_KNOWN}
        value = known[norm(value)] if known is not None else _bare(value)
    if kind == "youtube" and known is not None:
        vid = youtube_id(value)
        if vid is not None and vid not in {youtube_id(url) for url in known.values()}:
            return {"kind": kind, param: value, "error": NOT_KNOWN}
    try:
        if kind == "youtube":
            return await youtube(value)
        if key is None:
            raise ToolError(NO_KEY)
        return await (search(key, value) if kind == "search" else page(key, value))
    except ToolError as e:
        return {"kind": kind, param: value, "error": e.args[0]}
    except Exception as e:                       # noqa: BLE001 — сбой инструмента не рвёт ответ
        return {"kind": kind, param: value, "error": f"сбой инструмента: {type(e).__name__}"}
