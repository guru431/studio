"""web.py — инструменты «Интернет»: поиск и страницы Exa (спека 2026-10-21-studio-chat-internet, 6.4, 6.7)."""
import logging

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
