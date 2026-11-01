"""Рабочий стол: явное подключение, локальное разрешение, ввод и очистка экрана."""
import base64
import io

import httpx
import pytest
from PIL import Image

from fake_bridge import FakeMachine, Refused
from test_ui_browser import READY, browser, code_online, enroll_live, go, live, problems, signin
from test_ui_webkit import webkit

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
    output = io.BytesIO()
    Image.new('RGB', (160, 90), '#61718a').save(output, format='JPEG')
    image = base64.b64encode(output.getvalue()).decode()

    def status(req, events):
        return {'enabled': state['enabled'], 'active': state['control'] is not None}

    def start(req, events):
        if not state['enabled']:
            raise Refused('desktop_disabled', 'Рабочий стол отключён в агенте на компьютере')
        state['starts'] += 1
        state['control'] = f"{state['starts']:032x}"
        return {'control': state['control'], 'monitors': [
            {'id': 0, 'width': 1920, 'height': 1080, 'primary': True}], 'monitor': 0}

    def check(req):
        if not state['enabled']:
            raise Refused('desktop_disabled', 'Рабочий стол отключён в агенте на компьютере')
        if req.get('control') != state['control']:
            raise Refused('desktop_closed', 'Соединение закрыто')

    def frame(req, events):
        check(req)
        return {'image': image, 'width': 160, 'height': 90, 'screen_width': 1920,
                'screen_height': 1080, 'monitor': 0}

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
            ('click', '#code-desktop-right-click'),
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


def test_desktop_touch_screen_and_geometry_in_webkit(webkit, live, tmp_path):
    login = 'desktop-webkit'
    live.add_user(login)
    machine, state = desktop()
    aid, server = code_online(live, login, machine)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with webkit.new_context(viewport={'width': 320, 'height': 740}, is_mobile=True,
                                has_touch=True, extra_http_headers={'Cookie': cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(live.url + '/#/code')
            page.wait_for_selector('#code-projects li')
            page.locator(f'#code-agents li[data-id="{aid}"] .code-agent-desktop').tap()
            page.wait_for_function("() => !document.querySelector('#code-desktop-connect').disabled")
            page.locator('#code-desktop-connect').tap()
            page.wait_for_function('() => ' + CONNECTED)
            assert page.evaluate(FITS)
            page.screenshot(path=str(tmp_path / 'desktop-webkit-320.png'))
            page.locator('#code-desktop-screen').tap()
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
