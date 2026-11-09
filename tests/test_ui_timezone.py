"""Часовой пояс учётки: настройки, сохранение и календарные границы независимо от браузера."""
from datetime import datetime, timezone

import httpx
import pytest

from fake_bridge import FakeMachine, pid
from test_ui_browser import READY, browser, code_online, go, live, problems, set_value, signin
from test_ui_webkit import webkit

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
def test_timezone_setting_persists_and_applies_to_projects(browser, live, width, tmp_path):
    login = f'timezone-settings-{width}'
    live.add_user(login)
    stamp = 1_767_216_645  # 2025-12-31 21:30:45 UTC
    machine = FakeMachine({'alpha': {'summary': {'modified': stamp, 'checked_at': stamp, 'git': 'ok'}}})
    machine.project_summary = True
    _, server = code_online(live, login, machine)
    ready = "document.querySelector('#timezone') && !document.querySelector('#timezone').disabled"
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/appearance'),
            ('wait', ready), ('eval', 'default', "document.querySelector('#timezone').value"),
            set_value('#timezone', 'America/New_York'), ('click', '#timezone-save'),
            ('wait', "document.querySelector('#timezone-status').textContent === 'Часовой пояс сохранён'"),
            ('eval', 'profile', "fetch('/api/me').then(r => r.json()).then(me => me.timezone)"),
            ('screenshot', str(tmp_path / f'timezone-{width}.png'), 'section:has(> .field #timezone)'),
            ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth'),
            ('eval', 'reload', 'location.reload(); true'), ('wait', READY), ('wait', ready),
            ('eval', 'restored', "document.querySelector('#timezone').value"),
            ('eval', 'clock', f'Date.now = () => {stamp * 1000}; true'), *go('code'),
            ('wait', "document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"),
            ('eval', 'dates', f"""[document.querySelector("[data-pid='{pid('alpha')}'] .code-project-modified").textContent,
              document.querySelector('#code-project-updated').textContent]""")],
            width=width, mobile=width == 320, timezone='Asia/Tokyo')
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['default'] == 'Europe/Moscow'
    assert result['values']['profile'] == result['values']['restored'] == 'America/New_York'
    assert result['values']['dates'] == ['31.12.25, 16:30', 'Обновлено: 31.12.2025, 16:30:45']
    assert not result['values']['overflow'] and not problems(result), problems(result)


def test_timezone_setting_in_webkit(webkit, live):
    live.add_user('timezone-webkit')
    signed = httpx.post(live.url + '/login', data={'login': 'timezone-webkit', 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    with webkit.new_context(viewport={'width': 320, 'height': 740}, is_mobile=True, has_touch=True,
                            timezone_id='America/Los_Angeles', extra_http_headers={'Cookie': cookie}) as context:
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_default_timeout(10000)
        page.goto(live.url + '/#/settings')
        page.locator('.settings-nav a[data-section="appearance"]').tap()
        page.wait_for_selector('#timezone')
        assert page.locator('#timezone').input_value() == 'Europe/Moscow'
        page.locator('#timezone').select_option('Asia/Tokyo')
        page.locator('#timezone-save').tap()
        page.wait_for_function("() => document.querySelector('#timezone-status').textContent === 'Часовой пояс сохранён'")
        assert page.locator('#timezone').input_value() == 'Asia/Tokyo'
        assert page.evaluate("""async () => {
          const {dateTime} = await import('/static/js/time.js');
          return dateTime(1767216645);
        }""") == '01.01.2026, 06:30:45'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors


def test_saved_timezone_controls_dates_and_day_groups_across_dst(browser, live):
    live.add_user('timezone-calendar')
    def stamp(value):
        return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())
    cases = [
        # В браузере UTC ещё 31 декабря; в Москве уже новый год.
        ('Europe/Moscow', '2025-12-31T21:30:45', '2025-12-31T20:30:45',
         ['01.01.2026, 00:30:45', '1 января 2026 г.', '00:30', '2026-0-1', 'Сегодня', 'Вчера']),
        # Смена зимнего и летнего времени: соседние календарные дни через 23 и 25 часов.
        ('America/New_York', '2026-03-09T04:30:00', '2026-03-08T05:30:00',
         ['09.03.2026, 00:30:00', '9 марта 2026 г.', '00:30', '2026-2-9', 'Сегодня', 'Вчера']),
        ('America/New_York', '2026-11-02T05:30:00', '2026-11-01T04:30:00',
         ['02.11.2026, 00:30:00', '2 ноября 2026 г.', '00:30', '2026-10-2', 'Сегодня', 'Вчера']),
        # Пояс со смещением не на целое число часов.
        ('Asia/Kathmandu', '2025-12-31T21:30:45', '2025-12-30T21:30:45',
         ['01.01.2026, 03:15:45', '1 января 2026 г.', '03:15', '2026-0-1', 'Сегодня', 'Вчера']),
    ]
    steps = [*signin('timezone-calendar'), ('wait', READY)]
    for index, (zone, now, previous, _) in enumerate(cases):
        steps.append(('eval', str(index), f"""(async () => {{
          const {{api, loadMe}} = await import('/static/js/api.js');
          await api('PATCH', '/api/me', {{timezone: {zone!r}}});
          await loadMe();
          const {{dateTime, dateOnly, timeOnly}} = await import('/static/js/time.js');
          const {{dayKey, dayTitle}} = await import('/static/js/feed.js');
          Date.now = () => {stamp(now) * 1000};
          return [dateTime({stamp(now)}), dateOnly({stamp(now)}, {{day:'numeric', month:'long', year:'numeric'}}),
            timeOnly({stamp(now)}), dayKey({stamp(now)}), dayTitle({stamp(now)}), dayTitle({stamp(previous)})];
        }})()"""))
    result = browser.drive(live.url + '/', steps, timezone='UTC')
    assert not result.get('failed'), result
    assert result['values'] == {str(index): case[-1] for index, case in enumerate(cases)}
    assert not problems(result), problems(result)
