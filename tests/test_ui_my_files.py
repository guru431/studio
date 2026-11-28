"""«Мои файлы» в настоящем браузере (my-files.js, my_files.py): кнопка рельса и сервер с `studio/files`,
загрузка кусками, папка, «Ссылка на сутки»; ссылка `/f#<token>` открывается в чистом контексте браузера без входа."""
import base64

import pytest
import media

from conftest import browser, cdp_process  # noqa: F401 — фикстуры браузера
from fakes import PNG_REAL
from live_app import start_live_app
from test_mcp import TOKEN as MCP_KEY
from test_mcp_dialog import HOST
from test_my_files import FileServer
from test_ui_browser import MENU_OPEN, READY, live_sql, problems, signin
from test_ui_chat_mcp import admin_client

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

TEXT = "Отчёт за ноябрь — строка\n" * 40
ITEM = "document.querySelector('#myfiles-list .myfiles-item[data-name=\"{0}\"]')"
MCP_TAB = '#myfiles-tabs [data-storage="mcp"]'
UPLOAD = f"""(() => {{
  const input = document.querySelector('#myfiles-picker');
  const data = new DataTransfer();
  data.items.add(new File([{TEXT!r}], 'отчёт.txt', {{ type: 'text/plain' }}));
  input.files = data.files;
  const picked = input.files.length;
  input.dispatchEvent(new Event('change'));
  return picked;
}})()"""
#: скачивание страницы ссылки — нажатие на <a download>: настоящая загрузка в headless-браузере роняла следующий
#: контекст, поэтому адрес нажатия записывается, а файл читается fetch той же страницы (cookie `studio_file`)
NO_DOWNLOAD = ("HTMLAnchorElement.prototype.click = function () {"
               " document.documentElement.dataset.clicked = this.getAttribute('href'); };")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = start_live_app(tmp_path_factory.mktemp("studio-live-my-files"))
    FileServer(app)
    client = admin_client(app)
    try:
        response = client.post("/api/admin/shared/mcp", json={"name": "Файлы", "url": HOST + "/mcp", "token": MCP_KEY})
    finally:
        client.close()
    assert response.status_code == 201, response.text
    yield app
    app.stop()


