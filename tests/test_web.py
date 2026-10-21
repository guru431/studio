"""web.py — инструменты «Интернет»: поиск и страницы Exa, субтитры YouTube (спека 2026-10-21-studio-chat-internet,
6.4, 6.7)."""
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import httpx
import pytest

KEY = "exa-test-key"


def run(s, fn, *args):
    return s.c.portal.call(fn, *args)


def test_search_sends_only_the_query_and_the_key_header(studio, caplog):
    import web
    caplog.set_level(logging.DEBUG)
    step = run(studio, web.search, KEY, "последняя версия Python")
    assert studio.gw.exa_requests == [("/search", {"query": "последняя версия Python", "type": "auto",
                                                   "numResults": web.SEARCH_RESULTS,
                                                   "contents": {"highlights": True}}, KEY)]
    assert studio.gw.requests == []                      # шлюз srvllm не видел ни запроса, ни ключа
    assert step["kind"] == "search" and step["query"] == "последняя версия Python"
    assert "error" not in step
    sample = studio.gw.exa_replies["/search"][1]["results"]
    for res in sample:
        assert res["title"] in step["text"] and res["url"] in step["text"]
        for h in res["highlights"]:
            assert h in step["text"]
    first = sample[0]
    assert step["results"][0] == {"title": first["title"], "url": first["url"], "published": "2025-10-07"}
    assert step["text"].startswith("1. ")
    assert "2025-10-07" in step["text"]
    assert KEY not in caplog.text and "Python" not in caplog.text


def test_search_text_has_no_more_than_search_results(studio):
    import web
    many = [{"title": f"Заголовок {i}", "url": f"https://site{i}.test/", "highlights": [f"выдержка {i}"]}
            for i in range(web.SEARCH_RESULTS + 3)]
    studio.gw.exa_replies["/search"] = (200, {"results": many})
    step = run(studio, web.search, KEY, "что-то")
    assert len(step["results"]) == web.SEARCH_RESULTS
    assert f"{web.SEARCH_RESULTS}. Заголовок {web.SEARCH_RESULTS - 1}" in step["text"]
    assert f"Заголовок {web.SEARCH_RESULTS}" not in step["text"]


def test_page_reads_text_with_the_limit_in_the_request(studio):
    import web
    studio.gw.exa_page_text = "я" * (web.PAGE_MAX + 500)
    step = run(studio, web.page, KEY, "https://example.test/article")
    path, body, key = studio.gw.exa_requests[0]
    assert (path, body, key) == ("/contents", {"urls": ["https://example.test/article"],
                                               "text": {"maxCharacters": web.PAGE_MAX}}, KEY)
    assert step["kind"] == "page" and step["url"] == "https://example.test/article"
    assert step["title"] == "Страница example.test"
    head, _, text = step["text"].partition("\n\n")
    assert head == "Страница example.test\nhttps://example.test/article"
    assert text == "я" * web.PAGE_MAX


@pytest.mark.parametrize("url", ["ftp://example.test/file", "file:///etc/passwd", "javascript:alert(1)",
                                 "example.test/page", "https:///nohost", "http://[::1"])
def test_page_refuses_non_http_addresses_without_a_request(studio, url):
    import web
    with pytest.raises(web.ToolError) as e:
        run(studio, web.page, KEY, url)
    assert e.value.args[0] == "адрес страницы — только http или https"
    assert studio.gw.exa_requests == []


@pytest.mark.parametrize("tag, words", [("CRAWL_NOT_FOUND", "страница не найдена"),
                                        ("CRAWL_HTTP_403", "страница не прочиталась (CRAWL_HTTP_403)")])
def test_page_status_error_becomes_words(studio, tag, words):
    import web
    url = "https://example.test/missing"
    studio.gw.exa_replies["/contents"] = (200, {"requestId": "r", "results": [], "statuses": [
        {"id": url, "status": "error", "error": {"tag": tag, "httpStatusCode": 404}}]})
    with pytest.raises(web.ToolError) as e:
        run(studio, web.page, KEY, url)
    assert e.value.args[0] == words


