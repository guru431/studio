"""Настройки ntfy устройства: выключенный старт, сохранение и проверка доставки на узком экране."""
import json

import pytest

import db
import upstream
from test_ui_browser import READY, browser, go, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
def test_ntfy_settings_save_reload_test_and_disable(browser, live, monkeypatch, width, tmp_path):
    login = f'ntfy-settings-{width}'
    live.add_user(login)
    conn = db.connect(live.db_path)
    conn.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", ('["psh.example.net:443"]',))
    conn.close()
    async def resolve(host, port):
        return ['93.184.216.34']
    monkeypatch.setattr(upstream, 'RESOLVE', resolve)
    ready = "document.querySelector('#ntfy-save') && !document.querySelector('#ntfy-save').disabled"
    before = len(live.gw.ntfy_requests)
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/notifications'), ('wait', ready),
        ('eval', 'default_off', "!document.querySelector('#ntfy-enabled').checked && document.querySelector('#ntfy-test').disabled"),
        set_value('#ntfy-server', 'https://psh.example.net'), set_value('#ntfy-topic', 'ui-private-topic'),
        set_value('#ntfy-token', 'tk-ui-private'), ('click', '#ntfy-enabled'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены на этом устройстве'"),
        ('eval', 'saved', "fetch('/api/me/ntfy').then(r => r.json())"),
        ('eval', 'reload', 'location.reload(); true'), ('wait', READY), ('wait', ready),
        ('eval', 'restored', "[document.querySelector('#ntfy-enabled').checked, document.querySelector('#ntfy-topic').value, document.querySelector('#ntfy-token').value]"),
        ('eval', 'subscribe', "document.querySelector('#ntfy-subscribe').getAttribute('href')"),
        ('click', '#ntfy-test'),
        ('wait', "document.querySelector('#ntfy-status').textContent.includes('Уведомление отправлено')"),
        ('screenshot', str(tmp_path / f'ntfy-{width}.png'), '#ntfy-settings'),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth'),
        ('click', '#ntfy-enabled'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены на этом устройстве'"),
        ('eval', 'disabled', "fetch('/api/me/ntfy').then(r => r.json()).then(v => v.enabled)"),
        ('click', '#ntfy-clear-token'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены на этом устройстве'"),
        ('eval', 'cleared', "fetch('/api/me/ntfy').then(r => r.json()).then(v => v.has_token)"),
        ('click', '#ntfy-remove'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сброшены'"),
        ('eval', 'reset', "document.querySelector('#ntfy-topic').value === '' && document.querySelector('#ntfy-test').disabled")],
        width=width, mobile=width == 320)
    assert not result.get('failed'), result
    assert result['values']['default_off']
    assert result['values']['saved'] == {'server': 'https://psh.example.net', 'topic': 'ui-private-topic', 'enabled': True, 'has_token': True, 'configured': True}
    assert result['values']['subscribe'] == 'ntfy://psh.example.net/ui-private-topic'
    assert result['values']['restored'] == [True, 'ui-private-topic', '']
    assert not result['values']['overflow'] and not result['values']['disabled'] and not result['values']['cleared']
    assert result['values']['reset']
    assert len(live.gw.ntfy_requests) == before + 1
    assert json.loads(live.gw.ntfy_requests[-1].content)['topic'] == 'ui-private-topic'
    assert not problems(result), problems(result)


def test_ntfy_follows_the_device_language_after_save_and_after_switching(browser, live, monkeypatch):
    """Сохранение ntfy передаёт язык интерфейса; после смены языка страница при запуске правит язык сессии."""
    login = 'ntfy-language'
    live.add_user(login)
    conn = db.connect(live.db_path)
    conn.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", ('["psh.example.net:443"]',))
    conn.close()
    async def resolve(host, port):
        return ['93.184.216.34']
    monkeypatch.setattr(upstream, 'RESOLVE', resolve)
    ready = "document.querySelector('#ntfy-save') && !document.querySelector('#ntfy-save').disabled"
    langs = """(async () => {
      for (let i = 0; i < 50; i++) {
        const langs = (await fetch('/api/me').then(r => r.json())).notify_langs;
        if (langs.length && langs[0] === localStorage.getItem('studio-language')) return langs;
        await new Promise(r => setTimeout(r, 100));
      }
      return (await fetch('/api/me').then(r => r.json())).notify_langs;
    })()"""
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('eval', 'english', "localStorage.setItem('studio-language', 'en'); location.reload(); true"),
        ('wait', READY), *go('settings/notifications'), ('wait', ready),
        set_value('#ntfy-server', 'https://psh.example.net'), set_value('#ntfy-topic', 'ui-language-topic'),
        ('click', '#ntfy-enabled'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent !== ''"),
        ('eval', 'saved', langs),
        ('eval', 'russian', "localStorage.setItem('studio-language', 'ru'); location.reload(); true"),
        ('wait', READY), ('eval', 'switched', langs)], width=1280)
    assert not result.get('failed'), result
    assert result['values']['saved'] == ['en'] and result['values']['switched'] == ['ru']
    conn = db.connect(live.db_path)
    assert [r[0] for r in conn.execute('SELECT n.lang FROM ntfy_devices n JOIN sessions s ON s.digest = n.digest'
                                       ' JOIN users u ON u.id = s.user_id WHERE u.login = ?', (login,))] == ['ru']
    conn.close()
    assert not problems(result), problems(result)


def test_ntfy_form_remembers_server_and_topic_but_not_the_token_after_a_new_sign_in(browser, live, monkeypatch):
    """FINDINGS 06:F11: настройка ntfy уходит с сессией; после нового входа форма подставляет сервер и тему последнего
    сохранения этого браузера (localStorage `studio-ntfy:<id>`), токен вводится заново и в браузере не хранится;
    явный «Выйти» запомненное забывает."""
    login = 'ntfy-again'
    uid = live.add_user(login)
    conn = db.connect(live.db_path)
    conn.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", ('["psh.example.net:443"]',))
    conn.close()
    async def resolve(host, port):
        return ['93.184.216.34']
    monkeypatch.setattr(upstream, 'RESOLVE', resolve)
    ready = "document.querySelector('#ntfy-save') && !document.querySelector('#ntfy-save').disabled"
    form = ("[document.querySelector('#ntfy-server').value, document.querySelector('#ntfy-topic').value,"
            " document.querySelector('#ntfy-token').value, document.querySelector('#ntfy-enabled').checked,"
            " document.querySelector('#ntfy-test').disabled]")
    first = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/notifications'), ('wait', ready),
        set_value('#ntfy-server', 'https://psh.example.net'), set_value('#ntfy-topic', 'ui-again-topic'),
        set_value('#ntfy-token', 'tk-ui-again'), ('click', '#ntfy-enabled'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены на этом устройстве'"),
        ('eval', 'stored', f"localStorage.getItem('studio-ntfy:{uid}')"),
        ('eval', 'out', "fetch('/logout', { method: 'POST' }).then((r) => r.ok)")], width=320, mobile=True)
    assert not first.get('failed'), first
    assert json.loads(first['values']['stored']) == {'server': 'https://psh.example.net', 'topic': 'ui-again-topic'}
    assert first['values']['out'] is True
    again = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/notifications'), ('wait', ready),
        ('eval', 'form', form), ('eval', 'note', "document.querySelector('#ntfy-status').textContent"),
        ('eval', 'server', "fetch('/api/me/ntfy').then(r => r.json()).then(v => v.configured)"),
        # явный «Выйти» (форма меню) забывает сервер и тему: на общем компьютере тема не остаётся
        ('eval', 'forgot', "(() => { document.querySelector('form[action=\"/logout\"]').dispatchEvent("
                           f"new Event('submit', {{ bubbles: true, cancelable: true }})); return localStorage.getItem('studio-ntfy:{uid}'); }})()"),
        ('eval', 'out', "fetch('/logout', { method: 'POST' }).then((r) => r.ok)")], width=320, mobile=True)
    assert not again.get('failed'), again
    assert again['values']['form'] == ['https://psh.example.net', 'ui-again-topic', '', False, True]
    assert again['values']['note'] == ('Сервер и тема — с прошлой настройки на этом устройстве.'
                                       ' Токен, если он нужен, введите заново и сохраните.')
    assert again['values']['server'] is False             # подстановка — только в форме, сервер ничего не сохранил
    assert again['values']['forgot'] is None
    assert not problems(first) and not problems(again), (problems(first), problems(again))
