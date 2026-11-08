"""Рабочий стол: явное подключение, локальное разрешение, ввод и очистка экрана."""
import base64
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from browser import find_browser
from fake_bridge import FakeMachine, Refused
from test_ui_browser import READY, browser, code_online, enroll_live, go, live, problems, signin
from test_ui_webkit import pw, webkit

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

OPEN = "document.querySelector('#code-desktop-dialog')?.open"
CONNECTED = "!document.querySelector('.code-desktop-stage').hidden && document.querySelector('#code-desktop-screen').width === 160"
FITS = """(() => {
  const dialog = document.querySelector('#code-desktop-dialog'), bounds = dialog.getBoundingClientRect();
  return bounds.left >= 0 && bounds.right <= innerWidth + 1 && bounds.top >= 0
    && bounds.bottom <= innerHeight + 1 && dialog.scrollWidth <= dialog.clientWidth + 1
    && document.documentElement.scrollWidth <= innerWidth;
})()"""


def desktop(enabled=True):
    machine = FakeMachine({'alpha': {}})
    machine.desktop = True
    state = {'enabled': enabled, 'control': None, 'clipboard': 'Буфер компьютера', 'starts': 0}
    images = {}
    for ident, size in [(0, (160, 90)), (1, (90, 160))]:
        output = io.BytesIO()
        Image.new('RGB', size, '#61718a').save(output, format='JPEG')
        images[ident] = base64.b64encode(output.getvalue()).decode()

    def status(req, events):
        return {'enabled': state['enabled'], 'active': state['control'] is not None,
                'resolutions': [640, 800, 1024, 1280, 1600, 1920, 2560]}

    def start(req, events):
        if not state['enabled']:
            raise Refused('desktop_disabled', 'Рабочий стол отключён в агенте на компьютере')
        state['starts'] += 1
        state['control'] = f"{state['starts']:032x}"
        return {'control': state['control'], 'monitors': [
            {'id': 0, 'width': 1920, 'height': 1080, 'primary': True},
            {'id': 1, 'width': 1080, 'height': 1920, 'primary': False}], 'monitor': 0}

    def check(req):
        if not state['enabled']:
            raise Refused('desktop_disabled', 'Рабочий стол отключён в агенте на компьютере')
        if req.get('control') != state['control']:
            raise Refused('desktop_closed', 'Соединение закрыто')

    def frame(req, events):
        check(req)
        portrait = req['monitor'] == 1
        return {'image': images[req['monitor']], 'width': 90 if portrait else 160,
                'height': 160 if portrait else 90, 'screen_width': 1080 if portrait else 1920,
                'screen_height': 1920 if portrait else 1080, 'monitor': req['monitor']}

    def input_(req, events):
        check(req)
        return {}

    def clipboard(req, events):
        check(req)
        if req.get('text') is not None:
            state['clipboard'] = req['text']
        return {'text': state['clipboard']}

    def stop(req, events):
        if req.get('control') == state['control']:
            state['control'] = None
        return {}

    machine._op_desktop_status = status
    machine._op_desktop_start = start
    machine._op_desktop_frame = frame
    machine._op_desktop_input = input_
    machine._op_desktop_clipboard = clipboard
    machine._op_desktop_stop = stop
    return machine, state


def opened(login, aid):
    return [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-projects li')"),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-desktop'), ('wait', OPEN)]


def desktop_requests(machine):
    return [req for req in machine.requests if req['op'].startswith('desktop_')]


@pytest.fixture(params=['firefox', 'chromium', 'webkit'])
def desktop_browser(request):
    # Function scope avoids keeping a second Playwright loop alive alongside the WebKit fixture.
    with pw.sync_playwright() as driver:
        kind = getattr(driver, request.param)
        executable = find_browser() if request.param == 'chromium' else kind.executable_path
        if not executable or not Path(executable).is_file():
            pytest.skip(f'нет {request.param}')
        browser = kind.launch(executable_path=executable)
        yield browser
        browser.close()