@pytest.mark.parametrize("width", [320, 1280])
def test_my_files_upload_folder_and_day_link_opens_in_a_clean_browser(browser, cdp_process, live, width):
    login = f"files{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "railOrder", """(() => {
          const [search, files, jobs] = ['#search-open', '#myfiles-open', '#jobs']
            .map(id => document.querySelector(id).getBoundingClientRect());
          return matchMedia('(max-width: 760px)').matches
            ? search.right <= files.left && files.right <= jobs.left
            : search.bottom <= files.top && files.bottom <= jobs.top;
        })()"""),
        ("click", "#myfiles-open"),
        ("click", MCP_TAB),
        ("wait", "document.querySelector('#myfiles-none')"),
        ("eval", "title", "document.querySelector('#view h1').textContent"),
        ("eval", "picked", UPLOAD),
        ("wait", ITEM.format("отчёт.txt")),
        ("click", "#myfiles-mkdir"), ("type", "#myfiles-folder-name", "Папка"), ("click", "#myfiles-folder-create"),
        ("wait", ITEM.format("Папка")),
        ("click", f"#myfiles-list .myfiles-item[data-name='отчёт.txt'] .myfiles-share"),
        ("wait", "document.querySelector('#myfiles-link-url')?.value.includes('/f#')"
                 " && document.querySelector('#myfiles-links .myfiles-link-item')"),
        ("eval", "url", "document.querySelector('#myfiles-link-url').value"),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("click", "#myfiles-list .myfiles-item[data-name='Папка'] .myfiles-folder"),
        ("wait", "document.querySelector('#myfiles-path [data-path=\"Папка\"]') && document.querySelector('#myfiles-none')")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    values = r["values"]
    assert values["title"] == "Мои файлы" and values["picked"] == 1 and values["railOrder"] and not values["overflow"]
    assert values["url"].startswith(live.url + "/f#")
    assert not problems(r), problems(r)

    # Чистый контекст: ни сессии, ни cookie — ссылка открывает файл сама.
    with cdp_process.isolated() as clean:
        shared = clean.drive(values["url"], [
            ("wait", "document.querySelector('#file-name').textContent === 'отчёт.txt'"
                     " && !document.querySelector('#file-download').hidden"),
            ("eval", "facts", "document.querySelector('#file-facts').textContent"),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
            ("click", "#file-download"),
            ("wait", "document.querySelector('#file-status').textContent !== ''"),
            ("eval", "clicked", "document.documentElement.dataset.clicked"),
            ("eval", "cookie", "document.cookie"),
            ("eval", "body", "fetch('/f/file').then(async (r) => [r.status, r.headers.get('content-disposition'), await r.text()])")],
            width=width, mobile=width == 320, init=NO_DOWNLOAD)
    assert not shared.get("failed"), shared
    got = shared["values"]
    assert not got["overflow"] and "studio_file" not in got["cookie"] and got["clicked"] == "/f/file"
    assert got["body"][0] == 200 and got["body"][1].startswith("attachment;") and got["body"][2] == TEXT
    assert not problems(shared), problems(shared)


def test_my_files_trash_switches_between_studio_records_and_server_files(browser, live):
    login = "files-trash-switch"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("click", "#myfiles-open"),
        ("click", MCP_TAB),
        ("wait", "document.querySelector('#myfiles-none')"), ("eval", "upload", UPLOAD),
        ("wait", ITEM.format("отчёт.txt")), ("click", '#myfiles-list button[aria-label="Удалить: отчёт.txt"]'),
        ("wait", "document.querySelector('#myfiles-none')"), ("click", "#myfiles-trash"),
        ("wait", "document.querySelector('#trash-none') && document.querySelector('#myfiles-tabs [data-sid]:not([data-sid=\"0\"])')"),
        ("click", '#myfiles-tabs [data-sid]:not([data-sid="0"])'),
        ("wait", "document.querySelector('.myfiles-trash-item')?.textContent.includes('отчёт.txt')"),
        ("click", ".myfiles-trash-item button"),
        ("wait", "document.querySelector('#myfiles-none') && document.querySelector('#myfiles-status').textContent.includes('Возвращено')"),
        ("click", "#myfiles-back"), ("click", MCP_TAB), ("wait", ITEM.format("отчёт.txt"))], width=320, mobile=True)
    assert not r.get("failed"), r
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_my_files_sidebar_and_studio_and_mcp_thumbnails(browser, live, width):
    login = f"files-preview-{width}"
    uid = live.add_user(login)
    mid = media.save(uid, PNG_REAL, "image/png", "studio.png", thumb=True)
    live_sql(live, "INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created)"
             " VALUES (?, ?, 'generation', 'flux', 'Образец', '64x48', 1, ?, 'done', '', 1)", (mid, uid, mid))
    encoded = base64.b64encode(PNG_REAL).decode()
    upload = f"""(() => {{
      const picker = document.querySelector('#myfiles-picker');
      const data = new DataTransfer();
      data.items.add(new File([Uint8Array.from(atob({encoded!r}), c => c.charCodeAt(0))], 'server.png',
        {{ type: 'image/png' }}));
      picker.files = data.files;
      picker.dispatchEvent(new Event('change'));
      return true;
    }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("click", "#myfiles-open"),
        ("wait", "document.querySelector('#myfiles-projects button')"),
        ("eval", "sections", """(() => {
          const nav = document.querySelector('#myfiles-tabs');
          const pane = document.querySelector('.myfiles-pane');
          const first = Array.from(nav.children).slice(0, 3).map(item => item.dataset.sid);
          return first.join(',') === 'projects,photo,video' && (innerWidth <= 680
            ? nav.getBoundingClientRect().bottom <= pane.getBoundingClientRect().top
            : nav.getBoundingClientRect().right <= pane.getBoundingClientRect().left);
        })()"""),
        ("click", MCP_TAB),
        ("eval", "upload", upload),
        ("wait", f"{ITEM.format('server.png')}?.querySelector('.myfiles-thumb.has-preview img')?.naturalWidth > 0"),
        ("click", "#myfiles-tabs [data-sid='photo']"),
        ("wait", f"{ITEM.format('studio.png')}?.querySelector('.myfiles-thumb.has-preview img')?.naturalWidth > 0"),
        ("eval", "thumb", f"{ITEM.format('studio.png')}.querySelector('.myfiles-thumb img').src"),
        ("click", "#myfiles-list .myfiles-item[data-name='studio.png'] button.myfiles-file-info"),
        ("wait", "document.querySelector('#myfiles-preview[open] img')?.naturalWidth > 0"),
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth")], width=width, mobile=width == 320)
    assert not r.get("failed"), r
    assert r["values"]["sections"] and r["values"]["fits"]
    assert r["values"]["thumb"].endswith(f"/api/media/{mid}?thumb=1")
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_project_grid_folder_import_and_mixed_selection(browser, live, tmp_path_factory, width):
    login = f"files-project-tree-{width}"
    live.add_user(login)
    tree = """(() => {
      const input = document.querySelector('#project-files-directory');
      const transfer = new DataTransfer();
      for (const path of ['Отчёты/Sub/2026/notes.txt', 'Отчёты/notes.txt']) {
        const file = new File([path], 'notes.txt', { type: 'text/plain', lastModified: 1700000000500 });
        Object.defineProperty(file, 'webkitRelativePath', { value: path });
        transfer.items.add(file);
      }
      input.files = transfer.files;
      input.dispatchEvent(new Event('change'));
      return input.webkitdirectory;
    })()"""
    single = f"""(() => {{
      const data = new DataTransfer();
      data.items.add(new File([{TEXT!r}], 'отчёт.txt', {{ type: 'text/plain' }}));
      return !document.querySelector('#myfiles-list').dispatchEvent(new DragEvent('drop',
        {{ bubbles: true, cancelable: true, dataTransfer: data }}));
    }})()"""
    folder = '#myfiles-list li[data-name="Отчёты"]'
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("click", "#myfiles-open"),
        ("click", ".myfiles-new-project:not(:disabled)"),
        ("type", "#chat-project-editor input[required]", "Алгебра"), ("click", "#chat-project-editor .btn-accent"),
        ("wait", "document.querySelector('#myfiles-none')"),
        ("click", '#myfiles-tabs [data-sid="projects"]'),
        ("wait", "document.querySelectorAll('#myfiles-projects button').length === 2"),
        ("eval", "columns", "getComputedStyle(document.querySelector('#myfiles-projects')).gridTemplateColumns.split(' ').length"),
        ("screenshot", str(tmp_path_factory.getbasetemp() / f"my-files-projects-{width}.png")),
        ("click", '#myfiles-project-gallery button[aria-label="Сортировка проектов"]'),
        ("click", '#myfiles-project-gallery button[aria-label="Поиск проектов"]'),
        ("type", ".myfiles-project-search", "Алгебра"),
        ("wait", "document.querySelectorAll('#myfiles-projects button').length === 1"),
        ("click", '#myfiles-projects button[title="Алгебра"]'),
        ("wait", "document.querySelector('#myfiles-none')"),
        ("eval", "pathGap", """(() => {
          const path = document.querySelector('#myfiles-path').getBoundingClientRect();
          const actions = document.querySelector('#project-files-directory').parentElement.getBoundingClientRect();
          return actions.top - path.bottom;
        })()"""),
        ("eval", "directoryPicker", tree), ("wait", ITEM.format("Отчёты")),
        ("eval", "folderDates", f"[...{ITEM.format('Отчёты')}.querySelectorAll('.myfiles-dates > span')].map(s => s.textContent)"),
        ("eval", "totals", f"{ITEM.format('Отчёты')}.querySelector('.myfiles-meta').textContent"),
        ("click", f"{folder} .myfiles-folder"),
        ("wait", f"{ITEM.format('notes.txt')} && {ITEM.format('Sub')}"),
        ("eval", "fileDates", f"[...{ITEM.format('notes.txt')}.querySelectorAll('.myfiles-dates > span')].map(s => s.textContent)"),
        ("click", '#myfiles-list li[data-name="Sub"] .myfiles-folder'),
        ("wait", ITEM.format("2026")), ("click", '#myfiles-list li[data-name="2026"] .myfiles-folder'),
        ("wait", ITEM.format("notes.txt")),
        ("eval", "location", "document.querySelector('#myfiles-path [aria-current=location]').textContent"),
        ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
        ("click", "#myfiles-path li:nth-child(2) button"),
        ("wait", ITEM.format("Отчёты")), ("eval", "picked", single), ("wait", ITEM.format("отчёт.txt")),
        ("click", "#myfiles-select"), ("click", f"{folder} input[type=checkbox]"),
        ("click", '#myfiles-list li[data-name="отчёт.txt"] input[type=checkbox]'),
        ("eval", "selected", "document.querySelectorAll('#myfiles-list input:checked').length"),
        ("eval", "skipDownload", NO_DOWNLOAD), ("click", "#myfiles-selection-zip"),
        ("eval", "ids", "new URL(document.documentElement.dataset.clicked, location.href).searchParams.getAll('item').length"),
        ("click", "#myfiles-selection-delete"), ("click", "#myfiles-confirm-selection"),
        ("wait", "document.querySelector('#myfiles-none') && document.querySelector('#myfiles-status').textContent === 'Готово'"),
        ("click", "#myfiles-path li:first-child button"),
        ("wait", "document.querySelector('#myfiles-project-gallery:not([hidden])')")],
        width=width, mobile=width == 320)
    assert not r.get("failed"), r
    assert r["values"]["pathGap"] >= 8
    created, modified = r["values"]["folderDates"]
    assert created.startswith("Создано: ") and modified.startswith("Изменено: ") and "—" not in created + modified
    uploaded, modified = r["values"]["fileDates"]
    assert uploaded.startswith("Загружено: ") and "—" not in uploaded
    assert modified.startswith("Изменено: ") and "2023" in modified
    assert r["values"]["totals"].split(" · ")[1:] == ["Файлов: 2", "Подпапок: 2"]
    assert r["values"]["location"] == "2026" and r["values"]["fits"]
    assert r["values"]["picked"]
    assert r["values"]["columns"] == (1 if width == 320 else 2)
    assert r["values"]["selected"] == r["values"]["ids"] == 2
    assert r["values"]["directoryPicker"] and r["values"]["picked"] == 1
    assert not problems(r), problems(r)


