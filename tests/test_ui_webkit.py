"""Раздел «Код» в WebKit: касания и геометрия iPhone, не только узкое окно Chromium."""

import hashlib
from pathlib import Path

import httpx
import pytest

from fake_bridge import pid
from test_codex import CATALOG
from test_ui_browser import CODE_CHAT_A, CODE_CHAT_B, code_online, code_three, live, wait_for

pw = pytest.importorskip("playwright.sync_api")
pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.fixture(scope="module")
def webkit():
    with pw.sync_playwright() as driver:
        if not Path(driver.webkit.executable_path).is_file():
            pytest.skip("нет WebKit: .venv/Scripts/python -m playwright install webkit")
        browser = driver.webkit.launch()
        yield browser
        browser.close()


@pytest.fixture
def iphone(webkit, live, request):
    width, height = request.param
    login = "webkit-" + hashlib.sha256(request.node.name.encode()).hexdigest()[:12]
    live.add_user(login)
    machine = code_three()
    machine.projects["alpha"]["files"] = {"README.md": b"# Hello\n\nPreview text", "src/app.py": b"print(1)\n"}
    machine.projects.update({f"project-{n:02}": {} for n in range(40)})
    machine.projects["zulu"] = {}
    aid, bridge = code_online(live, login, machine)
    try:
        signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
        assert signed.status_code == 303
        # WebKit не отправляет Secure cookie по HTTP localhost. Только тестовый контекст
        # получает заголовок своей действительной сессии; защита cookie приложения прежняя.
        cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
        with webkit.new_context(viewport={"width": width, "height": height}, is_mobile=True,
                                has_touch=True, device_scale_factor=3,
                                extra_http_headers={"Cookie": cookie}) as context:
            page = context.new_page()
            page.studio_machine = machine
            page.studio_bridge = bridge
            page.studio_cookie = cookie
            page.set_default_timeout(10000)
            errors = []
            # WebKit r2370 ошибочно превращает отмену fetch при reload в pageerror:
            # https://github.com/microsoft/playwright/issues/8443
            # Проверяем настоящие необработанные ошибки JS во всех документах страницы.
            marker = 'studio-test-uncaught: '
            page.on('console', lambda message: errors.append(message.text)
                    if message.type == 'error' and message.text.startswith(marker) else None)
            page.add_init_script("""addEventListener('error', event => {
                if (event.error) console.error('studio-test-uncaught: ' + String(event.error.stack || event.message));
            });
            addEventListener('unhandledrejection', event => {
                console.error('studio-test-uncaught: ' + String(event.reason?.stack || event.reason));
            });""")
            page.goto(live.url + "/#/code")
            page.locator("#code-projects li[data-pid]").first.wait_for()
            yield page, aid
            assert not errors
    finally:
        bridge.stop()