@pytest.mark.parametrize('width,height', [(1280, 720), (320, 740), (852, 393)])
def test_desktop_compact_start_fit_zoom_and_resize(desktop_browser, live, tmp_path, width, height):
    login = f'desktop-fit-{desktop_browser.browser_type.name}-{width}'
    live.add_user(login)
    machine, _ = desktop()
    aid, server = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': height},
                                      has_touch=width != 1280, extra_http_headers={'Cookie': cookie}) as context:
            if desktop_browser.browser_type.name == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(live.url + '/#/code')
            page.locator('#code-projects li').wait_for()
            page.locator(f'#code-agents li[data-id="{aid}"] .code-agent-desktop').click()
            page.wait_for_function("() => document.querySelector('#code-desktop-connect')?.disabled === false")
            start = page.locator('#code-desktop-dialog').bounding_box()
            page.screenshot(path=str(tmp_path / f'desktop-start-{width}.png'))
            page.locator('#code-desktop-connect').click()
            page.wait_for_function('() => ' + CONNECTED)
            page.screenshot(path=str(tmp_path / f'desktop-fit-{width}.png'))

            def check_fit():
                assert page.evaluate(FITS)
                bounds = page.locator('.code-desktop-stage').bounding_box()
                viewport = page.locator('.code-desktop-body').bounding_box()
                assert viewport['x'] - 1 <= bounds['x'] and viewport['y'] - 1 <= bounds['y']
                assert bounds['x'] + bounds['width'] <= viewport['x'] + viewport['width'] + 1
                assert bounds['y'] + bounds['height'] <= viewport['y'] + viewport['height'] + 1
                assert abs(bounds['width'] / bounds['height'] - (
                    16 / 9 if page.locator('#code-desktop-monitor').input_value() == '0' else 9 / 16)) < .01

            check_fit()
            assert start['width'] <= 560 and start['height'] <= 300, start
            assert not page.locator('#code-desktop-text').is_visible()
            assert page.locator('#code-desktop-fit').get_attribute('aria-pressed') == 'true'
            page.locator('#code-desktop-zoom-in').click()
            page.wait_for_function("() => document.querySelector('#code-desktop-fit').getAttribute('aria-pressed') === 'false'")
            enlarged = page.locator('.code-desktop-stage').bounding_box()['width']
            page.locator('#code-desktop-zoom-out').click()
            assert page.locator('.code-desktop-stage').bounding_box()['width'] < enlarged
            page.locator('#code-desktop-zoom-reset').click()
            assert page.locator('.code-desktop-stage').bounding_box()['width'] == pytest.approx(1920, abs=1)
            # Zooming and scrolling must still point to the correct remote coordinates.
            page.locator('.code-desktop-body').evaluate('body => {body.scrollLeft = 200; body.scrollTop = 100;}')
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-screen').click(position={'x': 450, 'y': 250}, force=True)
            moves = [event for req in desktop_requests(machine) if req['op'] == 'desktop_input'
                     for event in req['events'] if event['type'] == 'move']
            assert moves[-1]['x'] == pytest.approx(450 / 1920, abs=.002)
            assert moves[-1]['y'] == pytest.approx(250 / 1080, abs=.002)
            page.locator('#code-desktop-fit').click()
            check_fit()
            for scale in ('.5', '1'):
                page.evaluate('scale => {document.documentElement.style.zoom = scale;'
                              'document.documentElement.style.setProperty("--ui-scale", scale);'
                              'document.documentElement.style.setProperty("--font-scale", "1.5");}', scale)
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                check_fit()
                assert page.locator('.code-desktop-toolbar').evaluate('toolbar => [...toolbar.querySelectorAll("button, select")]'
                    '.every(e => {const b=e.getBoundingClientRect(); return b.left >= 0 && b.right <= innerWidth + 1;})')
            page.evaluate('() => document.documentElement.style.setProperty("--font-scale", "1")')
            page.locator('#code-desktop-tools-toggle').click()
            page.locator('#code-desktop-text').wait_for()
            check_fit()
            page.locator('#code-desktop-tools-toggle').click()
            page.set_viewport_size({'width': height, 'height': width})
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            check_fit()
            page.locator('#code-desktop-monitor').select_option('1')
            page.wait_for_function("() => document.querySelector('#code-desktop-screen').height === 160")
            check_fit()
            page.locator('#code-desktop-disconnect').click()
            assert not page.locator('#code-desktop-text').is_visible()
            assert not page.locator('#code-desktop-fit').is_visible()
            page.wait_for_function("() => document.querySelector('#code-desktop-connect').disabled === false")
            page.locator('#code-desktop-connect').click()
            page.wait_for_function('() => ' + CONNECTED)
            check_fit()
            page.locator('#code-desktop-close').click()
            assert not errors
    finally:
        server.stop()