@pytest.mark.parametrize("code, words", [
    (401, "ключ Exa не подходит — проверьте в Настройках"),
    (402, "Exa: бесплатный баланс исчерпан, обновится 1-го числа"),
    (429, "Exa: слишком часто — попробуйте позже"),
    (500, "Exa ответил 500"),
])
def test_exa_error_words(studio, code, words):
    import web
    assert web._exa_error(httpx.Response(code, json={"error": "x", "tag": "T"})) == words
    studio.gw.exa_replies["/search"] = (code, {"requestId": "r", "error": "x", "tag": "T"})
    with pytest.raises(web.ToolError) as e:
        run(studio, web.search, KEY, "запрос")
    assert e.value.args[0] == words


def test_cloudflare_html_block_is_exa_unreachable(studio):
    import web
    html = httpx.Response(403, text="<html>Sorry, you have been blocked</html>",
                          headers={"content-type": "text/html"})
    assert web._exa_error(html) == "Exa недоступен с сервера студии"
    studio.gw.exa_html = True
    for call in ((web.search, "запрос"), (web.page, "https://example.test/")):
        with pytest.raises(web.ToolError) as e:
            run(studio, call[0], KEY, call[1])
        assert e.value.args[0] == "Exa недоступен с сервера студии"


@pytest.mark.parametrize("exc, words", [(httpx.ReadTimeout, "Exa не ответил за 30 с"),
                                        (httpx.ConnectError, "Exa недоступен: ConnectError")])
def test_exa_timeout_and_network_words(studio, exc, words):
    import web
    studio.gw.exa_raise = exc
    with pytest.raises(web.ToolError) as e:
        run(studio, web.search, KEY, "запрос")
    assert e.value.args[0] == words


# --- YouTube (спека 6.4): адрес разбирает студия, yt-dlp получает только канонический ------------------

VID = "dM4yjJzDUvo"
CANON = f"https://www.youtube.com/watch?v={VID}"

# начало настоящих русских автосубтитров этого ролика (debian, yt-dlp 2026.8.19): «бегущие» строки и теги;
# «␠» — строка из одного пробела внутри реплики (редактор срезал бы пробел в конце строки)
AUTO_VTT = """WEBVTT
Kind: captions
Language: ru

00:00:00.920 --> 00:00:04.030 align:start position:0%
␠
Давайте<00:00:01.360><c> начнём,</c><00:00:02.120><c> друзья.</c><00:00:03.000><c> Начинаем</c><00:00:03.520><c> наш</c>

00:00:04.030 --> 00:00:04.040 align:start position:0%
Давайте начнём, друзья. Начинаем наш
␠

00:00:04.040 --> 00:00:06.150 align:start position:0%
Давайте начнём, друзья. Начинаем наш
прекрасный<00:00:04.800><c> интенсив.</c><00:00:05.680><c> Сегодня</c><00:00:06.040><c> он</c>

00:00:06.150 --> 00:00:06.160 align:start position:0%
прекрасный интенсив. Сегодня он
␠

00:00:31.000 --> 00:00:33.000 align:start position:0%
прекрасный интенсив. Сегодня он
будет<00:00:31.500><c> про</c><00:00:32.000><c> пунктуацию.</c>

00:00:33.000 --> 00:00:33.010 align:start position:0%
будет про пунктуацию.
␠
""".replace("␠", " ")

INFO = {"id": VID, "title": "Интенсив по русскому ЕГЭ", "channel": "Академия", "duration": 6602,
        "language": "ru", "subtitles": {}, "automatic_captions": {"ru-orig": [{}], "ru": [{}], "en": [{}]}}


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/watch?v={VID}&t=42s",
    f"https://youtube.com/watch?feature=share&v={VID}&list=PL123",
    f"http://m.youtube.com/watch?v={VID}#t=10",
    f"https://M.YouTube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}&si=abc",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=Xyz123",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/live/{VID}?si=abc",
    f"https://www.youtube.com/embed/{VID}",
    f"  https://youtu.be/{VID}  ",
])
def test_youtube_id_accepts_every_form(url):
    import web
    assert web.youtube_id(url) == VID


