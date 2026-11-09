"""«Настройки → Защита»: сессии с «Завершить», журнал безопасности и аварийный выход на 320 и 1280 px.
Аварийный выход стирает только HTTP-кэш (Clear-Site-Data: "cache"): кэш оболочки offline и localStorage живы."""
import time

import pytest

import db
from test_ui_browser import READY, browser, go, live, problems, set_value, signin  # noqa: F401

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
    assert values['listed'] == ['Это устройство', 'Телефон']
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