def test_desktop_disabled_requires_a_person_at_the_computer(browser, live):
    login = 'desktop-disabled'
    live.add_user(login)
    machine, _ = desktop(enabled=False)
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*opened(login, aid),
            ('wait', "document.querySelector('.code-desktop-status').textContent.includes('отключён')"),
            ('eval', 'disabled', "document.querySelector('#code-desktop-connect').disabled"),
            ('eval', 'text', "document.querySelector('.code-desktop-status').textContent"),
            ('eval', 'controls', "[...document.querySelectorAll('#code-desktop-dialog button')].map(b => b.textContent)"),
            ('click', '#code-desktop-refresh'),
            ('wait', "!document.querySelector('#code-desktop-refresh').disabled"),
            ('click', '#code-desktop-close')])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['disabled']
    assert 'Человек за компьютером' in result['values']['text']
    assert not any('Включить' in value or 'Разрешить' in value for value in result['values']['controls'])
    assert [req['op'] for req in desktop_requests(machine)] == ['desktop_status', 'desktop_status']
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 1280])
def test_desktop_explicit_connect_mouse_keys_unicode_clipboard_and_clear(browser, live, width):
    login = f'desktop-input-{width}'
    live.add_user(login)
    machine, state = desktop()
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*opened(login, aid),
            ('wait', "!document.querySelector('#code-desktop-connect').disabled"),
            ('eval', 'initial', "document.querySelector('.code-desktop-stage').hidden"),
            ('click', '#code-desktop-connect'), ('wait', CONNECTED), ('eval', 'fits', FITS),
            ('click', '#code-desktop-screen'),
            ('eval', 'keyboard', """(() => {
              const screen = document.querySelector('#code-desktop-screen');
              for (const type of ['keydown', 'keyup']) screen.dispatchEvent(new KeyboardEvent(type,
                {code:'KeyA', key:'a', bubbles:true, cancelable:true}));
              screen.dispatchEvent(new WheelEvent('wheel', {deltaY:120, bubbles:true, cancelable:true}));
              return document.activeElement === screen;
            })()"""),
            ('click', '#code-desktop-tools-toggle'),
            ('click', '#code-desktop-touch-right-click'),
            ('type', '#code-desktop-text', 'Привет, удалённый компьютер'), ('click', '#code-desktop-send-text'),
            ('click', '#code-desktop-clipboard-get'),
            ('wait', "document.querySelector('#code-desktop-clipboard').value === 'Буфер компьютера'"),
            ('eval', 'clearClipboard', "document.querySelector('#code-desktop-clipboard').value = '';true"),
            ('type', '#code-desktop-clipboard', 'Только по кнопке'), ('click', '#code-desktop-clipboard-send'),
            ('wait', "document.querySelector('.code-desktop-status').textContent.includes('передан')"),
            ('eval', 'stored', "JSON.stringify([{...localStorage}, {...sessionStorage}]).includes('Буфер компьютера')"),
            ('click', '#code-desktop-close'),
            ('wait', "!document.querySelector('#code-desktop-dialog').open"),
            ('eval', 'cleared', "document.querySelector('#code-desktop-screen').width === 1 && "
             "document.querySelector('#code-desktop-text').value === '' && document.querySelector('#code-desktop-clipboard').value === ''"),
            ('eval', 'waitStop', 'new Promise(resolve => setTimeout(() => resolve(true), 250))')],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values'] == {'go': '#/code', 'initial': True, 'fits': True, 'keyboard': True, 'stored': False,
                               'cleared': True, 'waitStop': True, 'clearClipboard': True}
    requests = desktop_requests(machine)
    assert requests[0]['op'] == 'desktop_status' and requests[1]['op'] == 'desktop_start'
    events = [event for req in requests if req['op'] == 'desktop_input' for event in req['events']]
    assert any(event['type'] == 'move' and 0 <= event['x'] <= 1 and 0 <= event['y'] <= 1 for event in events)
    for name in ('left', 'right'):
        assert {'type': 'button', 'button': name, 'down': True} in events
        assert {'type': 'button', 'button': name, 'down': False} in events
    assert {'type': 'key', 'code': 'KeyA', 'down': True} in events
    assert {'type': 'key', 'code': 'KeyA', 'down': False} in events
    assert {'type': 'wheel', 'dx': 0, 'dy': 120} in events
    assert {'type': 'text', 'text': 'Привет, удалённый компьютер'} in events
    assert state['clipboard'] == 'Только по кнопке' and state['control'] is None
    assert requests[-1]['op'] == 'desktop_stop'
    assert not problems(result), problems(result)