@pytest.mark.parametrize("url", [
    f"https://youtube.com.evil.test/watch?v={VID}",
    f"https://evilyoutube.com/watch?v={VID}",
    f"http://10.77.1.1/watch?v={VID}",
    "https://www.youtube.com/watch?v=dM4yjJzDUv",            # 10 знаков
    "https://www.youtube.com/watch?v=dM4yjJzDUvoX",          # 12 знаков
    "https://www.youtube.com/watch?v=dM4yjJz$Uvo",
    "https://www.youtube.com/watch?v=",
    "https://www.youtube.com/playlist?list=PLabcdefghijk",
    "https://www.youtube.com/watch?list=PLabcdefghijk",
    f"https://www.youtube.com/{VID}",
    f"https://www.youtube.com/shorts/{VID}/extra",
    f"https://youtu.be/{VID}/extra",
    f"ftp://www.youtube.com/watch?v={VID}",
    f"www.youtube.com/watch?v={VID}",
    f"file:///watch?v={VID}",
    "https://www.youtube.com/@channel",
    "http://[::1",
    "",
])
def test_youtube_id_refuses_everything_else(url):
    import web
    assert web.youtube_id(url) is None


def test_not_youtube_address_never_reaches_yt_dlp(studio, monkeypatch):
    import web
    calls = []

    async def fake(argv, timeout):
        calls.append(argv)
        return 0, b"{}", b""

    monkeypatch.setattr(web, "RUN", fake)
    for url in (f"https://youtube.com.evil.test/watch?v={VID}", "https://example.test/video",
                "https://www.youtube.com/playlist?list=PLabcdefghijk"):
        with pytest.raises(web.ToolError):
            run(studio, web.youtube, url)
    assert calls == []


@pytest.mark.parametrize("info, track", [
    ({"language": "ru", "subtitles": {"ru": [], "en": []}, "automatic_captions": {"ru-orig": [], "ru": []}},
     ("ru", False)),
    ({"language": "ru", "subtitles": {"en": []}, "automatic_captions": {"en": [], "ru": [], "ru-orig": []}},
     ("ru-orig", True)),
    ({"language": "ru", "subtitles": {}, "automatic_captions": {"en": [], "ru": []}}, ("ru", True)),
    ({"language": None, "subtitles": {}, "automatic_captions": {"de": [], "en-orig": [], "en": []}},
     ("en-orig", True)),
    ({"language": None, "subtitles": {"en": [], "de": []}, "automatic_captions": {}}, ("en", False)),
    ({"language": None, "subtitles": {"live_chat": []}, "automatic_captions": {}}, None),
    ({"language": None, "subtitles": {}, "automatic_captions": {}}, None),
    ({}, None),
])
def test_pick_track(info, track):
    import web
    assert web.pick_track(info) == track


def test_vtt_text_drops_markup_and_rolling_repeats_and_marks_paragraphs():
    import web
    text = web.vtt_text(AUTO_VTT)
    assert text == ("[0:00] Давайте начнём, друзья. Начинаем наш прекрасный интенсив. Сегодня он\n\n"
                    "[0:31] будет про пунктуацию.")


def test_vtt_text_keeps_both_lines_of_a_manual_cue_and_hour_marks():
    import web
    vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n"
           "00:00:27.103 --> 00:00:29.678\nGood morning. How are you?\n\n"
           "00:00:43.096 --> 00:00:46.663\nThere have been three themes\nrunning &amp; running\n\n"
           "3\n00:00:57.200 --> 00:00:58.000\nNext.\n\n"
           "01:02:03.000 --> 01:02:05.000\n<i>The end</i>\n")
    assert web.vtt_text(vtt) == ("[0:27] Good morning. How are you? There have been three themes running & running"
                                 "\n\n[0:57] Next.\n\n[1:02:03] The end")


