"""Версия и обновление в панели на телефоне и компьютере."""
import zipfile

import httpx
import pytest

import agents
import bridge
from fake_bridge import FakeMachine
from test_ui_browser import READY, browser, code_online, go, live, problems, signin
from test_ui_webkit import webkit

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.fixture
def update_zip(tmp_path, monkeypatch):
    path = tmp_path / 'studio-agent.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('studio_agent/__init__.py', 'VERSION = "0.10.1"\n')
    monkeypatch.setattr(agents, 'AGENT_ZIP', path)
    setup = tmp_path / 'studio-agent-setup.exe'
    setup.write_bytes(b'MZ-test-installer')
    monkeypatch.setattr(agents, 'AGENT_SETUP', setup)


def machine_for_update():
    machine = FakeMachine({'alpha': {}})
    machine.agent_version, machine.agent_update = '0.10.0', True
    return machine


@pytest.mark.parametrize('width', [320, 1280])
def test_update_shows_running_version_waits_and_observes_new_version(browser, live, update_zip, width):
    login = f'agent-update-{width}'
    live.add_user(login)
    machine = machine_for_update()
    aid, server = code_online(live, login, machine, name='Компьютер с очень длинным именем')
    row = f'#code-agents li[data-id="{aid}"]'
    original = machine._op_agent_update_status
    polls = []

    def progress(req, events):
        polls.append(req['op'])
        if len(polls) == 2:
            machine.update_state = 'waiting'
        elif len(polls) >= 3:
            machine.update_state = 'restarting'
            bridge.LINKS[aid].hello['agent_version'] = '0.10.1'
        return original(req, events)

    machine._op_agent_update_status = progress
    geometry = f'''(() => {{
      const row = document.querySelector('{row}'), version = row.querySelector('.code-agent-version').getBoundingClientRect(),
        button = row.querySelector('.code-agent-update').getBoundingClientRect(), panel = row.getBoundingClientRect();
      return version.right <= button.left && button.right <= panel.right + 1 && version.width > 0
        && document.documentElement.scrollWidth <= innerWidth;
    }})()'''
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-projects li')"),
            ('eval', 'before', f"document.querySelector('{row} .code-agent-version').textContent"),
            ('eval', 'available', f"document.querySelector('{row} .code-agent-update-status').textContent"),
            ('eval', 'geometry', geometry), ('click', row + ' .code-agent-update'),
            ('wait', f"document.querySelector('{row} .code-agent-update-status')?.textContent.includes('Ждём')"),
            ('eval', 'disabled', f"document.querySelector('{row} .code-agent-update').disabled"),
            ('wait', f"document.querySelector('{row} .code-agent-version').textContent === '0.10.1'"),
            ('eval', 'complete', f"document.querySelector('{row} .code-agent-update-status') === null"),
            ('eval', 'installer', f"document.querySelector('{row} .code-agent-update').getAttribute('download')")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values'] == {'go': '#/code', 'before': '0.10.0', 'geometry': True, 'disabled': True,
                                'available': 'Доступно обновление до 0.10.1',
                                'complete': True, 'installer': 'studio-agent-setup.exe'}
    assert [req['op'] for req in machine.requests].count('agent_update') == 1
    assert not problems(result), problems(result)


def test_update_continues_after_pin_and_failure_can_be_retried(browser, live, update_zip):
    login = 'agent-update-pin'
    live.add_user(login)
    machine = machine_for_update()
    machine.pin = '2468'
    original = machine._op_agent_update

    def fail(req, events):
        original(req, events)
        machine.update_state = 'failed'
        return {'state': 'failed', 'version': '0.10.1'}

    machine._op_agent_update = fail
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-pin-input')"), ('click', '.code-agent-update'),
            ('wait', "document.querySelector('.code-agent-update-status')?.textContent.includes('Введите PIN')"),
            ('type', '#code-pin-input', '2468'), ('click', '#code-pin-open'),
            ('wait', "document.querySelector('.code-agent-update-status')?.textContent.includes('Не удалось')"),
            ('eval', 'retry', "!document.querySelector('.code-agent-update').disabled")])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['retry']
    assert [req['op'] for req in machine.requests].count('agent_update') == 1


@pytest.mark.parametrize('width', [320, 393])
def test_agent_update_geometry_and_old_agent_in_webkit(webkit, live, update_zip, width):
    login = f'agent-update-webkit-{width}'
    live.add_user(login)
    machine = machine_for_update()
    aid, server = code_online(live, login, machine, name='Очень длинное имя компьютера')
    old = FakeMachine({'demo': {}})
    old_aid, old_server = code_online(live, login, old, name='Старый агент')
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with webkit.new_context(viewport={'width': width, 'height': 852}, is_mobile=True, has_touch=True,
                                extra_http_headers={'Cookie': cookie}) as context:
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(live.url + '/#/code')
            row = page.locator(f'#code-agents li[data-id="{aid}"]')
            row.locator('.code-agent-version').wait_for()
            assert row.locator('.code-agent-version').inner_text() == '0.10.0'
            version, button, bounds = [row.locator(selector).bounding_box() for selector in
                                       ('.code-agent-version', '.code-agent-update', '.code-agent')]
            assert version['x'] + version['width'] <= button['x']
            assert button['x'] + button['width'] <= width
            assert version['y'] >= bounds['y']
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            old_row = page.locator(f'#code-agents li[data-id="{old_aid}"]')
            assert old_row.locator('.code-agent-version').inner_text() == '—'
            assert old_row.locator('.code-agent-update').evaluate('e => e.tagName') == 'A'
            assert 'Доступна версия 0.10.1' in old_row.inner_text()
            assert old_row.get_by_text('Установить новый агент').get_attribute('href') == '/api/agent/download'
            assert old_row.get_by_text('Установить новый агент').get_attribute('download') == 'studio-agent-setup.exe'
            with page.expect_download() as downloaded:
                old_row.locator('.code-agent-update').tap()
            assert downloaded.value.suggested_filename == 'studio-agent-setup.exe'
            assert downloaded.value.failure() is None
            row.locator('.code-agent-update').tap()
            page.wait_for_function("document.querySelector('.code-agent-update-status')?.textContent.includes('Скачивание')")
            assert row.locator('.code-agent-update').is_disabled()
    finally:
        server.stop()
        old_server.stop()
