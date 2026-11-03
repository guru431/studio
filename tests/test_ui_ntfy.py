"""Настройки ntfy: сохранение учётки, токен без раскрытия и проверка доставки на узком экране."""
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
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/other'), ('wait', ready),
        set_value('#ntfy-server', 'https://psh.example.net'), set_value('#ntfy-topic', 'ui-private-topic'),
        set_value('#ntfy-token', 'tk-ui-private'), ('click', '#ntfy-enabled'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены'"),
        ('eval', 'saved', "fetch('/api/me/ntfy').then(r => r.json())"),
        ('eval', 'reload', 'location.reload(); true'), ('wait', READY), ('wait', ready),
        ('eval', 'restored', "[document.querySelector('#ntfy-enabled').checked, document.querySelector('#ntfy-topic').value, document.querySelector('#ntfy-token').value]"),
        ('click', '#ntfy-test'),
        ('wait', "document.querySelector('#ntfy-status').textContent.includes('Уведомление отправлено')"),
        ('screenshot', str(tmp_path / f'ntfy-{width}.png'), '#ntfy-settings'),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth'),
        ('click', '#ntfy-enabled'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены'"),
        ('eval', 'disabled', "fetch('/api/me/ntfy').then(r => r.json()).then(v => v.enabled)"),
        ('click', '#ntfy-clear-token'), ('click', '#ntfy-save'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сохранены'"),
        ('eval', 'cleared', "fetch('/api/me/ntfy').then(r => r.json()).then(v => v.has_token)"),
        ('click', '#ntfy-remove'),
        ('wait', "document.querySelector('#ntfy-status').textContent === 'Настройки ntfy сброшены'"),
        ('eval', 'reset', "document.querySelector('#ntfy-topic').value === '' && document.querySelector('#ntfy-test').disabled")],
        width=width, mobile=width == 320)
    assert not result.get('failed'), result
    assert result['values']['saved'] == {'server': 'https://psh.example.net', 'topic': 'ui-private-topic', 'enabled': True, 'has_token': True}
    assert result['values']['restored'] == [True, 'ui-private-topic', '']
    assert not result['values']['overflow'] and not result['values']['disabled'] and not result['values']['cleared']
    assert result['values']['reset']
    assert len(live.gw.ntfy_requests) == before + 1
    assert json.loads(live.gw.ntfy_requests[-1].content)['topic'] == 'ui-private-topic'
    assert not problems(result), problems(result)
