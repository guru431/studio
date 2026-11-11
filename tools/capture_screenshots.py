"""Capture the real Studio UI with isolated, synthetic data for the GitHub READMEs.

Run with the project's development environment. Uses the existing browser test
stand; never opens a production database, connects a real agent or calls a model.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "tests")]

import db
from browser import find_browser
from fake_bridge import FakeMachine, pid
from live_app import PASSWORD, start_live_app
from playwright.sync_api import sync_playwright
from test_ui_browser import code_online

CHAT = "11111111-1111-4111-8111-111111111111"
AGENT_CHAT = "22222222-2222-4222-8222-222222222222"
TRANSLATION = "33333333-3333-4333-8333-333333333333"
SCAN = "44444444-4444-4444-8444-444444444444"
PROJECT = "demo-workspace"
READY = "() => document.documentElement.dataset.ready === '1'"


def seed(live, document: bytes) -> None:
    """Populate only the temporary database owned by this invocation."""
    now = time.time()
    with closing(db.connect(live.db_path)) as conn:
        uid = conn.execute("SELECT id FROM users WHERE login='owner'").fetchone()[0]
        cid = conn.execute("SELECT id FROM connections WHERE user_id=?", (uid,)).fetchone()[0]
        conn.execute("UPDATE users SET name='Demo', theme='light' WHERE id=?", (uid,))
        conn.execute("UPDATE connections SET name='Local AI' WHERE id=?", (cid,))
        conn.execute("INSERT INTO chats(id,user_id,title,connection_id,model,created,updated)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (CHAT, uid, "Идея для нового проекта", cid, "local-gemma-abl", now, now))
        messages = [
            ("user", "Помоги придумать небольшое приложение для домашней библиотеки. "
             "Хочу хранить список книг, отмечать прочитанное и быстро находить нужное."),
            ("assistant", "## Домашняя библиотека\n\nНачните с трёх простых экранов:\n\n"
             "| Экран | Что на нём |\n| --- | --- |\n"
             "| Книги | Обложка, автор, название и статус чтения |\n"
             "| Поиск | Фильтры по автору, жанру и вашим заметкам |\n"
             "| Моя полка | Читаю сейчас, прочитано и планы |\n\n"
             "**Для первой версии:** добавление книги, поиск и короткая личная заметка. "
             "Затем можно добавить сканирование штрихкода и семейные подборки.\n\n"
             "```python\nclass Book:\n    title: str\n    author: str\n    status: str = 'to_read'\n```"),
        ]
        for i, (role, content) in enumerate(messages):
            conn.execute("INSERT INTO messages(id,chat_id,role,content,status,via,model,created)"
                         " VALUES (?,?,?,?,'done','server',?,?)",
                         (f"demo-message-{i}", CHAT, role, content, "local-gemma-abl", now + i))
        source = ("A place for every idea.\n\nBring your notes, conversations and creative projects "
                  "together in one workspace. Start on your computer and pick up where you left "
                  "off on your phone.\n\nYour library stays with you: search it, organize it and "
                  "share only what you choose.")
        translated = ("Место для каждой идеи.\n\nСоберите заметки, разговоры и творческие проекты "
                      "в одном рабочем пространстве. Начните на компьютере и продолжите с того же "
                      "места на телефоне.\n\nВаша библиотека всегда под рукой: ищите, наводите "
                      "порядок и делитесь только тем, что выбрали сами.")
        conn.execute("INSERT INTO translations(id,user_id,kind,connection_id,model,source_lang,"
                     "target_lang,detected_lang,result_lang,name,source,text,parts,done_parts,"
                     "state,detail,created,finished) VALUES (?,?,'text',?,?,'en','ru','en','ru',"
                     "'Welcome',?,?,1,1,'done','',?,?)",
                     (TRANSLATION, uid, cid, "local-gemma-abl", source, translated, now, now))
        conn.execute("INSERT INTO translation_parts(translation_id,n,source,text,state)"
                     " VALUES (?,1,?,?,'done')", (TRANSLATION, source, translated))
        media_dir = live.db_path.parent / "media"
        (media_dir / "demo-document.png").write_bytes(document)
        conn.execute("INSERT INTO media(id,user_id,rel_path,mime,size,has_thumb,download_name,created)"
                     " VALUES ('demo-scan-media',?,'demo-document.png','image/png',?,0,'Project brief.png',?)",
                     (uid, len(document), now))
        text = ("# План проекта\n\n## Домашняя библиотека\n\n"
                "Цель: все книги и заметки в одном месте.\n\n"
                "| Этап | Результат |\n| --- | --- |\n"
                "| 01 | Каталог книг |\n| 02 | Поиск и фильтры |\n| 03 | Заметки и подборки |\n\n"
                "**Первая версия:** добавить книгу, найти её и сохранить заметку.\n\n"
                "Демонстрационный документ · Studio")
        conn.execute("INSERT INTO scans(id,user_id,connection_id,model,name,source_media_id,origin,"
                     "pages,done_pages,state,detail,text,created,finished)"
                     " VALUES (?,?,?,'hunyuan-ocr','План проекта.png','demo-scan-media','section',"
                     "1,1,'done','',?,?,?)", (SCAN, uid, cid, text, now, now))
        conn.execute("INSERT INTO scan_pages(scan_id,user_id,n,media_id,state,text)"
                     " VALUES (?,?,1,'demo-scan-media','done',?)", (SCAN, uid, text))


def demo_machine() -> FakeMachine:
    now = time.time()
    messages = [
        {"id": "a1", "role": "user", "time": now, "steps": [],
         "text": "Добавь поиск книг по названию и автору. Покажи изменения и проверь тесты."},
        {"id": "a2", "role": "assistant", "time": now + 1,
         "text": "Добавил поиск в каталоге. Запрос ищет по названию и автору, "
                 "не зависит от регистра и убирает лишние пробелы.\n\n"
                 "```python\ndef find_books(books, query):\n    query = query.strip().casefold()\n"
                 "    return [book for book in books\n"
                 "            if query in f'{book.title} {book.author}'.casefold()]\n```\n\n"
                 "**Проверка:** все 12 тестов прошли. Добавлены случаи для пустого запроса, "
                 "названия и имени автора.\n\nИзменения готовы к просмотру в Git.",
         "steps": [{"id": "s1", "tool": "Read", "input": {"file_path": "src/library.py"},
                    "text": "Read the catalog module.", "error": False},
                   {"id": "s2", "tool": "Edit", "input": {"file_path": "src/library.py"},
                    "text": "Added title and author search.", "error": False},
                   {"id": "s3", "tool": "Bash", "input": {"command": "python -m pytest -q"},
                    "text": "12 passed in 0.18s", "error": False}]},
    ]
    machine = FakeMachine({PROJECT: {
        "chats": {AGENT_CHAT: {"title": "Поиск по библиотеке", "created": now, "modified": now,
                                "messages": messages, "tag": "studio"}},
        "files": {"README.md": b"# Home Library\n\nA small, searchable personal library.\n\n"
                  b"## Features\n\n- Book catalog\n- Search by title and author\n- Reading notes\n",
                  "src/library.py": b"def find_books(books, query):\n    query = query.strip().casefold()\n"
                  b"    return [b for b in books if query in f'{b.title} {b.author}'.casefold()]\n",
                  "tests/test_library.py": b"from src.library import find_books\n\n"
                  b"def test_empty_catalog():\n    assert find_books([], 'Dune') == []\n"},
        "git": {"branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0,
                "files": [{"path": "src/library.py", "status": " M"},
                          {"path": "tests/test_library.py", "status": " M"}]},
    }})
    machine.project_summary = True
    machine.project_activity = True
    machine.passive_requests = True
    machine.file_operations = True
    machine.now = now
    machine.projects[PROJECT]["summary"] = {
        "findings": 0, "ideas": 2, "changed": 2, "unpushed": 0,
        "modified": now, "checked_at": now, "git": "ok"}
    machine.codex = {"version": "1.0", "models": [{"id": "gpt-5", "label": "GPT-5"}],
                     "efforts": ["low", "high"]}
    machine.opencode = {"version": "1.0", "models": [{"id": "demo", "label": "Demo model"}],
                        "efforts": ["low", "high"]}
    return machine


def capture(output: Path) -> None:
    executable = find_browser()
    if not executable:
        raise RuntimeError("Install Edge or Chromium before capturing screenshots.")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="studio-screenshots-") as folder, ExitStack() as stack:
        live = start_live_app(Path(folder))
        stack.callback(live.stop)
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch(executable_path=executable, headless=True,
                                     args=["--no-proxy-server", "--disable-background-networking"])
        stack.callback(browser.close)
        # Render an original demonstration document, rather than using a private scan.
        doc = browser.new_page(viewport={"width": 700, "height": 840}, device_scale_factor=1)
        doc.set_content("""<!doctype html><html lang="ru"><meta charset="utf-8">
            <style>body{padding:65px;color:#253239;font:22px/1.6 Georgia;background:#fff}
            h1{font-size:40px}h2{font-size:28px;color:#c45100}table{width:100%;border-collapse:collapse}
            td,th{text-align:left;padding:14px 0;border-bottom:1px solid #ddd}
            footer{margin-top:65px;color:#777;font:16px Arial}</style>
            <h1>План проекта</h1><h2>Домашняя библиотека</h2>
            <p>Цель: все книги и заметки в одном месте.</p>
            <table><tr><th>Этап</th><th>Результат</th></tr><tr><td>01</td><td>Каталог книг</td></tr>
            <tr><td>02</td><td>Поиск и фильтры</td></tr><tr><td>03</td><td>Заметки и подборки</td></tr></table>
            <p><b>Первая версия:</b> добавить книгу, найти её и сохранить заметку.</p>
            <footer>Демонстрационный документ · Studio</footer></html>""")
        seed(live, doc.screenshot())
        doc.close()
        aid, bridge = code_online(live, "owner", demo_machine(), name="Demo PC")
        stack.callback(bridge.stop)
        manifest = []
        for mobile in (False, True):
            live.set_theme("owner", "light")
            width, height = (390, 844) if mobile else (1440, 1000)
            context = browser.new_context(viewport={"width": width, "height": height},
                                          device_scale_factor=1, is_mobile=mobile, has_touch=mobile,
                                          service_workers="block", color_scheme="light")
            stack.callback(context.close)
            # All web traffic stays on the local test stand, including accidental external links.
            context.route("**/*", lambda route: route.continue_()
                          if route.request.url.startswith((live.url + "/", "data:", "blob:"))
                          else route.abort())
            response = context.request.post(live.url + "/login",
                                             form={"login": "owner", "password": PASSWORD}, max_redirects=0)
            if response.status != 303:
                raise RuntimeError(f"Demo login failed ({response.status}).")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            agent_route = f"code/{aid}/{pid(PROJECT)}/{AGENT_CHAT}"
            shots = [("chat", f"chat/{CHAT}", ".msg"),
                     ("agents", agent_route, "#code-chat[data-loaded='1']")]
            if not mobile:
                shots += [("files", f"code/{aid}/{pid(PROJECT)}?tab=files", "#code-files"),
                          ("images", "photo", "#view textarea"),
                          ("translation", f"translate?id={TRANSLATION}", "#tl-output"),
                          ("ocr", f"ocr?id={SCAN}", ".ocr-text"),
                          ("appearance", "settings/appearance", "#view h1"),
                          ("agents-dark", agent_route, "#code-chat[data-loaded='1']")]
            for name, route, selector in shots:
                if name == "agents-dark":
                    live.set_theme("owner", "dark")
                page.goto(live.url + "/#/" + route)
                if name == "agents-dark":
                    # A hash-only navigation retains the old account profile in the page.
                    page.reload()
                page.wait_for_function(READY)
                expected_theme = "dark" if name == "agents-dark" else "light"
                page.wait_for_function("theme => document.documentElement.dataset.theme === theme",
                                       arg=expected_theme)
                page.locator(selector).first.wait_for(state="visible")
                if name == "translation":
                    page.wait_for_function("() => document.querySelector('#tl-output')?.textContent.includes('Место')")
                if name == "ocr":
                    page.wait_for_function("() => document.querySelector('img.ocr-scan')?.naturalWidth > 0")
                if name == "images":
                    page.locator("#view textarea").fill("Уютная домашняя библиотека, мягкий свет из окна, "
                                                       "деревянные полки, иллюстрация акварелью")
                if name == "files":
                    page.get_by_text("README.md", exact=True).click()
                    page.locator("#code-file h1").wait_for(state="visible")
                if mobile and name == "chat":
                    page.locator("#messages").evaluate("node => { node.scrollTop = 0; }")
                page.evaluate("() => document.fonts.ready")
                page.wait_for_timeout(400)
                if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                    raise RuntimeError(f"Horizontal overflow: {name}, {width}px")
                if errors:
                    raise RuntimeError(f"Browser errors in {name}: {errors}")
                filename = f"{name}{'-mobile' if mobile else ''}.png"
                page.screenshot(path=str(output / filename), animations="disabled")
                manifest.append({"file": filename, "viewport": [width, height],
                                 "theme": "dark" if name == "agents-dark" else "light"})
                print(f"Captured {filename}", flush=True)
            context.close()
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "screenshots")
    capture(parser.parse_args().output.resolve())
