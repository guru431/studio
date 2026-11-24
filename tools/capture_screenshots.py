"""Capture the real Studio UI with isolated, synthetic data for the GitHub READMEs.

Run with the project's development environment. Uses the existing browser test
stand; never opens a production database, connects a real agent or calls a model.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "tests"), str(ROOT)]

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
LANGUAGE = "en"
THEME = "dark"
BRIEF = (
    "Project brief",
    "Home Library",
    "Keep books and reading notes in one place.",
    (("01", "Book catalog"), ("02", "Search and filters"), ("03", "Notes and collections")),
    "Add a book, find it and save a note.",
)


def seed(live, document: bytes) -> None:
    """Populate only the temporary database owned by this invocation."""
    now = time.time()
    with closing(db.connect(live.db_path)) as conn:
        uid = conn.execute("SELECT id FROM users WHERE login='owner'").fetchone()[0]
        cid = conn.execute("SELECT id FROM connections WHERE user_id=?", (uid,)).fetchone()[0]
        conn.execute("UPDATE users SET name='Demo', theme=? WHERE id=?", (THEME, uid))
        conn.execute("UPDATE connections SET name='Local AI' WHERE id=?", (cid,))
        conn.execute("INSERT INTO chats(id,user_id,title,connection_id,model,created,updated)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (CHAT, uid, "Plan a home library app", cid, "local-gemma-abl", now, now))
        messages = [
            ("user", "Help me plan a small home library app. I want to catalog my books, "
             "track what I have read and find any title quickly."),
            ("assistant", "## Home Library\n\nStart with three screens.\n\n"
             "| Screen | What it shows |\n| --- | --- |\n"
             "| Books | Cover, author, title and reading status |\n"
             "| Search | Filters for authors, genres and notes |\n"
             "| My shelf | Reading, finished and to read |\n\n"
             "**First release** — Add a book, search the catalog and save a reading note. "
             "Barcode scanning and shared shelves can follow.\n\n"
             "```python\nclass Book:\n    title: str\n    author: str\n    status: str = 'to_read'\n```"),
        ]
        for i, (role, content) in enumerate(messages):
            conn.execute("INSERT INTO messages(id,chat_id,role,content,status,via,model,created)"
                         " VALUES (?,?,?,?,'done','server',?,?)",
                         (f"demo-message-{i}", CHAT, role, content, "local-gemma-abl", now + i))
        source = ("Une bibliothèque à portée de main.\n\nRetrouvez vos livres et vos notes au même endroit. "
                  "Commencez sur votre ordinateur et reprenez là où vous en étiez sur votre téléphone.")
        translated = ("Your library at your fingertips.\n\nFind your books and notes in one place. "
                      "Start on your computer and pick up where you left off on your phone.")
        conn.execute("INSERT INTO translations(id,user_id,kind,connection_id,model,source_lang,"
                     "target_lang,detected_lang,result_lang,name,source,text,parts,done_parts,"
                     "state,detail,created,finished) VALUES (?,?,'text',?,?,'fr','en','fr','en',"
                     "'Library introduction',?,?,1,1,'done','',?,?)",
                     (TRANSLATION, uid, cid, "local-gemma-abl", source, translated, now, now))
        conn.execute("INSERT INTO translation_parts(translation_id,n,source,text,state)"
                     " VALUES (?,1,?,?,'done')", (TRANSLATION, source, translated))
        media_dir = live.db_path.parent / "media"
        (media_dir / "demo-document.png").write_bytes(document)
        conn.execute("INSERT INTO media(id,user_id,rel_path,mime,size,has_thumb,download_name,created)"
                     " VALUES ('demo-scan-media',?,'demo-document.png','image/png',?,0,'Project brief.png',?)",
                     (uid, len(document), now))
        title, subtitle, goal, milestones, first_release = BRIEF
        rows = "\n".join(f"| {step} | {result} |" for step, result in milestones)
        text = (f"# {title}\n\n## {subtitle}\n\n{goal}\n\n"
                f"| Milestone | Result |\n| --- | --- |\n{rows}\n\n"
                f"**First release** — {first_release}")
        conn.execute("INSERT INTO scans(id,user_id,connection_id,model,name,source_media_id,origin,"
                     "pages,done_pages,state,detail,text,created,finished)"
                     " VALUES (?,?,?,'hunyuan-ocr','Project brief.png','demo-scan-media','section',"
                     "1,1,'done','',?,?,?)", (SCAN, uid, cid, text, now, now))
        conn.execute("INSERT INTO scan_pages(scan_id,user_id,n,media_id,state,text)"
                     " VALUES (?,?,1,'demo-scan-media','done',?)", (SCAN, uid, text))


def demo_machine() -> FakeMachine:
    now = time.time()
    messages = [
        {"id": "a1", "role": "user", "time": now, "steps": [],
         "text": "Add book search by title and author. Show the changes and run the tests."},
        {"id": "a2", "role": "assistant", "time": now + 1,
         "text": "The catalog now searches both titles and authors. "
                 "Search ignores case and trims whitespace.\n\n"
                 "```python\ndef find_books(books, query):\n    query = query.strip().casefold()\n"
                 "    return [book for book in books\n"
                 "            if query in f'{book.title} {book.author}'.casefold()]\n```\n\n"
                 "**Tests** — All 12 passed, including the new cases for an empty query, "
                 "a title and an author's name. The changes are ready for Git review.",
         "steps": [{"id": "s1", "tool": "Read", "input": {"file_path": "src/library.py"},
                    "text": "Read the catalog module.", "error": False},
                   {"id": "s2", "tool": "Edit", "input": {"file_path": "src/library.py"},
                    "text": "Added title and author search.", "error": False},
                   {"id": "s3", "tool": "Bash", "input": {"command": "python -m pytest -q"},
                    "text": "12 passed in 0.18s", "error": False}]},
    ]
    machine = FakeMachine({PROJECT: {
        "chats": {AGENT_CHAT: {"title": "Add catalog search", "created": now, "modified": now,
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
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="studio-screenshots-", dir=scratch) as folder, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, dict.fromkeys(("TEMP", "TMP", "TMPDIR"), folder)))
        live = start_live_app(Path(folder))
        stack.callback(live.stop)
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch(executable_path=executable, headless=True,
                                     args=["--no-proxy-server", "--disable-background-networking"])
        stack.callback(browser.close)
        # Render an original demonstration document.
        doc = browser.new_page(viewport={"width": 700, "height": 840}, device_scale_factor=1)
        title, subtitle, goal, milestones, first_release = BRIEF
        rows = "".join(f"<tr><td>{step}</td><td>{result}</td></tr>" for step, result in milestones)
        doc.set_content(f"""<!doctype html><html lang="en"><meta charset="utf-8">
            <style>body{{padding:65px;color:#253239;font:22px/1.6 Georgia;background:#fff}}
            h1{{font-size:40px}}h2{{font-size:28px;color:#c45100}}table{{width:100%;border-collapse:collapse}}
            td,th{{text-align:left;padding:14px 0;border-bottom:1px solid #ddd}}</style>
            <h1>{title}</h1><h2>{subtitle}</h2><p>{goal}</p>
            <table><tr><th>Milestone</th><th>Result</th></tr>{rows}</table>
            <p><b>First release</b> — {first_release}</p></html>""")
        seed(live, doc.screenshot())
        doc.close()
        aid, bridge = code_online(live, "owner", demo_machine(), name="Demo PC")
        stack.callback(bridge.stop)
        manifest = []
        for mobile in (False, True):
            width, height = (390, 844) if mobile else (1440, 1000)
            context = browser.new_context(viewport={"width": width, "height": height},
                                          device_scale_factor=1, is_mobile=mobile, has_touch=mobile,
                                          service_workers="block", color_scheme=THEME, locale="en-US")
            stack.callback(context.close)
            context.add_init_script(f"localStorage.setItem('studio-language', '{LANGUAGE}')")
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
                          ("directory", "admin", "#ldap-settings summary")]
            for name, route, selector in shots:
                page.goto(live.url + "/#/" + route)
                page.wait_for_function(READY)
                page.wait_for_function(
                    "({theme, language}) => document.documentElement.dataset.theme === theme"
                    " && document.documentElement.lang === language",
                    arg={"theme": THEME, "language": LANGUAGE},
                )
                page.locator(selector).first.wait_for(state="visible")
                if name == "translation":
                    page.locator("#tl-output").get_by_text(
                        "Your library at your fingertips.", exact=False
                    ).wait_for(state="visible")
                if name == "ocr":
                    page.wait_for_function("() => document.querySelector('img.ocr-scan')?.naturalWidth > 0")
                if name == "images":
                    page.locator("#photo-prompt").fill("A cozy home library with oak shelves, "
                                                       "soft window light and watercolor textures")
                if name == "files":
                    page.get_by_text("README.md", exact=True).click()
                    page.locator("#code-file h1").wait_for(state="visible")
                if name == "directory":
                    page.locator("#ldap-settings summary").click()
                    page.wait_for_function("() => document.querySelector('#ldap-config-save')?.disabled === false")
                    page.locator("#ldap-config-url").fill("ldaps://dc.example.com")
                    page.locator("#ldap-config-base").fill("DC=example,DC=com")
                    page.locator("#ldap-config-group").fill("CN=Studio Users,OU=Groups,DC=example,DC=com")
                    bind_dn = page.locator("#ldap-config-bind-dn")
                    bind_dn.fill("CN=Directory Reader,OU=Services,DC=example,DC=com")
                    bind_dn.blur()
                    page.locator("#ldap-settings").evaluate("node => node.scrollIntoView({block: 'start'})")
                if mobile and name == "chat":
                    page.locator("#messages").evaluate("node => { node.scrollTop = 0; }")
                page.evaluate("() => document.fonts.ready")
                page.wait_for_timeout(400)
                if mobile and name == "agents":
                    page.locator("#code-chat-scroll").evaluate("node => { node.scrollTop = 0; }")
                if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                    raise RuntimeError(f"Horizontal overflow: {name}, {width}px")
                if errors:
                    raise RuntimeError(f"Browser errors in {name}: {errors}")
                filename = f"{name}{'-mobile' if mobile else ''}.png"
                page.screenshot(path=str(output / filename), animations="disabled")
                manifest.append({"file": filename, "viewport": [width, height],
                                 "theme": THEME, "language": LANGUAGE})
                print(f"Captured {filename}", flush=True)
            context.close()
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "screenshots")
    capture(parser.parse_args().output.resolve())
