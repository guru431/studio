"""Владелец настраивает MCP и SMTP обычными полями на телефоне и компьютере."""
import pytest
import httpx

from fake_bridge import FakeMachine
from test_ui_browser import browser, live, signin, READY, NO_OVERFLOW, problems, set_value, code_online, code_requests

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.mark.parametrize('width', [320, 1280])
def test_mcp_and_smtp_settings_forms_save_without_showing_password(browser, live, width):
    login = f'mcp-smtp-{width}'
    live.add_user(login, is_admin=True)
    steps = [*signin(login), ('wait', READY),
        ('eval', 'nav', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-add')"),
        ('click', '#mcp-add'), ('type', '#mcp-name', 'Tools'), ('type', '#mcp-url', 'http://10.77.1.15:4000/mcp'),
        ('type', '#mcp-token', 'private-token'), ('click', '#mcp-save'),
        ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ('eval', 'mcpFits', NO_OVERFLOW),
        ('eval', 'nav', "location.hash='#/settings/smtp'"), ('wait', "document.querySelector('#smtp-save')?.disabled === false"),
        set_value('#smtp-host', 'smtp.example.com'), set_value('#smtp-port', '465'), set_value('#smtp-tls', 'ssl'),
        set_value('#smtp-username', 'studio'), set_value('#smtp-password', 'private-smtp-password'),
        set_value('#smtp-sender', 'studio@example.com'), ('click', '#smtp-save'),
        ('wait', "document.querySelector('#smtp-status')?.textContent === 'Настройки SMTP сохранены'"),
        ('eval', 'passwordCleared', "document.querySelector('#smtp-password').value === ''"),
        ('eval', 'smtpFits', NO_OVERFLOW),
        ('eval', 'nav', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-servers [data-mcp-id]')"),
        ('eval', 'savedName', "document.querySelector('#mcp-servers b').textContent")]
    result = browser.drive(live.url + '/', steps, width=width, mobile=width == 320)
    assert not result.get('failed'), result
    assert result['values']['mcpFits'] and result['values']['smtpFits'] and result['values']['passwordCleared']
    assert result['values']['savedName'] == 'Tools'
    assert not problems(result), result.get('log')


def test_agent_mcp_fields_save_to_selected_project(browser, live):
    class Machine(FakeMachine):
        config = {'mcpServers': {}}

        @property
        def hello(self):
            return {**super().hello, 'mcp': True}

        def _op_mcp_get(self, req, events):
            return self.config

        def _op_mcp_save(self, req, events):
            self.config = req['config']
            return {}

    login = 'mcp-agent-ui'; live.add_user(login)
    machine = Machine(projects={'alpha': {}}, pin=None)
    aid, sb = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
            ('eval', 'nav', "location.hash='#/settings/code'"), ('wait', "document.querySelector('#mcp-pin')?.parentElement.parentElement.hidden === false"),
            ('click', '#agent-mcp-settings button:last-child'),
            ('wait', "document.querySelector('#agent-mcp-add')?.disabled === false"),
            ('click', '#agent-mcp-add'), ('type', '#mcp-name', 'local-tools'), set_value('#mcp-type', 'stdio'),
            ('type', '#mcp-command', 'python'), ('type', '#mcp-args', '-m\nexample'), ('type', '#mcp-env', 'KEY=private'),
            ('click', '#mcp-save'), ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#agent-mcp-servers b')"),
            ('eval', 'fits', NO_OVERFLOW)], width=320, mobile=True)
        assert not result.get('failed'), result
        assert result['values']['fits']
        saved = code_requests(machine, 'mcp_save')[-1]
        assert saved['project'] and saved['engine'] == 'claude'
        assert saved['config']['mcpServers']['local-tools']['env'] == {'KEY': 'private'}
    finally:
        sb.stop()


def test_chat_mcp_selection_is_saved_for_the_current_dialog(browser, live):
    import auth
    from fakes import TOKEN
    login = 'mcp-chat-selection'; live.add_user(login)
    with httpx.Client(base_url=live.url, headers={'Origin':live.url}) as client:
        assert client.post('/login', data={'login':login,'password':'password-123'}).status_code == 303
        # Chromium принимает Secure cookie на localhost; httpx в тестовом HTTP — только явно.
        for cookie in client.cookies.jar:
            cookie.secure = False
        client.headers[auth.CSRF_HEADER] = client.get('/api/me').json()['csrf']
        connection = client.post('/api/connections', json={'name':'Model','kind':'srvllm',
            'base_url':'http://10.77.1.15:4000','token':TOKEN,'sections':['chat']})
        assert connection.status_code == 201, connection.text
        server = client.post('/api/mcp', json={'name':'Tools','url':'http://10.77.1.15:4000/mcp'})
        sid = server.json()['id']
        created = client.post('/api/chats', json={'connection_id':connection.json()['id'],'model':'local-gemma-abl'})
        assert created.status_code == 201, created.text
        cid = created.json()['id']
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
            ('eval','nav',f"location.hash='#/chat/{cid}'"), ('wait', "document.querySelector('#mcp-toggle')?.hidden === false"),
            ('click','#mcp-toggle'), ('wait', "document.querySelector('#chat-mcp-dialog')?.open"),
            ('click',f'#chat-mcp-{sid}'), ('click','#chat-mcp-save'),
            ('wait',"!document.querySelector('#chat-mcp-dialog') && document.querySelector('#mcp-toggle').textContent === 'MCP (1)'"),
            ('eval','fits',NO_OVERFLOW)], width=320, mobile=True)
        assert not result.get('failed'), result
        assert result['values']['fits']
        assert client.get(f'/api/chats/{cid}').json()['chat']['mcp_ids'] == [sid]
