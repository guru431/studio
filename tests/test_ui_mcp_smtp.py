"""Владелец настраивает MCP и SMTP обычными полями на телефоне и компьютере."""
import json

import pytest
import httpx

import security
import state
from fake_bridge import FakeMachine
from fakes import PNG_REAL, WAV_FAKE
from test_mcp import TOKEN, URL
from test_mcp_sections import service
from test_ui_browser import (browser, live, signin, READY, NO_OVERFLOW, problems, put_audio, put_files, set_value,
                             code_online, code_requests, add_voice)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.mark.parametrize('width', [320, 1280])
def test_mcp_uses_the_existing_photo_video_and_translate_forms(browser, live, width):
    login = 'owner' if width == 1280 else 'mcp-forms-mobile'
    if login != 'owner':
        live.add_user(login)
    calls = service(live.gw)
    choose = lambda tool: ('eval', 'tool', "(() => { const s = document.querySelector('#section-mcp-tool');"
                           f"s.value = [...s.options].find(o => o.value.endsWith(':{tool}')).value;"
                           "s.dispatchEvent(new Event('change', { bubbles: true })); })()")
    def tool_ready(tool):
        return ("document.querySelector('#section-mcp-tool:not([hidden])')?.offsetWidth > 0 && "
                f"""document.querySelector('#section-mcp-tool option[value$=":{tool}"]')""")

    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('eval', 'settings', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-add')"),
        ('click', '#mcp-add'), ('type', '#mcp-name', 'Tool server'), ('type', '#mcp-url', URL),
        ('type', '#mcp-token', TOKEN), ('click', '#mcp-save'),
        ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ('eval', 'photo', "location.hash='#/photo'"), ('wait', tool_ready('image_generate')),
        ('eval', 'provider', "document.querySelector('#photo-composer #section-mcp-tool').value"),
        ('type', '#photo-prompt', 'A photo via MCP'), choose('image_generate'),
        ('click', '#chip-size'), ('click', '#chip-size-menu [data-value=square]'),
        ('click', '#photo-go'),
        ('wait', "document.querySelector('#section-mcp-result img')?.naturalWidth === 64"),
        ('eval', 'photoFits', NO_OVERFLOW),
        ('eval', 'video', "location.hash='#/video'"), ('wait', tool_ready('video_estimate')), choose('video_estimate'),
        put_files('#video-file', [("source.png", PNG_REAL)]),
        ('type', '#video-prompt', 'A slow pan'), set_value('#video-seconds', '9'),
        ('click', '#chip-quality'), ('click', '#chip-quality-menu [data-value=high]'), ('click', '#chip-smooth'),
        ('click', '#video-go'), ('wait', "document.querySelector('#section-mcp-result pre')?.textContent === 'Estimated time'"),
        ('eval', 'videoFits', NO_OVERFLOW),
        ('eval', 'translate', "location.hash='#/translate'"), ('wait', tool_ready('text_translate')), choose('text_translate'),
        ('wait', "document.querySelector('#tl-target option[value=en]')"),
        set_value('#tl-source', 'ru'), set_value('#tl-target', 'en'),
        ('type', '#tl-input', 'Текст для перевода'), ('click', '#tl-go'),
        ('wait', "document.querySelector('#tl-output')?.textContent === 'Translated text'"),
        ('eval', 'translateFits', NO_OVERFLOW),
        ('eval', 'hasDialog', "!!document.querySelector('dialog[open]')")],
        width=width, mobile=width == 320)
    assert not result.get('failed'), result
    assert result['values']['photoFits'] and result['values']['videoFits'] and result['values']['translateFits']
    assert not result['values']['hasDialog']
    assert bool(result['values']['provider']) is (width == 320)
    model = {'model': 'snofs-krea2-v1.6'} if login == 'owner' else {}
    assert calls[0] == ('image_generate', {'prompt': 'A photo via MCP', 'size': '1024x1024', 'n': 1, **model})
    assert calls[1][0] == 'artifact_upload' and calls[1][1]['mime_type'] == 'image/png'
    assert calls[2][0] == 'video_estimate'
    assert {key: value for key, value in calls[2][1].items() if key != 'artifact_id'} == {
        'prompt': 'A slow pan', 'seconds': 9, 'quality': 'high', 'smooth': False}
    assert calls[3:] == [('text_translate', {'text': 'Текст для перевода', 'source_lang': 'ru', 'target_lang': 'en'})]
    assert not problems(result), result.get('log')


