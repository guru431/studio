"""Выбор компьютера и отдельная кнопка сведений: телефон, рабочий стол, PIN и старый мост."""
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


def test_headless_linux_terminal_shows_only_explicit_launch_links(browser, live):
    login = 'linux-terminal'
    live.add_user(login)
    machine = FakeMachine({'alpha': {}}, pin='2468')
    machine.platform, machine.terminal = 'linux', True
    machine._op_terminal_info = lambda *_: {'ssh_uri': 'ssh://user@host.example.net:2222',
                                            'web_url': 'https://host.example.net:9090/system/terminal'}
    aid, server = code_online(live, login, machine)
    row = f'#code-agents li[data-id="{aid}"]'
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "!!document.querySelector('#code-pin-input')"),
            ('click', row + ' .code-agent-terminal'),
            ('eval', 'pin', "!document.querySelector('#code-terminal-dialog')?.open"),
            ('type', '#code-pin-input', '2468'), ('click', '#code-pin-open'),
            ('wait', "!!document.querySelector('#code-terminal-dialog[open] input')"),
            ('eval', 'links', "Array.from(document.querySelectorAll('#code-terminal-dialog a'), a=>a.href)"),
            ('eval', 'command', "document.querySelector('#code-terminal-dialog input').value"),
            ('eval', 'headless', "!document.querySelector('.code-agent-desktop')")], width=320, mobile=True)
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values'] == {'go': '#/code', 'pin': True, 'headless': True, 'command': 'ssh -p 2222 user@host.example.net',
        'links': ['ssh://user@host.example.net:2222', 'https://host.example.net:9090/system/terminal']}
    unexpected = [line for line in problems(result) if not (
        'status of 409 ' in line and line.endswith(f'/api/code/agents/{aid}/open)'))]
    assert not unexpected, unexpected


SHOWN = "document.querySelector('#code-computer-dialog')?.open && document.querySelector('.code-computer-details')"
FITS = """(() => {
  const dialog = document.querySelector('#code-computer-dialog'), bounds = dialog.getBoundingClientRect();
  return bounds.left >= 0 && bounds.right <= innerWidth + 1 && bounds.top >= 0
    && bounds.bottom <= innerHeight + 1 && dialog.scrollWidth <= dialog.clientWidth + 1
    && document.documentElement.scrollWidth <= innerWidth;
})()"""


def test_macos_bundled_claude_offers_an_explicit_native_install(browser, live):
    login = "macos-bundled-claude"
    live.add_user(login)
    machine = computer()
    machine.platform = "darwin"
    machine.software_versions = machine.software_update_all = True
    machine.software_data = {
        "items": [{"id": "claude", "installed": True, "version": "2.1.310", "source": "sdk",
                   "latest": "2.1.327", "update_available": True, "can_update": True}],
        "update": {"tool": None, "state": "idle"},
    }
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', '!!document.querySelector("#code-projects li")'),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-info'),
            ('wait', 'document.querySelector("[data-software-update=claude]")?.textContent === "Установить"'),
            ('eval', 'batchDisabled', 'document.querySelector("#code-software-update-all").disabled'),
            ('click', '[data-software-update=claude]'),
            ('wait', '!!document.querySelector("[data-software-cancel]")')])
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["batchDisabled"]
    assert [r["software"] for r in machine.requests if r["op"] == "software_update"] == ["claude"]


