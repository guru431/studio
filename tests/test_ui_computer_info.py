"""Сведения по нажатию на компьютер: телефон, рабочий стол, PIN и старый мост."""
import httpx
import pytest

from fake_bridge import FakeMachine
from test_ui_browser import READY, browser, code_online, enroll_live, go, live, problems, signin
from test_ui_webkit import webkit

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def computer(pin=None):
    machine = FakeMachine({'alpha': {}}, pin=pin)
    machine.computer_info = True
    machine.computer_data = {
        'hostname': 'WORKSTATION', 'username': 'test-worker', 'os': 'Windows 11 Pro 10.0.26100',
        'internal_ips': ['10.77.1.8', '10.8.0.4'], 'external_ip': '8.8.8.8',
        'memory': {'total': 17179869184, 'used': 12884901888, 'available': 4294967296},
        'disks': [{'path': 'C:\\', 'free': 107374182400, 'total': 536870912000},
                  {'path': 'D:\\', 'free': None, 'total': None}],
        'projects_roots': ['C:\\work\\projects', 'D:\\Проекты\\' + 'длинное-имя-папки-' * 12],
        'checked_at': 1800000000}
    return machine


SHOWN = "document.querySelector('#code-computer-dialog')?.open && document.querySelector('.code-computer-details')"
FITS = """(() => {
  const dialog = document.querySelector('#code-computer-dialog'), bounds = dialog.getBoundingClientRect();
  return bounds.left >= 0 && bounds.right <= innerWidth + 1 && bounds.top >= 0
    && bounds.bottom <= innerHeight + 1 && dialog.scrollWidth <= dialog.clientWidth + 1
    && document.documentElement.scrollWidth <= innerWidth;
})()"""


@pytest.mark.parametrize('width', [320, 1280])
def test_computer_click_shows_snapshot_refreshes_and_keeps_project(browser, live, width):
    login = f'computer-info-{width}'
    live.add_user(login)
    machine = computer()
    reads = []

    def read(req, events):
        reads.append(req)
        return {**machine.computer_data, 'username': 'test-worker' if len(reads) == 1 else 'after-refresh'}

    machine._op_computer_info = read
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-projects li')"),
            ('eval', 'initiallyClosed', "!document.querySelector('#code-computer-dialog').open"),
            ('eval', 'before', 'location.hash'), ('click', f'#code-agents li[data-id="{aid}"] .code-agent'),
            ('wait', SHOWN), ('eval', 'details', "document.querySelector('.code-computer-details').textContent"),
            ('eval', 'fits', FITS), ('click', '#code-computer-refresh'),
            ('wait', "document.querySelector('.code-computer-details')?.textContent.includes('after-refresh')"),
            ('eval', 'stored', "JSON.stringify([{...localStorage}, {...sessionStorage}]).includes('test-worker')"),
            ('eval', 'close', "document.querySelector('#code-computer-dialog').close();true"),
            ('wait', "!document.querySelector('#code-computer-dialog').open"),
            ('eval', 'after', "[location.hash, !!document.querySelector('#code-projects li')]"),
            ('wait', "!document.querySelector('.code-computer-details')")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    values = result['values']
    assert values['initiallyClosed'] and values['fits'] and not values['stored']
    assert values['after'] == [values['before'], True]
    for text in ('Имя компьютера', 'WORKSTATION', 'Имя пользователя', 'test-worker', 'Внешний IP', '8.8.8.8',
                 'Внутренние IP', '10.77.1.8', '10.8.0.4', 'Windows 11', '12 ГиБ из 16 ГиБ (75%)',
                 'C:\\ — свободно 100 ГиБ из 500 ГиБ', 'D:\\ — нет данных', 'C:\\work\\projects', 'D:\\Проекты'):
        assert text in values['details']
    assert len(reads) == 2
    assert not problems(result), problems(result)


def test_computer_info_waits_for_pin_and_reopens_after_expiration(browser, live):
    login = 'computer-info-pin'
    live.add_user(login)
    machine = computer(pin='2468')

    def read(req, events):
        machine.open_sessions.clear()  # Следующее обновление уже потребует PIN.
        return machine.computer_data

    machine._op_computer_info = read
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-pin-input')"),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent'),
            ('eval', 'closed', "!document.querySelector('#code-computer-dialog').open"),
            ('type', '#code-pin-input', '2468'), ('click', '#code-pin-open'), ('wait', SHOWN),
            ('click', '#code-computer-refresh'),
            ('wait', "!document.querySelector('#code-computer-dialog').open && !!document.querySelector('#code-pin-input')"),
            ('type', '#code-pin-input', '2468'), ('click', '#code-pin-open'), ('wait', SHOWN),
            ('eval', 'reopened', "document.querySelector('.code-computer-details').textContent.includes('WORKSTATION')")])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['closed'] and result['values']['reopened']
    assert sum(req['op'] == 'computer_info' for req in machine.requests) == 3


def test_old_and_offline_computers_explain_why_snapshot_is_unavailable(browser, live):
    login = 'computer-info-old'
    live.add_user(login)
    machine = FakeMachine({'alpha': {}})
    aid, server = code_online(live, login, machine)
    offline, _ = enroll_live(live, login, 'Спящий компьютер')
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-projects li')"),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent'),
            ('wait', "document.querySelector('#code-computer-dialog').open"),
            ('eval', 'old', "document.querySelector('.code-computer-content').textContent"),
            ('eval', 'disabled', "document.querySelector('#code-computer-refresh').disabled"),
            ('eval', 'close', "document.querySelector('#code-computer-dialog').close();true"),
            ('click', f'#code-agents li[data-id="{offline}"] .code-agent'),
            ('wait', "document.querySelector('#code-computer-dialog').open"),
            ('eval', 'offline', "document.querySelector('.code-computer-content').textContent")])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert 'Обновите агент' in result['values']['old'] and result['values']['disabled']
    assert 'Компьютер не на связи' in result['values']['offline']
    assert not any(req['op'] == 'computer_info' for req in machine.requests)
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 393])
def test_computer_card_in_webkit(webkit, live, width, tmp_path):
    login = f'computer-info-webkit-{width}'
    live.add_user(login)
    machine = computer()
    aid, server = code_online(live, login, machine, name='Компьютер с длинным именем')
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with webkit.new_context(viewport={'width': width, 'height': 852}, is_mobile=True, has_touch=True,
                                extra_http_headers={'Cookie': cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(live.url + '/#/code')
            page.wait_for_selector('#code-projects li')
            page.locator(f'#code-agents li[data-id="{aid}"] .code-agent').tap()
            page.wait_for_function('() => ' + SHOWN)
            assert page.evaluate(FITS)
            assert 'WORKSTATION' in page.locator('.code-computer-details').inner_text()
            page.screenshot(path=str(tmp_path / f'computer-info-{width}.png'))
            page.get_by_role('button', name='Закрыть', exact=True).tap()
            assert not page.locator('#code-computer-dialog').evaluate('e => e.open')
    finally:
        server.stop()