class FakeYtDlp:
    """Поддельный web.RUN: -J отдаёт info, второй вызов пишет файл дорожки по шаблону -o."""

    def __init__(self, info=INFO, vtt=AUTO_VTT, fail=None):
        self.info, self.vtt, self.fail, self.calls = info, vtt, fail, []

    async def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        if self.fail and len(self.calls) == self.fail[0]:
            if isinstance(self.fail[1], BaseException):
                raise self.fail[1]
            return 1, b"", self.fail[1]
        if "-J" in argv:
            return 0, json.dumps(self.info).encode(), b"WARNING: impersonation"
        out = argv[argv.index("-o") + 1]
        key = argv[argv.index("--sub-langs") + 1]
        if self.vtt is not None:
            Path(out.replace("%(ext)s", f"{key}.vtt")).write_text(self.vtt, encoding="utf-8")
            Path(out.replace("%(ext)s", "part-left")).write_text("x", encoding="utf-8")
        return 0, b"", b"WARNING: impersonation"


def _tmp_files():
    import state
    return sorted(p.name for p in state.CFG.tmp_dir.iterdir()) if state.CFG.tmp_dir.exists() else []


def test_youtube_reads_the_original_track_with_canonical_argv(studio, monkeypatch, caplog):
    import state
    import web
    caplog.set_level(logging.DEBUG)
    fake = FakeYtDlp()
    monkeypatch.setattr(web, "RUN", fake)
    step = run(studio, web.youtube, f"https://youtu.be/{VID}?si=Tracking123")
    (meta, t1), (subs, t2) = fake.calls
    assert t1 == t2 == web.YTDLP_TIMEOUT_SEC
    assert meta == [web.YTDLP, "--ignore-config", "--no-playlist", "--no-cache-dir", "-J", CANON]
    out = subs[subs.index("-o") + 1]
    assert subs == [web.YTDLP, "--ignore-config", "--no-playlist", "--no-cache-dir", "--skip-download",
                    "--write-auto-subs", "--sub-langs", "ru-orig", "--sub-format", "vtt", "-o", out, CANON]
    folder, name = Path(out).parent, Path(out).name
    assert folder == state.CFG.tmp_dir and re.fullmatch(r"[0-9a-f]{32}\.%\(ext\)s", name)
    assert step == {"kind": "youtube", "id": VID, "url": CANON, "title": "Интенсив по русскому ЕГЭ",
                    "channel": "Академия", "duration": 6602, "lang": "ru", "text": step["text"]}
    assert step["text"] == ("Интенсив по русскому ЕГЭ — Академия, 1:50:02, язык ru\n" + CANON + "\n\n"
                            + web.vtt_text(AUTO_VTT))
    assert _tmp_files() == []
    assert "Tracking123" not in caplog.text and "Давайте" not in caplog.text


def test_youtube_own_subtitles_use_write_subs(studio, monkeypatch):
    import web
    info = dict(INFO, language="en", subtitles={"en": [{}], "de": [{}]}, duration=75)
    fake = FakeYtDlp(info=info)
    monkeypatch.setattr(web, "RUN", fake)
    step = run(studio, web.youtube, CANON)
    subs = fake.calls[1][0]
    assert "--write-subs" in subs and "--write-auto-subs" not in subs
    assert subs[subs.index("--sub-langs") + 1] == "en"
    assert step["lang"] == "en"
    assert step["text"].startswith("Интенсив по русскому ЕГЭ — Академия, 1:15, язык en\n")


def test_youtube_long_subtitles_are_cut_with_a_note(studio, monkeypatch):
    import web
    monkeypatch.setattr(web, "SUBS_MAX", 50)
    monkeypatch.setattr(web, "RUN", FakeYtDlp())
    step = run(studio, web.youtube, CANON)
    body = step["text"].split("\n\n", 1)[1]
    assert body == web.vtt_text(AUTO_VTT)[:50] + "\n\n[субтитры обрезаны]"


