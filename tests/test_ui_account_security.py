"""«Настройки → Защита»: сессии с «Завершить», журнал безопасности и аварийный выход на 320 и 1280 px.
Аварийный выход стирает только HTTP-кэш (Clear-Site-Data: "cache"): кэш оболочки offline и localStorage живы."""
import time
import uuid

import httpx
import pytest

import db
import security
import security_log
from test_ui_browser import READY, browser, go, live, problems, set_value, signin  # noqa: F401
from test_ui_chat_focus import focus_browser
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

SHELL = "caches.keys().then(keys => keys.some(k => k.startsWith('studio-shell-')))"


@pytest.mark.parametrize('width', [320, 1280])
def test_admin_password_reset_notice_leads_to_security_and_journal_shows_collapsed_failures(browser, live, width, tmp_path):
    """После сброса пароля администратором: окно-совет один раз (закрытие снимает флаг на сервере), «Открыть
    «Защиту»» ведёт в «Защиту»; свёрнутые неудачи в журнале — одной строкой «×N»."""
    login = f'password-reset-{width}'
    uid = live.add_user(login)
    conn = db.connect(live.db_path)
    now = time.time()
    conn.execute("UPDATE users SET password_reset = ? WHERE id = ?", (now - 60, uid))
    conn.execute("INSERT INTO security_events(user_id, created, kind, detail, ip, count, first)"
                 " VALUES (?, ?, 'login_fail', 'login', '203.0.113.5', 5, ?)", (uid, now - 30, now - 600))
    conn.close()
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('wait', "document.querySelector('#password-reset-dialog')?.open"),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth'),
        ('screenshot', str(tmp_path / f'password-reset-{width}.png'), '#password-reset-dialog'),
        ('click', '#password-reset-open'),
        ('wait', "location.hash === '#/settings/security' && !document.querySelector('#password-reset-dialog')"),
        ('wait', "document.querySelector('#security-events li[data-kind=login_fail]')"),
        ('eval', 'row', "document.querySelector('#security-events li[data-kind=login_fail]').textContent"),
        ('wait', "fetch('/api/me').then(r => r.json()).then(m => m.password_reset === null)")],
        width=width, mobile=width == 320)
    assert not result.get('failed'), result
    values = result['values']
    assert not values['overflow']
    assert '×5' in values['row'] and '203.0.113.5' in values['row']
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 1280])
def test_sessions_journal_and_emergency_exit(browser, live, width, tmp_path):
    login = f'account-security-{width}'
    uid = live.add_user(login)
    conn = db.connect(live.db_path)
    now = time.time()
    conn.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed, seen, created, device, active)"
                 " VALUES (?, ?, 'c', ?, ?, ?, ?, 'phone', ?)", (f'phone-{width}', uid, now + 86400, now, now, now - 60, now))
    conn.close()
    rows = "document.querySelectorAll('#sessions .session').length"
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), ('wait', SHELL),
        ('eval', 'flag', "localStorage.setItem('studio-test-flag', '1'); true"),
        *go('settings/security'), ('wait', f'{rows} === 2'),
        ('eval', 'listed', "[...document.querySelectorAll('#sessions .session-device')].map(n => n.textContent)"),
        ('wait', "document.querySelector('#security-events li[data-kind=login]')"),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth'),
        ('eval', 'shown', "document.querySelector('.sessions-block').scrollIntoView(); true"),
        ('screenshot', str(tmp_path / f'sessions-{width}.png'), '.sessions-block'),
        ('eval', 'shown', "document.querySelector('.emergency-block').scrollIntoView(); true"),
        ('screenshot', str(tmp_path / f'emergency-{width}.png'), '.emergency-block'),
        ('click', '#sessions .session-end'), ('click', '#session-end-confirm'),
        ('wait', f"{rows} === 1 && document.querySelector('#sessions-status').textContent === 'Сессия завершена'"),
        ('eval', 'journal', "fetch('/api/me/security-events').then(r => r.json()).then(p => p.items.map(i => i.kind))"),
        ('click', '#emergency-start'),
        ('wait', "document.querySelector('#emergency-status').textContent === 'Введите пароль учётки'"),
        set_value('#emergency-password', 'wrong-password'), ('click', '#emergency-start'),
        ('click', '#emergency-confirm'),
        ('wait', "document.querySelector('#emergency-dialog .error').textContent === 'Пароль неверен'"),
        ('click', '#emergency-dialog .dialog-close'),
        set_value('#emergency-password', 'password-123'), ('click', '#emergency-start'),
        ('click', '#emergency-confirm'),
        ('wait', "location.pathname === '/login'"),
        ('eval', 'me', "fetch('/api/me').then(r => r.status)"),
        ('eval', 'shell', SHELL),
        ('eval', 'kept', "localStorage.getItem('studio-test-flag')")],
        width=width, mobile=width == 320)
    assert not result.get('failed'), result
    values = result['values']
    assert values['listed'] == ['Компьютер', 'Телефон']
    assert not values['overflow']
    assert values['journal'][:2] == ['session_end', 'login']
    assert values['me'] == 401
    assert values['shell'] is True and values['kept'] == '1'
    conn = db.connect(live.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0] == 0
    finally:
        conn.close()
    # ожидаемые отказы: неверный пароль аварийного выхода (403) и проверка входа после него (401)
    expected = ("status of 403 (Forbidden) (http://localhost", "/api/me/emergency)"), ("status of 401", "/api/me)")
    left = [p for p in problems(result) if not any(a in p and p.endswith(b) for a, b in expected)]
    assert not left, left