def test_audio_results_are_identified_by_their_recording_and_spoken_text(browser, live):
    login = "files-audio-titles"
    uid = live.add_user(login)
    transcript = media.save(uid, b'{"segments": [], "raw": []}', "application/json", "transcript.json")
    live_sql(live, "INSERT INTO transcripts(id, user_id, model, name, source_media_id, result_media_id, state, detail, created)"
             " VALUES ('transcript', ?, 'asr', 'Встреча.m4a', '', ?, 'done', '', 1)", (uid, transcript))
    speech = media.save(uid, b"MP3", "audio/mpeg", "speech-12345678.mp3")
    live_sql(live, "INSERT INTO speeches(id, user_id, model, voice, text, media_id, state, detail, created)"
             " VALUES ('speech', ?, 'tts', 'voice', ?, ?, 'done', '', 1)",
             (uid, "Первый абзац.\n\nВторой абзац. " + "Продолжение " * 12, speech))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("click", "#myfiles-open"),
        ("click", '#myfiles-tabs [data-sid="transcribe"]'), ("wait", ITEM.format("transcript.json")),
        ("eval", "transcript", "document.querySelector('#myfiles-list .myfiles-name').textContent"),
        ("eval", "transcriptMeta", "document.querySelector('#myfiles-list .myfiles-meta').textContent"),
        ("click", '#myfiles-tabs [data-sid="speech"]'), ("wait", ITEM.format("speech-12345678.mp3")),
        ("eval", "speech", "document.querySelector('#myfiles-list .myfiles-name').textContent")])
    assert not r.get("failed"), r
    values = r["values"]
    assert values["transcript"] == "Встреча.m4a" and values["transcriptMeta"].startswith("Диалог · JSON · ")
    assert values["speech"].startswith("Первый абзац. Второй абзац. ")
    assert len(values["speech"]) <= 81 and values["speech"].endswith("…")
    assert not problems(r), problems(r)


def test_avatar_menu_under_expired_partial_lock_does_not_force_the_lock_screen(browser, live):
    """Защищён только «Чат», срок истёк, открыто «Фото»: проверка пункта «Мои файлы» при открытии меню аватара
    (GET /api/my-files/servers — 423, пока заперт хоть один раздел) экран блокировки не вызывает; содержимое «Моих
    файлов» по-прежнему закрыто."""
    from test_ui_browser import API, LOCK_OPEN, go, hide, show
    login = "files-partial"
    live.add_user(login)
    configure = f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/me/screen-lock', {{enabled:true, sections:['chat'], timeout:0}});
      await m.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "configure", configure),
        *go("photo"), ("wait", "document.querySelector('#rail [data-section=photo][aria-current=page]')"),
        hide(), show(),
        ("wait", "fetch('/api/my-files/servers').then(r => r.status === 423)"),
        ("click", "#avatar"), ("wait", MENU_OPEN),
        ("eval", "later", f"new Promise(r => setTimeout(() => r({LOCK_OPEN}), 1500))"),
        ("eval", "hash", "location.hash")])
    assert not r.get("failed"), r
    assert r["values"]["later"] is False and r["values"]["hash"] == "#/photo"