@pytest.mark.parametrize('cause', ['hidden', 'shrouded', 'pagehide'])
def test_desktop_background_stops_and_never_reconnects_or_reuses_control(browser, live, cause):
    login = f'desktop-stop-{cause}'
    live.add_user(login)
    machine, state = desktop()
    aid, server = code_online(live, login, machine)
    if cause == 'hidden':
        leave = "Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))"
        back = "Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))"
    elif cause == 'shrouded':
        leave = "document.documentElement.classList.add('shrouded')"
        back = "document.documentElement.classList.remove('shrouded')"
    else:
        leave = "window.dispatchEvent(new Event('pagehide'))"
        back = "true"
    try:
        result = browser.drive(live.url + '/', [*opened(login, aid),
            ('wait', "!document.querySelector('#code-desktop-connect').disabled"),
            ('click', '#code-desktop-connect'), ('wait', CONNECTED),
            ('click', '#code-desktop-screen'),
            ('eval', 'hold', "document.querySelector('#code-desktop-screen').dispatchEvent(new KeyboardEvent('keydown',"
             "{code:'ControlLeft',key:'Control',bubbles:true,cancelable:true}));true"),
            ('eval', 'leave', f'(() => {{{leave};return true;}})()'),
            ('wait', "document.querySelector('.code-desktop-stage').hidden"),
            ('eval', 'back', f'(() => {{{back};return true;}})()'),
            ('eval', 'waitAfter', 'new Promise(resolve => setTimeout(() => resolve(true), 650))'),
            ('eval', 'stillStopped', "document.querySelector('.code-desktop-stage').hidden && "
             "document.querySelector('#code-desktop-screen').width === 1 && !document.querySelector('#code-desktop-connect').disabled"),
            ('click', '#code-desktop-close')])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['stillStopped'] and state['control'] is None and state['starts'] == 1
    requests = desktop_requests(machine)
    assert requests[-1]['op'] == 'desktop_stop'
    assert len([req for req in requests if req['op'] == 'desktop_frame']) <= 2
    assert not problems(result), problems(result)


