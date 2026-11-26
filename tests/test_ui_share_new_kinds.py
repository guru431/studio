"""«Поделиться» чатом «Озвучки» и перепиской Open WebUI со страниц разделов (п. 42 спеки
2026-11-25-studio-skills-status-local-models): `#sp-share` — пока в чате ничего не идёт, страница получателя — записи
с «Слушать»; меню `#owui-share` в шапке ленты Open WebUI — ссылка и экспорт."""
import pytest

import db
import media
from fake_owui import FakeOwui  # noqa: F401
from test_ui_browser import live  # noqa: F401 — модульная живая служба
from test_ui_owui import connected, fake, fits, opened, owui_browser, session, talk, toolbar_button  # noqa: F401
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def seed_speech(live, login):
    """Чат `<login>-run` с идущей записью и чат `<login>-sc` с двумя готовыми mp3."""
    conn = db.connect(live.db_path)
    try:
        uid = conn.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()[0]
        one = media.save(uid, b"ID3 one", "audio/mpeg", "one.mp3")
        two = media.save(uid, b"ID3 two", "audio/mpeg", "two.mp3")
        rows = [(f"{login}-1", f"{login}-sc", "Первая озвучка", one, "done", 1),
                (f"{login}-2", f"{login}-sc", "Вторая озвучка", two, "done", 2),
                (f"{login}-3", f"{login}-run", "Готовая", one, "done", 3),
                (f"{login}-4", f"{login}-run", "Идёт", None, "working", 4)]
        conn.executemany(
            "INSERT INTO speeches(id, user_id, model, voice, voice_name, text, media_id, duration, state, detail, created,"
            " chat_id, chat_created) VALUES (?, ?, 'qwen3-tts', 'v1', 'Анна', ?, ?, 1.5, ?, '', ?, ?, 0)",
            [(sid, uid, text, mid, st, created, cid) for sid, cid, text, mid, st, created in rows])
    finally:
        conn.close()
    return f"{login}-sc", f"{login}-run"


@pytest.mark.parametrize("width", [320, 1280])
def test_speech_chat_and_open_webui_are_shared_from_their_sections(owui_browser, live, fake, width):
    login = f"share-kinds-{owui_browser.browser_type.name}-{width}"
    cookie = session(live, login)
    ready, running = seed_speech(live, login)
    with opened(owui_browser, live, cookie, width, f"speech?chat={running}") as page:
        pw.expect(page.locator("#sp-delete-chat")).to_be_visible()
        pw.expect(page.locator("#sp-share")).to_be_hidden()                  # идёт запись — ссылки нет
        page.goto(live.url + f"/#/speech?chat={ready}")
        share = page.locator("#sp-share")
        pw.expect(share).to_be_visible()
        share.click()
        pw.expect(page.locator("#chat-share-dialog-title")).to_have_text("Поделиться чатом озвучки")
        page.locator("#chat-share-pin").fill("0123")
        page.locator("#chat-share-create").click()
        link = page.locator("#chat-share-link")
        pw.expect(link).to_be_visible()
        url = link.input_value()
        assert url.startswith(live.url + "/share#") and fits(page)
        assert not page.problems, page.problems
    with owui_browser.new_context(viewport={"width": width, "height": 800}, locale="en-US") as reader_context:
        reader = reader_context.new_page()
        reader.set_default_timeout(10000)
        reader.goto(url)
        reader.locator("#share-pin").fill("0123")
        reader.locator("#share-open").click()
        pw.expect(reader.locator("#shared-kind")).to_have_text("Read only · speech chat")   # получатель — по-английски (Review Focus 5)
        pw.expect(reader.locator("#shared-title")).to_have_text("Первая озвучка")
        records = reader.locator(".shared-record")
        pw.expect(records).to_have_count(2)
        pw.expect(records.nth(1)).to_contain_text("Вторая озвучка")
        pw.expect(records.nth(0)).to_contain_text("Voice: Анна")
        records.nth(0).locator(".shared-listen").click()
        pw.expect(records.nth(0).locator("audio.shared-audio")).to_have_count(1)
        assert reader.evaluate("document.documentElement.scrollWidth <= innerWidth")

    fake.add_model("m1", "Model One")
    chat = fake.add_chat("Boats", models=["m1"], messages=talk({"id": "a1", "done": True, "content": "",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi there"}]}]}))
    aid = connected(live, cookie)
    with opened(owui_browser, live, cookie, width, f"owui/{aid}/{chat}") as page:
        pw.expect(page.locator(".owui-feed-title")).to_have_text("Boats")
        trigger = toolbar_button(page, "#owui-share")
        pw.expect(trigger).to_be_enabled()
        trigger.click()
        items = page.locator("#owui-share-menu [data-action]:visible")
        pw.expect(items).to_have_text(["Поделиться ссылкой", "Скачать PDF", "Скачать выделенное в PDF", "Скачать Markdown"])
        page.locator("#owui-share-menu [data-action=link]").click()
        pw.expect(page.locator("#chat-share-dialog-title")).to_have_text("Поделиться чатом Open WebUI")
        page.locator("#chat-share-pin").fill("4567")
        page.locator("#chat-share-create").click()
        link = page.locator("#chat-share-link")
        pw.expect(link).to_be_visible()
        assert link.input_value().startswith(live.url + "/share#") and fits(page)
        assert not page.problems, page.problems