@pytest.mark.parametrize('width', [320, 568, 1280])
def test_software_versions_update_progress_and_verified_result(browser, live, width, tmp_path):
    login = f'software-versions-{width}'
    live.add_user(login)
    machine = computer()
    if width == 568:
        machine.computer_data.update(
            os='Microsoft Windows 11 Enterprise LTSC 10.0.26100',
            internal_ips=['10.77.1.8'], projects_roots=['D:\\work'])
    machine.software_versions = True
    machine.software_update_all = width == 320
    machine.software_data = {'items': [
        {'id': tool, 'installed': tool != 'opencode', 'version': '1.2.10' if tool == 'claude' else '1.2.3' if tool == 'codex' else None,
         'cli_version': '1.2.10' if tool == 'claude' else '1.2.3' if tool == 'codex' else None, 'latest': '1.2.10',
         'source': 'vscode' if tool == 'claude' else 'npm', 'update_available': tool == 'codex',
         'can_update': tool == 'codex', 'reason': 'not_installed' if tool == 'opencode' else None}
        for tool in ('codex', 'claude', 'opencode')], 'update': {'tool': None, 'state': 'idle'}}
    if width == 568:
        machine.software_data['items'][0].update(source='vscode', cli_version='0.169.0-alpha.11.1')
    polls = []
    def progress(req, events):
        if machine.software_data['update']['state'] in ('waiting', 'updating'):
            polls.append(req)
            machine.software_data['update']['state'] = 'updating' if len(polls) == 1 else 'done'
            if len(polls) > 1:
                machine.software_data['items'][0].update(version='1.2.10', cli_version='1.2.10',
                    update_available=False, can_update=False)
        return machine.software_data
    machine._op_software_versions = progress
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', '!!document.querySelector("#code-projects li")'),
            ('eval', 'theme', f"document.documentElement.dataset.theme = '{'light' if width == 568 else 'dark'}'; true"),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-info'),
            ('wait', 'document.querySelectorAll("[data-software-update]").length === 3'),
            ('eval', 'versions', 'document.querySelector(".code-software").textContent'),
            ('eval', 'initialColors', '[...document.querySelectorAll(".code-software-version")].map(e => [e.dataset.state,getComputedStyle(e).color])'),
            ('eval', 'missing', 'document.querySelector("[data-software-update=opencode]").disabled'),
            ('eval', 'fits', FITS),
            ('eval', 'layout', '''(() => {
              const dialog = document.querySelector('#code-computer-dialog');
              const content = document.querySelector('.code-computer-content');
              const actions = dialog.querySelector('.actions').getBoundingClientRect();
              return {full: content.scrollHeight <= content.clientHeight + 1
                  && dialog.scrollHeight <= dialog.clientHeight + 1,
                actions: actions.top >= 0 && actions.bottom <= innerHeight};
            })()'''),
            ('eval', 'softwareScroll', 'document.querySelector(".code-software").scrollIntoView({block:"start"});true'),
            ('screenshot', str(tmp_path / f'software-{width}.png')),
            ('click', '#code-software-update-all' if width == 320 else '[data-software-update=codex]'),
            ('wait', 'document.querySelector("[data-software-cancel]")'),
            ('wait', 'document.querySelector(".code-software").textContent.includes("версия проверена")'),
            ('eval', 'result', 'document.querySelector(".code-software").textContent'),
            ('eval', 'updatedColor', 'getComputedStyle(document.querySelector(".code-software-version")).color'),
            ('eval', 'disabled', 'document.querySelector("[data-software-update=codex]").disabled'),
            ('eval', 'close', 'document.querySelector("#code-computer-dialog").close();true'),
            ('wait', '!document.querySelector(".code-software-row")')], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['missing'] and result['values']['fits'] and result['values']['disabled']
    assert result['values']['layout']['actions']
    if width == 568:
        assert result['values']['layout']['full']
    colors = result['values']['initialColors']
    assert [entry[0] for entry in colors] == ['outdated', 'current', 'unknown']
    assert len({entry[1] for entry in colors}) == 3
    assert result['values']['updatedColor'] == colors[1][1]
    for text in ('Codex', 'Claude Code', 'OpenCode', 'Установлено: 1.2.3', 'Актуальная версия: 1.2.10',
                 'Расширение VS Code', 'Не установлено'):
        assert text in result['values']['versions']
    assert 'Установлено: 1.2.10' in result['values']['result']
    assert sum(r['op'] == 'software_update' for r in machine.requests) == 1
    assert [r['software'] for r in machine.requests if r['op'] == 'software_update'] == ['all' if width == 320 else 'codex']
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 1280])
def test_deepseek_harness_row_in_software_versions(browser, live, width):
    """Четвёртый помощник в «Программах»: имя, предварительный выпуск, ручное обновление Desktop/Python."""
    login = f'software-deepseek-{width}'
    live.add_user(login)
    machine = computer()
    machine.software_versions = True
    machine.software_data = {'items': [
        {'id': 'deepseek', 'installed': True, 'version': '0.1.0-rc.6', 'cli_version': '0.1.0-rc.6',
         'latest': '0.1.0-rc.7', 'source': 'native', 'update_available': True, 'can_update': False, 'reason': 'manual'},
        {'id': 'codex', 'installed': True, 'version': '1.2.3', 'cli_version': '1.2.3', 'latest': '1.2.3',
         'source': 'npm', 'update_available': False, 'can_update': False, 'reason': None}],
        'update': {'tool': None, 'state': 'idle'}}
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', '!!document.querySelector("#code-projects li")'),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-info'),
            ('wait', 'document.querySelectorAll("[data-software-update]").length === 2'),
            ('eval', 'row', 'document.querySelector("[data-software-update=deepseek]").closest(".code-software-row").textContent'),
            ('eval', 'disabled', 'document.querySelector("[data-software-update=deepseek]").disabled'),
            ('eval', 'fits', FITS)], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    values = result['values']
    for text in ('DeepSeek Harness', 'Установлено: 0.1.0-rc.6', 'Актуальная версия: 0.1.0-rc.7',
                 'Обновите программу на компьютере'):
        assert text in values['row'], values['row']
    assert values['disabled'] and values['fits']
    assert not any(req['op'] == 'software_update' for req in machine.requests)
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 1280])
def test_computer_name_switches_projects_without_opening_info(browser, live, width):
    login = f'computer-select-{width}'
    live.add_user(login)
    first = computer()
    second = computer()
    second.projects = {'beta': {}}
    _, first_server = code_online(live, login, first)
    aid, second_server = code_online(live, login, second, name='Другой компьютер')
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "document.querySelector('.code-project-name')?.textContent === 'alpha'"),
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent'),
            ('wait', "document.querySelector('.code-project-name')?.textContent === 'beta'"),
            ('eval', 'closed', "!document.querySelector('#code-computer-dialog')?.open"),
            ('eval', 'selected', f"document.querySelector('#code-agents li[data-id=\"{aid}\"] .code-agent').getAttribute('aria-current')")],
            width=width, mobile=width == 320)
    finally:
        second_server.stop()
        first_server.stop()
    assert not result.get('failed'), result
    assert result['values']['closed']
    assert result['values']['selected'] == 'true'
    assert not any(req['op'] == 'computer_info' for machine in (first, second) for req in machine.requests)
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 1280])
def test_computer_info_button_shows_snapshot_refreshes_and_keeps_project(browser, live, width):
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
            ('eval', 'initiallyClosed', "!document.querySelector('#code-computer-dialog')?.open"),
            ('eval', 'before', 'location.hash'), ('click', f'#code-agents li[data-id="{aid}"] .code-agent-info'),
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
                 'свободно 4 ГиБ',
                 'C:\\ — свободно 100 ГиБ из 500 ГиБ', 'D:\\ — нет данных', 'C:\\work\\projects', 'D:\\Проекты'):
        assert text in values['details']
    assert 'Доступно памяти' not in values['details']
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
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-info'),
            ('eval', 'closed', "!document.querySelector('#code-computer-dialog')?.open"),
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
            ('click', f'#code-agents li[data-id="{aid}"] .code-agent-info'),
            ('wait', "document.querySelector('#code-computer-dialog').open"),
            ('eval', 'old', "document.querySelector('.code-computer-content').textContent"),
            ('eval', 'disabled', "document.querySelector('#code-computer-refresh').disabled"),
            ('eval', 'close', "document.querySelector('#code-computer-dialog').close();true"),
            ('click', f'#code-agents li[data-id="{offline}"] .code-agent-info'),
            ('wait', "document.querySelector('#code-computer-dialog').open"),
            ('eval', 'offline', "document.querySelector('.code-computer-content').textContent")])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert 'Обновите мост' in result['values']['old'] and result['values']['disabled']
    assert 'Компьютер не на связи' in result['values']['offline']
    assert not any(req['op'] == 'computer_info' for req in machine.requests)
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 393])
def test_computer_card_in_webkit(webkit, live, width, tmp_path):
    login = f'computer-info-webkit-{width}'
    live.add_user(login)
    machine = computer()
    machine.software_versions = True
    machine.software_data = {'items': [{'id': 'codex', 'installed': True, 'version': '26.1002.51308',
        'cli_version': '0.162.0-alpha.2', 'latest': '26.1111.21308', 'source': 'vscode',
        'update_available': True, 'can_update': True, 'reason': None}],
        'update': {'tool': None, 'state': 'idle'}}
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
            row = page.locator(f'#code-agents li[data-id="{aid}"]')
            row.locator('.code-agent').tap()
            page.wait_for_selector('#code-projects li')
            assert page.locator('#code-computer-dialog[open]').count() == 0
            controls = [row.locator(selector).bounding_box() for selector in
                        ('.code-agent', '.code-agent-update', '.code-agent-info', '.code-agent-desktop')]
            assert all(bounds['width'] > 0 and bounds['height'] >= 30 for bounds in controls)
            assert all(left['x'] + left['width'] <= right['x'] + 1 for left, right in zip(controls, controls[1:]))
            assert controls[-1]['x'] + controls[-1]['width'] <= width
            page.screenshot(path=str(tmp_path / f'computer-buttons-{width}.png'))
            row.get_by_role('button', name='Сведения о компьютере · Компьютер с длинным именем', exact=True).tap()
            page.wait_for_function('() => ' + SHOWN)
            assert page.evaluate(FITS)
            assert 'WORKSTATION' in page.locator('.code-computer-details').inner_text()
            update = page.locator('[data-software-update=codex]')
            update.wait_for()
            assert update.is_enabled() and '0.162.0-alpha.2' in page.locator('.code-software').inner_text()
            update.scroll_into_view_if_needed()
            assert page.evaluate(FITS)
            page.screenshot(path=str(tmp_path / f'computer-info-{width}.png'))
            page.set_viewport_size({'width': width, 'height': 420})
            page.evaluate("document.documentElement.style.setProperty('--font-scale', '1.5')")
            assert page.evaluate(FITS)
            assert page.locator('.code-computer-content').evaluate('e => e.scrollHeight > e.clientHeight')
            assert page.locator('.code-computer-content').bounding_box()['height'] >= 120
            for selector in ('.code-computer-heading', '.code-computer-footer'):
                bounds = page.locator(selector).bounding_box()
                assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 421
            page.screenshot(path=str(tmp_path / f'computer-info-short-{width}.png'))
            page.get_by_role('button', name='Закрыть', exact=True).tap()
            assert not page.locator('#code-computer-dialog').evaluate('e => e.open')
    finally:
        server.stop()