@pytest.mark.parametrize('width', [320, 1280])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_detailed_sessions_can_be_named_refreshed_and_ended_with_keyboard_and_touch(focus_browser, live, width, theme, tmp_path):
    engine = focus_browser.browser_type.name
    mobile = width == 320 and engine != 'firefox'
    login = 'devices-' + uuid.uuid4().hex[:16]
    uid = live.add_user(login)
    with httpx.Client(base_url=live.url) as client:
        signed = client.post('/login', data={'login': login, 'password': 'password-123'})
        assert signed.status_code == 303
        cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
        conn = db.connect(live.db_path)
        now = time.time()
        other = security.new_token()
        digest = security.digest(other)
        sid = security_log.handle(digest)
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, uid))
        conn.execute("INSERT INTO sessions(digest,user_id,csrf,expires,renewed,created,active,device,browser,os,model,ip)"
                     " VALUES(?,?,'c',?,?,?,?, 'phone','Safari 18.6','iOS 18.6.2','iPhone','2001:db8:85a3::8a2e:370:7334')",
                     (digest, uid, now + 86400, now, now - 3600, now - 60))
        conn.close()
        with focus_browser.new_context(viewport={'width': width, 'height': 900}, is_mobile=mobile, has_touch=mobile,
                                       extra_http_headers={'Cookie': cookie}, service_workers='block') as context:
            if engine == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                                     'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(live.url + '/#/settings/security')
            other_row = page.locator(f'#sessions [data-id="{sid}"]')
            pw.expect(other_row).to_contain_text('Safari 18.6 · iOS 18.6.2')
            pw.expect(other_row).to_contain_text('2001:db8:85a3::8a2e:370:7334')
            pw.expect(page.locator('.session-current')).to_have_text('Это устройство')
            assert page.locator('#sessions .session-end').count() == 1
            rename = other_row.locator('.session-rename')
            (rename.tap if mobile else rename.click)()
            pw.expect(page.locator('#session-name-dialog')).to_be_visible()
            assert page.evaluate("document.activeElement.id !== 'session-name'")  # не поднимаем клавиатуру телефона
            page.locator('#session-name').fill('Личный телефон <script>window.deviceXss=1</script>')
            if mobile:
                page.locator('#session-name-save').tap()
            else:
                page.locator('#session-name').press('Enter')
            pw.expect(other_row.locator('.session-device')).to_have_text('Личный телефон <script>window.deviceXss=1</script>')
            assert page.evaluate('!window.deviceXss')
            # Пользовательское имя остаётся после обновления страницы, а браузер и модель — отдельно.
            page.reload()
            pw.expect(other_row).to_contain_text('iPhone · Safari 18.6 · iOS 18.6.2')
            pw.expect(other_row.locator('.session-device')).to_contain_text('Личный телефон')
            other_row.locator('.session-rename').click()
            page.locator('#session-name').fill('Д' * 80)
            page.locator('#session-name-save').click()
            pw.expect(other_row.locator('.session-device')).to_have_text('Д' * 80)
            other_row.scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('.sessions-block').screenshot(path=str(tmp_path / f'devices-{engine}-{width}-{theme}.png'))
            # Текущую сессию тоже можно назвать; её выход остаётся отдельным действием.
            current_row = page.locator('#sessions .session').filter(has=page.locator('.session-current'))
            current_row.locator('.session-rename').click()
            page.locator('#session-name').fill('Это мой браузер')
            page.locator('#session-name-save').click()
            pw.expect(current_row.locator('.session-device')).to_have_text('Это мой браузер')
            pw.expect(current_row.locator('.session-end')).to_have_count(0)
            # Очистка имени возвращает модель; при завершении показаны сведения выбранной сессии.
            other_row.locator('.session-rename').click()
            page.locator('#session-name').fill('')
            page.locator('#session-name-save').click()
            pw.expect(other_row.locator('.session-device')).to_have_text('iPhone')
            if width == 320 and theme == 'light':
                # Запоздавший ответ «Обновить» не должен затереть имя, сохранённое следующим запросом.
                baseline = page.evaluate("fetch('/api/me/sessions').then(r => r.json())")
                held = []

                def hold_first(route):
                    if not held:
                        held.append(route)
                    else:
                        route.continue_()

                page.route('**/api/me/sessions', hold_first)
                page.locator('#sessions-refresh').click()
                pw.expect(page.locator('#sessions-refresh')).to_be_disabled()
                other_row.locator('.session-rename').click()
                page.locator('#session-name').fill('Проверенное имя')
                # Ошибка сохранения оставляет окно открытым и позволяет повторить действие.
                def fail_once(route):
                    page.unroute('**/api/me/sessions/' + sid, fail_once)
                    route.fulfill(status=503, json={'detail': {'code': 'temporary', 'message': 'Временный отказ'}})

                page.route('**/api/me/sessions/' + sid, fail_once)
                page.locator('#session-name-save').click()
                pw.expect(page.locator('#session-name-dialog .error').last).to_have_text('Временный отказ')
                pw.expect(page.locator('#session-name-save')).to_be_enabled()
                pw.expect(other_row.locator('.session-device')).to_have_text('iPhone')
                page.locator('#session-name-save').click()
                pw.expect(other_row.locator('.session-device')).to_have_text('Проверенное имя')
                assert len(held) == 1
                with page.expect_response(lambda response: response.url.endswith('/api/me/sessions')):
                    held[0].fulfill(json=baseline)
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                pw.expect(other_row.locator('.session-device')).to_have_text('Проверенное имя')
                page.unroute('**/api/me/sessions', hold_first)
            other_row.locator('.session-end').click()
            pw.expect(page.locator('#session-end-dialog')).to_contain_text('Safari 18.6')
            pw.expect(page.locator('#session-end-dialog')).to_contain_text('2001:db8:85a3::8a2e:370:7334')
            page.locator('#session-end-dialog .dialog-close').click()
            pw.expect(other_row).to_be_visible()
            other_row.locator('.session-end').click()
            page.locator('#session-end-confirm').click()
            pw.expect(other_row).to_have_count(0)
            page.locator('#sessions-refresh').click()
            pw.expect(page.locator('#sessions .session')).to_have_count(1)
            assert not errors, errors
        conn = db.connect(live.db_path)
        try:
            assert conn.execute('SELECT 1 FROM sessions WHERE digest = ?', (digest,)).fetchone() is None
        finally:
            conn.close()