def fits(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
def test_session_picker_keeps_titles_after_leaving_the_section(iphone):
    page, aid = iphone
    page.locator(f"#code-projects li[data-pid='{pid('alpha')}'] a").tap()
    page.locator(f"#code-chats li[data-chat='{CODE_CHAT_A}'] button").tap()
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    page.locator('#rail [data-section=photo]').tap()
    page.locator('#code-chat').wait_for(state='detached')
    page.evaluate("""() => {
      const real = window.fetch;
      window.fetch = async (url, options) => {
        if (String(url).endsWith('/chats') && options?.method === 'GET') {
          await new Promise(resolve => { window.resumeChatList = resolve; });
        }
        return real(url, options);
      };
    }""")
    page.locator('#rail [data-section=code]').tap()
    pw.expect(page.locator(f'.code-tab[data-chat="{CODE_CHAT_A}"] .code-tab-title')).to_have_text('Чат A')
    page.locator('#code-tab-add').tap()
    page.locator('#code-chat-search').fill('ЧАТ b')
    pw.expect(page.locator('#code-chats li[data-chat]')).to_have_count(1)
    pw.expect(page.locator(f'#code-chats li[data-chat="{CODE_CHAT_B}"]')).to_be_visible()
    page.evaluate('() => window.resumeChatList()')
    pw.expect(page.locator('#code-chats')).to_have_attribute('aria-busy', 'false')
    pw.expect(page.locator('#code-chat-search')).to_have_value('ЧАТ b')
    fits(page)


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
def test_pin_starts_before_the_app_shell_loads(iphone, tmp_path):
    page, _ = iphone
    page.evaluate("""async () => {
      const {api} = await import('/static/js/api.js');
      await api('POST', '/api/me/screen-lock', {enabled: true});
      await api('PUT', '/api/me/pin', {password: 'password-123', pin: '2580'});
      await api('POST', '/api/lock/alive', {touch: true});
      await api('POST', '/api/lock/now');
    }""")
    # Service worker выключен только здесь, чтобы Playwright мог перехватить модуль оболочки.
    with page.context.browser.new_context(viewport=page.viewport_size, is_mobile=True, has_touch=True,
            extra_http_headers={'Cookie': page.studio_cookie}, service_workers='block') as context:
        early = context.new_page()
        early.route('**/static/js/app.js?*', lambda route: route.fulfill(
            status=200, content_type='text/javascript', body='window.shellBlocked = true;'))
        early.goto(page.url)
        pw.expect(early.locator('#lock-pin')).to_be_visible()
        assert early.evaluate('window.shellBlocked === true && !document.documentElement.dataset.ready')
        early.screenshot(path=str(tmp_path / 'early-pin.png'))
        fits(early)
        for digit in '2580':
            early.locator(f'#lock-pin [data-digit="{digit}"]').tap()
        early.locator('#lock-pin-ok').tap()
        pw.expect(early.locator('#lock')).not_to_be_visible()
        assert early.evaluate("async () => (await fetch('/api/me')).status") == 200


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
def test_closing_phone_browser_keeps_job_running_and_result_is_available_on_return(iphone):
    import bridge

    page, aid = iphone
    machine, channel = page.studio_machine, page.studio_bridge
    cookie, browser = page.studio_cookie, page.context.browser
    url = page.url.split('/#')[0] + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    machine.script = [{"k": "text", "text": "Работаю"}]
    page.goto(url)
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    page.locator('#code-text').fill('Заверши задание без открытого телефона')
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-feed')).to_contain_text('Работаю')
    page.context.close()
    wait_for(lambda: not bridge.watching(aid, CODE_CHAT_A), 10)
    assert CODE_CHAT_A in machine.running
    assert not any(r['op'] == 'stop' for r in machine.requests)

    # Модель заканчивает уже без браузера; ответ остаётся в истории компьютера.
    machine.running[CODE_CHAT_A]['text'] = 'Задание завершено при закрытом телефоне'
    completed = []
    machine._finish(CODE_CHAT_A, completed, {"k": "done", "status": "done"})
    channel.send_events(pid('alpha'), CODE_CHAT_A, *(frame['e'] for frame in completed))
    assert CODE_CHAT_A not in machine.running

    with browser.new_context(viewport={"width": 393, "height": 852}, is_mobile=True, has_touch=True,
                             extra_http_headers={"Cookie": cookie}) as context:
        returned = context.new_page()
        returned.goto(url)
        returned.locator('#code-chat[data-loaded="1"]').wait_for()
        pw.expect(returned.locator('#code-feed')).to_contain_text('Задание завершено при закрытом телефоне')
        assert sum(r['op'] == 'turn' for r in machine.requests) == 1
        assert not any(r['op'] == 'stop' for r in machine.requests)


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
@pytest.mark.parametrize("scale,font", [(100, 85), (80, 100), (50, 85)])
def test_text_fields_stay_readable_and_fit_when_focused_at_reduced_scale(iphone, scale, font, tmp_path):
    page, aid = iphone
    page.goto(page.url.split('/#')[0] + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    page.locator('#avatar').tap()
    page.locator('#appearance-font').fill(str(font))
    page.locator('#appearance-scale').select_option(str(scale))
    page.locator('#avatar').tap()
    page.locator('#code-text').tap()
    page.locator('#code-text').fill('Длинный черновик ' * 100)
    geometry = page.evaluate("""() => {
      const root = document.documentElement, field = document.querySelector('#code-text');
      const scale = parseFloat(root.style.zoom), box = field.getBoundingClientRect();
      return { readable: parseFloat(getComputedStyle(field).fontSize) * scale >= 16,
        viewport: visualViewport.scale === 1,
        fits: box.left >= 0 && box.right <= innerWidth && box.bottom <= innerHeight,
        shell: document.body.getBoundingClientRect().height <= innerHeight + 1 };
    }""")
    assert all(geometry.values()), geometry
    page.locator('#code-text').blur()
    fits(page)
    page.screenshot(path=str(tmp_path / f'focused-{scale}-{font}.png'))


@pytest.mark.parametrize("iphone", [(320, 740), (393, 852)], ids=["320", "iphone15pro"], indirect=True)
def test_code_phone_projects_fill_the_panel_and_accept_taps(iphone, tmp_path):
    page, aid = iphone
    panel = page.locator("#code-panel")
    box = panel.bounding_box()
    rail = page.locator("#rail").bounding_box()
    assert abs(box["height"] - rail["y"]) <= 1, (box, rail)
    # Проверка попадания касания: наличие строк в DOM не означает, что они видны в обрезанной панели.
    alpha = page.locator(f"#code-projects li[data-pid='{pid('alpha')}'] a")
    alpha.tap(trial=True)
    assert alpha.evaluate("""e => {
        const r = e.getBoundingClientRect();
        return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }""")
    page.screenshot(path=str(tmp_path / "projects.png"))
    alpha.tap()
    panel.wait_for(state="hidden")
    page.locator("#code-chats li[data-chat]").first.wait_for()
    page.locator("#code-panel-toggle").tap()
    panel.wait_for(state="visible")
    page.locator(f"#code-projects li[data-pid='{pid('beta')}'] a").tap()
    panel.wait_for(state="hidden")
    pw.expect(page.locator(".code-place-project")).to_have_text("beta")
    assert page.url.endswith(f"#/code/{aid}/{pid('beta')}")
    page.locator("#code-panel-toggle").tap()
    page.locator(f"#code-projects li[data-pid='{pid('zulu')}'] a").tap()
    panel.wait_for(state="hidden")
    pw.expect(page.locator(".code-place-project")).to_have_text("zulu")
    fits(page)


@pytest.mark.parametrize("iphone", [(320, 740), (393, 852), (852, 393)], indirect=True)
def test_code_composer_fits_long_draft_and_sends_followup_while_working(iphone, tmp_path):
    page, aid = iphone
    page.goto(page.url.split('/#')[0] + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    machine = page.studio_machine
    machine.script = [{"k": "activity", "state": "thinking"}]
    page.locator('#code-text').fill('Первое задание')
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-activity')).to_contain_text('Думает')
    draft = 'Уточнение: ' + 'длинная строка ' * 150
    page.locator('#code-text').fill(draft)
    geometry = page.evaluate("""() => {
        const box = document.querySelector('#code-composer').getBoundingClientRect();
        const status = document.querySelector('#code-activity').getBoundingClientRect();
        const input = document.querySelector('#code-text').getBoundingClientRect();
        const buttons = [...document.querySelectorAll('#code-composer .field-tools button')]
            .filter(e => !e.hidden).map(e => e.getBoundingClientRect());
        const picks = [...document.querySelectorAll('#code-composer select')];
        return { statusAbove: status.bottom <= box.top, shortInput: input.height <= innerHeight * .25 + 1,
            noDuplicate: !document.querySelector('.code-chat-bar') && !document.querySelector('#code-close-chat'),
            column: buttons.every(b => Math.abs(b.x - buttons[0].x) < 1),
            font: picks.every(e => parseFloat(getComputedStyle(e).fontSize) >= 16),
            picksInside: picks.every(e => e.getBoundingClientRect().right <= innerWidth),
            canSend: !document.querySelector('#code-send').disabled, noZoom: visualViewport.scale === 1 };
    }""")
    assert all(geometry.values()), geometry
    fits(page)
    page.screenshot(path=str(tmp_path / 'composer.png'))
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-text')).to_have_value('')
    pw.expect(page.locator('#code-feed .code-msg.user').last).to_have_text(draft)
    assert len([r for r in machine.requests if r['op'] == 'turn']) == 1
    assert [r for r in machine.requests if r['op'] == 'steer'][0]['text'] == draft
    assert CODE_CHAT_A in machine.running
    fits(page)


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
def test_code_other_window_can_wait_and_take_control_without_restarting(iphone):
    page, aid = iphone
    target = page.url.split('/#')[0] + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    page.goto(target)
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    page.studio_machine.script = [{"k": "text", "text": "Выполняю задание"}]
    page.locator('#code-text').fill('Работай')
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-stop')).to_be_visible()
    with page.context.new_page() as other:
        other.goto(target)
        pw.expect(other.locator('#code-session-notice')).to_contain_text('другом окне')
        pw.expect(other.locator('#code-send')).to_be_disabled()
        other.locator('#code-wait-session').tap()
        pw.expect(other.locator('#code-session-notice')).to_contain_text('Жду завершения')
        other.locator('#code-take-session').tap()
        pw.expect(other.locator('#code-session-notice')).to_be_hidden()
        pw.expect(page.locator('#code-send')).to_be_disabled()
        other.locator('#code-text').fill('Уточнение из нового окна')
        other.locator('#code-send').tap()
        pw.expect(page.locator('#code-feed')).to_contain_text('Уточнение из нового окна')
        assert len([r for r in page.studio_machine.requests if r['op'] == 'turn']) == 1
        other.locator('#code-stop').tap()
        pw.expect(page.locator('#code-session-notice')).to_be_hidden()
        pw.expect(page.locator('#code-send')).to_be_enabled()


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
@pytest.mark.parametrize('mode', ['wait', 'fork'])
def test_codex_external_writer_restores_input_and_offers_wait_or_copy(iphone, mode):
    page, aid = iphone
    machine = page.studio_machine
    machine.codex = CATALOG
    machine.projects['alpha']['chats'][CODE_CHAT_A]['engine'] = 'codex'
    # hello уже ушёл: обновить каталог только у подставного канала сервера.
    import bridge
    bridge.LINKS[aid].hello['codex'] = CATALOG
    occupied = [True]

    def refused_turn(req, events):
        assert occupied[0]
        events.append({'t': 'ev', 'project': req['project'], 'chat': req['chat'], 'turn': 'rejected',
                       'e': {'k': 'done', 'status': 'error', 'not_started': True,
                             'code': 'writer_active', 'message': 'Сессия занята'}})
        return {'turn': 'rejected'}

    def claim(req, events):
        if req.get('fork'):
            occupied[0] = False
            return {'available': True, 'forked': True}
        return {'available': not occupied[0], 'external': occupied[0]}

    machine._op_turn = refused_turn
    machine._op_claim = claim
    page.add_init_script("""const later = window.setTimeout;
        window.setTimeout = (fn, ms, ...args) => later(fn, ms === 5000 ? 50 : ms, ...args);""")
    page.goto(page.url.split('/#')[0] + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
    page.reload()
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    page.locator('#code-text').fill('Сохранённое задание')
    page.locator('#code-image-input').set_input_files({'name': 'shot.png', 'mimeType': 'image/png',
        'buffer': bytes.fromhex('89504e470d0a1a0a') + b'screenshot'})
    pw.expect(page.locator('#code-images .code-image')).to_have_count(1)
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-session-notice')).to_contain_text('Codex Desktop')
    pw.expect(page.locator('#code-text')).to_have_value('Сохранённое задание')
    pw.expect(page.locator('#code-images .code-image')).to_have_count(1)
    assert not any(r['op'] == 'claim' for r in machine.requests)
    if mode == 'wait':
        page.locator('#code-wait-session').tap()
        pw.expect(page.locator('#code-session-notice')).to_contain_text('Жду освобождения')
        occupied[0] = False
    else:
        page.locator('#code-take-session').tap()
    pw.expect(page.locator('#code-session-notice')).to_be_hidden()
    pw.expect(page.locator('#code-send')).to_be_enabled()
    pw.expect(page.locator('#code-text')).to_have_value('Сохранённое задание')
    pw.expect(page.locator('#code-images .code-image')).to_have_count(1)
    assert len([r for r in machine.requests if r['op'] == 'turn']) == 1
    fits(page)


@pytest.mark.parametrize("iphone", [(393, 852)], indirect=True)
def test_opencode_stop_restores_queued_text_and_screenshot(iphone):
    import bridge
    page, aid = iphone
    machine = page.studio_machine
    machine.opencode = CATALOG
    machine.projects['alpha']['chats'][CODE_CHAT_A]['engine'] = 'opencode'
    bridge.LINKS[aid].hello['opencode'] = CATALOG
    machine.script = [{"k": "activity", "state": "thinking"}]
    page.goto(page.url.split('/#')[0] + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}")
    page.reload()
    page.locator('#code-chat[data-loaded="1"]').wait_for()
    page.locator('#code-text').fill('Задание')
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-stop')).to_be_visible()
    page.locator('#code-text').fill('Уточнение в очередь')
    page.locator('#code-image-input').set_input_files({'name': 'shot.png', 'mimeType': 'image/png',
        'buffer': bytes.fromhex('89504e470d0a1a0a') + b'screenshot'})
    pw.expect(page.locator('#code-images .code-image')).to_have_count(1)
    page.locator('#code-send').tap()
    pw.expect(page.locator('#code-text')).to_have_value('')
    page.locator('#code-stop').tap()
    pw.expect(page.locator('#code-text')).to_have_value('Уточнение в очередь')
    pw.expect(page.locator('#code-images .code-image')).to_have_count(1)


@pytest.mark.parametrize("iphone", [(393, 852), (852, 393)], ids=["portrait", "landscape"], indirect=True)
def test_code_chat_tabs_files_and_git_show_their_content(iphone, tmp_path):
    page, aid = iphone
    page.locator(f"#code-projects li[data-pid='{pid('alpha')}'] a").tap()
    if page.viewport_size["width"] <= 760:
        page.locator("#code-panel").wait_for(state="hidden")
    page.locator(f"#code-chats li[data-chat='{CODE_CHAT_A}'] button").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    pw.expect(page.locator("#code-feed")).to_be_in_viewport()
    page.locator("#code-tab-add").tap()
    page.locator(f"#code-tab-menu li[data-chat='{CODE_CHAT_B}'] button").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про B")
    page.locator(f"#code-tabbar .code-tab[data-chat='{CODE_CHAT_A}'] .code-tab-title").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    page.screenshot(path=str(tmp_path / "chat.png"))
    fits(page)
    page.locator("#code-tab-files").tap()
    page.locator("#code-files li[data-name='README.md'] button").tap()
    pw.expect(page.locator("#code-editor")).to_have_value("# Hello\n\nPreview text")
    page.locator("#code-md-view").get_by_role("button", name="Вид", exact=True).tap()
    pw.expect(page.locator("#code-md")).to_contain_text("Hello")
    pw.expect(page.locator("#code-md")).to_be_in_viewport()
    page.screenshot(path=str(tmp_path / "files.png"))
    fits(page)
    page.locator("#code-tab-git").tap()
    pw.expect(page.locator("#code-git")).to_contain_text("main")
    pw.expect(page.locator("#code-git")).to_contain_text("Изменений нет")
    pw.expect(page.locator("#code-git")).to_be_in_viewport()
    page.locator("#code-tab-chat").tap()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    page.reload()
    pw.expect(page.locator("#code-feed")).to_contain_text("про A")
    fits(page)
