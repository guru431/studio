"""«Мои файлы» в настоящем браузере (my-files.js, my_files.py): пункт меню аватара при сервере с `studio/files`,
загрузка кусками, папка, «Ссылка на сутки»; ссылка `/f#<token>` открывается в чистом контексте браузера без входа."""
import pytest

from conftest import browser, cdp_process  # noqa: F401 — фикстуры браузера
from live_app import start_live_app
from test_mcp import TOKEN as MCP_KEY
from test_mcp_dialog import HOST
from test_my_files import FileServer
from test_ui_browser import MENU_OPEN, READY, problems, signin
from test_ui_chat_mcp import admin_client

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

TEXT = "Отчёт за ноябрь — строка\n" * 40
ITEM = "document.querySelector('#myfiles-list .myfiles-item[data-name=\"{0}\"]')"
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
        ("click", "#avatar"), ("wait", f"{MENU_OPEN} && !document.querySelector('#menu-my-files').hidden"),
        ("click", "#menu-my-files"),
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
    assert values["title"] == "Мои файлы" and values["picked"] == 1 and not values["overflow"]
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