def test_desktop_old_and_offline_agents_do_not_request_screen(browser, live):
    login = 'desktop-old-offline'
    live.add_user(login)
    machine = FakeMachine({'alpha': {}})
    aid, server = code_online(live, login, machine)
    offline, _ = enroll_live(live, login, 'Выключенный компьютер')
    try:
        result = browser.drive(live.url + '/', [*opened(login, aid),
            ('eval', 'old', "document.querySelector('.code-desktop-status').textContent"),
            ('click', '#code-desktop-close'),
            ('click', f'#code-agents li[data-id="{offline}"] .code-agent-desktop'), ('wait', OPEN),
            ('eval', 'offline', "document.querySelector('.code-desktop-status').textContent"),
            ('eval', 'disabled', "document.querySelector('#code-desktop-connect').disabled"),
            ('click', '#code-desktop-close')])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert 'Обновите агент' in result['values']['old']
    assert 'Компьютер не на связи' in result['values']['offline'] and result['values']['disabled']
    assert not desktop_requests(machine)
    assert not problems(result), problems(result)


def test_desktop_late_frame_cannot_repaint_a_new_connection(browser, live):
    login = 'desktop-late-frame'
    live.add_user(login)
    machine, state = desktop()
    aid, server = code_online(live, login, machine)
    delay = """(() => {
      const original = window.fetch;
      window.desktopFrameReplies = [];
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (String(args[0]).endsWith('/desktop/frame')) {
          const json = response.json.bind(response);
          response.json = async () => {
            const data = await json();
            await new Promise(resolve => window.desktopFrameReplies.push(resolve));
            return data;
          };
        }
        return response;
      };
      return true;
    })()"""
    try:
        result = browser.drive(live.url + '/', [*opened(login, aid), ('eval', 'delay', delay),
            ('wait', "!document.querySelector('#code-desktop-connect').disabled"),
            ('click', '#code-desktop-connect'), ('wait', 'window.desktopFrameReplies.length === 1'),
            ('click', '#code-desktop-close'),
            ('eval', 'waitStop', 'new Promise(resolve => setTimeout(() => resolve(true), 200))'),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-desktop'), ('wait', OPEN),
            ('wait', "!document.querySelector('#code-desktop-connect').disabled"),
            ('click', '#code-desktop-connect'), ('wait', 'window.desktopFrameReplies.length === 2'),
            ('eval', 'releaseOld', 'window.desktopFrameReplies[0]();true'),
            ('eval', 'waitOld', 'new Promise(resolve => setTimeout(() => resolve(true), 100))'),
            ('eval', 'oldInvisible', "document.querySelector('.code-desktop-stage').hidden && "
             "document.querySelector('#code-desktop-screen').width === 1"),
            ('eval', 'releaseNew', 'window.desktopFrameReplies[1]();true'), ('wait', CONNECTED),
            ('click', '#code-desktop-close'),
            ('eval', 'finishStop', 'new Promise(resolve => setTimeout(() => resolve(true), 200))')])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['oldInvisible'] and state['starts'] == 2 and state['control'] is None
    frames = [req for req in desktop_requests(machine) if req['op'] == 'desktop_frame']
    assert len(frames) == 2 and len({req['control'] for req in frames}) == 2
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width,height', [(320, 740), (852, 393)])
def test_desktop_touch_screen_and_geometry_in_webkit(webkit, live, tmp_path, width, height):
    login = f'desktop-webkit-{width}'
    live.add_user(login)
    machine, state = desktop()
    aid, server = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with webkit.new_context(viewport={'width': width, 'height': height}, is_mobile=True,
                                has_touch=True, extra_http_headers={'Cookie': cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(live.url + '/#/code')
            page.wait_for_selector('#code-projects li')
            page.locator(f'#code-agents li[data-id="{aid}"] .code-agent-desktop').tap()
            page.wait_for_function("() => document.querySelector('#code-desktop-connect')?.disabled === false")
            page.locator('#code-desktop-connect').tap()
            page.wait_for_function('() => ' + CONNECTED)
            assert page.evaluate(FITS)
            page.screenshot(path=str(tmp_path / f'desktop-webkit-{width}.png'))
            geometry = page.locator('.code-desktop-stage').evaluate('''stage => {
              const bounds = stage.getBoundingClientRect();
              return {width: bounds.width, height: bounds.height};
            }''')
            assert geometry['width'] <= width
            assert geometry['height'] <= page.locator('.code-desktop-body').bounding_box()['height'] + 1
            assert abs(geometry['height'] - geometry['width'] * 9 / 16) <= 2
            assert page.locator('#code-desktop-close').bounding_box()['height'] == 32
            page.locator('#code-desktop-screen').tap()
            page.locator('#code-desktop-tools-toggle').tap()
            page.locator('#code-desktop-text').fill('Привет из Safari')
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-send-text').tap()
            page.locator('#code-desktop-close').tap()
            page.wait_for_function("() => document.querySelector('#code-desktop-screen').width === 1")
    finally:
        server.stop()
    events = [event for req in desktop_requests(machine) if req['op'] == 'desktop_input' for event in req['events']]
    assert {'type': 'text', 'text': 'Привет из Safari'} in events
    assert any(event['type'] == 'button' and event['down'] for event in events)


@pytest.mark.parametrize('width,height', [(320, 740), (852, 393)])
def test_desktop_touch_and_trackpad_gestures_resolution_and_release(webkit, live, tmp_path, width, height):
    login = f'desktop-trackpad-{width}'
    live.add_user(login)
    machine, _ = desktop()
    aid, server = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())

    def events():
        return [event for req in desktop_requests(machine) if req['op'] == 'desktop_input' for event in req['events']]

    def stroke(page, points, end='pointerup'):
        # WebKit Playwright умеет настоящий tap; траекторию пальца задаём PointerEvent.
        # Захват настоящего указателя отдельно проверяется tap в тесте геометрии.
        with page.expect_response('**/desktop/input'):
            page.evaluate('''([points, end]) => {
              const screen = document.querySelector('#code-desktop-screen'), b = screen.getBoundingClientRect();
              const capture = screen.setPointerCapture;
              screen.setPointerCapture = () => {};
              try {
                const send = (type, point) => screen.dispatchEvent(new PointerEvent(type, {
                  bubbles: true, cancelable: true, pointerType: 'touch', isPrimary: true, pointerId: 7,
                  button: 0, clientX: b.left + point[0] * b.width, clientY: b.top + point[1] * b.height}));
                send('pointerdown', points[0]);
                for (const point of points.slice(1)) send('pointermove', point);
                send(end, points.at(-1));
              } finally { screen.setPointerCapture = capture; }
            }''', [points, end])

    try:
        with webkit.new_context(viewport={'width': width, 'height': height}, is_mobile=True,
                                has_touch=True, extra_http_headers={'Cookie': cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(live.url + '/#/code')
            page.wait_for_selector('#code-projects li')
            page.locator(f'#code-agents li[data-id="{aid}"] .code-agent-desktop').tap()
            page.wait_for_function("() => document.querySelector('#code-desktop-connect')?.disabled === false")
            page.locator('#code-desktop-connect').tap()
            page.wait_for_function('() => ' + CONNECTED)
            assert page.locator('#code-desktop-touch').get_attribute('aria-pressed') == 'true'
            stroke(page, [[.25, .25], [.35, .35]])
            direct = events()
            assert direct[0]['type'] == 'move' and direct[0]['monitor'] == 0
            assert direct[0]['x'] == pytest.approx(.25, abs=.0001)
            assert direct[0]['y'] == pytest.approx(.25, abs=.0001)
            assert direct[1] == {'type': 'button', 'button': 'left', 'down': True}
            assert direct[-1] == {'type': 'button', 'button': 'left', 'down': False}
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-mouse').tap()
            footer = page.locator('.code-desktop-mouse-tools').bounding_box()
            assert 0 <= footer['y'] and footer['y'] + footer['height'] <= height
            assert page.locator('#code-desktop-mouse').evaluate(
                "button => getComputedStyle(button).backgroundColor !== getComputedStyle(document.querySelector('#code-desktop-touch')).backgroundColor")
            before = len(events())
            stroke(page, [[.1, .8], [.3, .9]])
            moved = events()[before:]
            assert all(event['type'] == 'move' for event in moved)
            assert moved[-1]['x'] == pytest.approx(.55, abs=.0001)
            assert moved[-1]['y'] == pytest.approx(.45, abs=.0001)
            before = len(events())
            stroke(page, [[.9, .1]])
            assert events()[before:] == [moved[-1], {'type': 'button', 'button': 'left', 'down': True},
                                        {'type': 'button', 'button': 'left', 'down': False}]
            for button in ('#code-desktop-right-click', '#code-desktop-scroll-down', '#code-desktop-drag'):
                with page.expect_response('**/desktop/input'):
                    page.locator(button).tap()
            assert page.locator('#code-desktop-drag').get_attribute('aria-pressed') == 'true'
            stroke(page, [[.2, .2], [.3, .3]])
            assert page.locator('#code-desktop-drag').get_attribute('aria-pressed') == 'true'
            assert events()[-1]['type'] == 'move'
            page.screenshot(path=str(tmp_path / f'desktop-mouse-{width}.png'))
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-drag').tap()
            assert events()[-1] == {'type': 'button', 'button': 'left', 'down': False}
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-drag').tap()
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-left-click').tap()
            assert page.locator('#code-desktop-drag').get_attribute('aria-pressed') == 'false'
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-drag').tap()
            stroke(page, [[.2, .2], [.3, .3]], end='pointercancel')
            assert events()[-1] == {'type': 'button', 'button': 'left', 'down': False}
            assert page.locator('#code-desktop-drag').get_attribute('aria-pressed') == 'false'
            assert page.locator('#code-desktop-resolution option').count() == 7
            for edge in (640, 2560):
                with page.expect_response(lambda response: response.url.endswith('/desktop/frame')
                                          and response.request.post_data_json.get('max_edge') == edge):
                    page.locator('#code-desktop-resolution').select_option(str(edge))
                assert any(req.get('max_edge') == edge for req in desktop_requests(machine))
            page.locator('#code-desktop-monitor').select_option('1')
            page.wait_for_function("() => document.querySelector('#code-desktop-screen').height === 160")
            assert page.evaluate(FITS)
            assert page.locator('.code-desktop-stage').evaluate(
                'stage => Math.abs(stage.clientHeight / stage.clientWidth - 16 / 9) < .01')
            page.locator('#code-desktop-zoom-reset').tap()
            with page.expect_response('**/desktop/input'):
                page.locator('#code-desktop-drag').tap()
            before = len(events())
            with page.expect_response('**/desktop/input'):
                scrolled = page.evaluate('''() => {
                  const screen = document.querySelector('#code-desktop-screen'), body = document.querySelector('.code-desktop-body');
                  body.scrollTop = body.scrollLeft = 0;
                  const send = (type, touches) => {
                    const event = new Event(type, {cancelable: true});
                    Object.defineProperty(event, 'touches', {value: touches});
                    screen.dispatchEvent(event);
                  };
                  send('touchstart', [{clientX: 200, clientY: 200}, {clientX: 220, clientY: 220}]);
                  send('touchmove', [{clientX: 100, clientY: 100}, {clientX: 120, clientY: 120}]);
                  send('touchend', []);
                  return [body.scrollLeft, body.scrollTop];
                }''')
            assert scrolled == [100, 100]
            assert events()[before:] == [{'type': 'button', 'button': 'left', 'down': False}]
            page.locator('#code-desktop-touch').tap()
            stroke(page, [[.1, .1], [.2, .2]], end='pointercancel')
            assert events()[-1] == {'type': 'button', 'button': 'left', 'down': False}
            page.locator('#code-desktop-close').tap()
    finally:
        server.stop()
    assert {'type': 'wheel', 'dx': 0, 'dy': 120} in events()
    assert {'type': 'button', 'button': 'right', 'down': True} in events()