def test_mcp_uses_the_existing_voice_audio_and_scan_inputs(browser, live):
    login = 'mcp-upload-forms'
    live.add_user(login)
    calls = service(live.gw, uploads=True, clone_models=True)
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('eval', 'settings', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-add')"),
        ('click', '#mcp-add'), ('type', '#mcp-name', 'Tool server'), ('type', '#mcp-url', URL),
        ('type', '#mcp-token', TOKEN), ('click', '#mcp-save'),
        ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ('eval', 'speech', "location.hash='#/speech'"), ('wait', "document.querySelector('#speech-composer #section-mcp-tool')?.offsetWidth > 0"),
        ('type', '#speech-text', 'Read this'), set_value('#speech-composer #chip-voice', 'ExampleVoice'), ('click', '#speech-go'),
        ('wait', "document.querySelector('#section-mcp-result pre')?.textContent === 'Synthesized speech'"),
        add_voice(), ('eval', 'resetChoices', "location.hash='#/settings/chat'"),
        ('wait', "document.querySelector('#mcp-add')"), ('eval', 'clone', "location.hash='#/speech'"),
        ('wait', "document.querySelector('#chip-mcp-model')?.offsetWidth > 0 && document.querySelector('#chip-voice option')"),
        set_value('#chip-mcp-model', 'f5-tts'), ('type', '#speech-text', 'Проверка голоса'), ('click', '#speech-go'),
        ('wait', "document.querySelector('#section-mcp-result pre')?.textContent === 'Synthesized speech'"),
        ('eval', 'transcribe', "location.hash='#/transcribe'"), ('wait', "document.querySelector('#transcribe-upload')?.offsetWidth > 0"),
        put_audio('#transcribe-add input', WAV_FAKE),
        ('wait', "document.querySelector('#section-mcp-result pre')?.textContent === 'Transcribed audio'"),
        ('eval', 'ocr', "location.hash='#/ocr'"), ('wait', "document.querySelector('#ocr-upload')?.offsetWidth > 0"),
        ('eval', 'onePage', "!document.querySelector('#ocr-add input').multiple"),
        put_files('#ocr-add input', [("page.png", PNG_REAL)]),
        ('wait', "document.querySelector('#section-mcp-result pre')?.textContent === 'Recognized page'"),
        ('eval', 'fits', NO_OVERFLOW)], width=1280)
    assert not result.get('failed'), result
    assert result['values']['onePage'] and result['values']['fits']
    assert calls[0] == ('speech_synthesize', {'text': 'Read this', 'voice': 'ExampleVoice'})
    assert calls[1][0] == 'artifact_upload' and calls[1][1]['mime_type'] == 'audio/wav'
    assert calls[2][0] == 'speech_synthesize' and calls[2][1]['model'] == 'f5-tts' and calls[2][1]['engine'] == 'clone'
    assert [name for name, _ in calls[3:]] == ['artifact_upload', 'audio_transcribe', 'artifact_upload', 'document_read']
    assert [calls[i][1]['mime_type'] for i in [3, 5]] == ['audio/wav', 'image/png']
    assert not problems(result), result.get('log')


@pytest.mark.parametrize('width', [320, 1280])
def test_mcp_and_smtp_settings_forms_save_without_showing_password(browser, live, width):
    login = f'mcp-smtp-{width}'
    live.add_user(login, is_admin=True)
    steps = [*signin(login), ('wait', READY),
        ('eval', 'nav', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-add')"),
        ('click', '#mcp-add'), ('type', '#mcp-name', 'Tools'), ('type', '#mcp-url', 'http://10.77.1.15:4000/mcp'),
        ('type', '#mcp-token', 'private-token'), ('type', '#mcp-headers', 'X-Api-Key=private-key'), ('click', '#mcp-save'),
        ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        # Значение заголовка, как и токен, обратно не показывается; маска при сохранении оставляет прежнее.
        ('click', '#mcp-servers [data-mcp-id] .row button'), ('wait', "document.querySelector('#mcp-headers')"),
        ('eval', 'headers', "document.querySelector('#mcp-headers').value"),
        ('click', '#mcp-save'), ('wait', "!document.querySelector('#mcp-editor')"),
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
    assert result['values']['headers'] == 'X-Api-Key=••••••'
    owner = state.DB.execute('SELECT id FROM users WHERE login = ?', (login,)).fetchone()[0]
    sealed = state.DB.execute('SELECT secret_enc FROM mcp_servers WHERE user_id = ?', (owner,)).fetchone()[0]
    assert json.loads(security.decrypt(sealed))['headers'] == {'X-Api-Key': 'private-key'}
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
            ('eval', 'pin', "(() => { const i = document.querySelector('#mcp-pin');"
                            " return [i.type, !!i.closest('form'), getComputedStyle(i).webkitTextSecurity]; })()"),
            ('click', '#agent-mcp-settings button:last-child'),
            ('wait', "document.querySelector('#agent-mcp-add')?.disabled === false"),
            ('click', '#agent-mcp-add'), ('type', '#mcp-name', 'local-tools'), set_value('#mcp-type', 'stdio'),
            ('type', '#mcp-command', 'python'), ('type', '#mcp-args', '-m\nexample'), ('type', '#mcp-env', 'KEY=private'),
            ('click', '#mcp-save'), ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#agent-mcp-servers b')"),
            ('eval', 'fits', NO_OVERFLOW)], width=320, mobile=True)
        assert not result.get('failed'), result
        assert result['values']['fits']
        assert result['values']['pin'] == ['text', False, 'disc']  # PIN компьютера — не password и не в <form>
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