def test_youtube_without_a_track_says_so_and_downloads_nothing(studio, monkeypatch):
    import web
    fake = FakeYtDlp(info=dict(INFO, language=None, subtitles={"live_chat": [{}]}, automatic_captions={}))
    monkeypatch.setattr(web, "RUN", fake)
    with pytest.raises(web.ToolError) as e:
        run(studio, web.youtube, CANON)
    assert e.value.args[0] == "у ролика нет субтитров"
    assert len(fake.calls) == 1


STDERR_429 = b"ERROR: Unable to download video subtitles for 'ru-orig': HTTP Error 429: Too Many Requests"


@pytest.mark.parametrize("fail, words", [
    ((1, b"ERROR: [youtube] x: Private video. Sign in if you've been granted access"),
     "ролик закрыт или недоступен"),
    ((1, b"ERROR: [youtube] x: Sign in to confirm your age. Use --cookies"), "ролик закрыт или недоступен"),
    ((1, b"ERROR: [youtube] x: This video is unavailable"), "ролик закрыт или недоступен"),
    ((1, b"ERROR: [youtube] x: This video has been removed for violating YouTube's policy"),
     "ролик закрыт или недоступен"),
    ((1, b"ERROR: [youtube] x: HTTP Error 429: Too Many Requests"),
     "YouTube временно ограничил запросы — попробуйте позже"),
    ((2, STDERR_429), "YouTube временно ограничил запросы — попробуйте позже"),
    ((2, b"ERROR: something else entirely"), "субтитры не получены"),
    ((1, TimeoutError()), "YouTube не ответил за 60 с"),
    ((2, TimeoutError()), "YouTube не ответил за 60 с"),
])
def test_youtube_errors_become_words_and_leave_no_files(studio, monkeypatch, caplog, fail, words):
    import web
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(web, "RUN", FakeYtDlp(fail=fail))
    with pytest.raises(web.ToolError) as e:
        run(studio, web.youtube, CANON)
    assert e.value.args[0] == words
    assert _tmp_files() == []
    assert "ERROR" not in caplog.text and "Sign in" not in caplog.text


def test_youtube_track_file_missing_is_not_received(studio, monkeypatch):
    import web
    monkeypatch.setattr(web, "RUN", FakeYtDlp(vtt=None))
    with pytest.raises(web.ToolError) as e:
        run(studio, web.youtube, CANON)
    assert e.value.args[0] == "субтитры не получены"


def test_youtube_cancel_removes_the_written_files(studio, monkeypatch):
    import web
    fake = FakeYtDlp()

    async def cancelled_after_writing(argv, timeout):
        result = await fake(argv, timeout)
        if "-J" not in argv:
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(web, "RUN", cancelled_after_writing)
    with pytest.raises(BaseException):
        run(studio, web.youtube, CANON)
    assert _tmp_files() == []


def test_run_without_yt_dlp_on_the_host_is_words(studio, monkeypatch):
    import web
    monkeypatch.setattr(web, "YTDLP", str(Path(__file__).parent / "no-such-yt-dlp"))
    with pytest.raises(web.ToolError) as e:
        run(studio, web.youtube, CANON)
    assert e.value.args[0] == "чтение YouTube на сервере недоступно"


def test_run_kills_the_process_after_the_deadline(studio):
    import web
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run(studio, web.RUN, [sys.executable, "-c", "import time; time.sleep(20)"], 0.3)
    assert time.monotonic() - started < 5


def test_run_returns_code_and_both_streams(studio):
    import web
    code, out, err = run(studio, web.RUN, [sys.executable, "-c",
                                           "import sys; print('o'); print('e', file=sys.stderr); sys.exit(3)"], 10)
    assert (code, out.strip(), err.strip()) == (3, b"o", b"e")
