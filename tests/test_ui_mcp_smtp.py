"""Владелец настраивает MCP и SMTP обычными полями на телефоне и компьютере."""
import json

import pytest
import httpx

import db
import security
import state
from fake_bridge import FakeMachine
from fakes import PNG_REAL, TOKEN as MODEL_KEY, WAV_FAKE
from test_mcp import TOKEN, URL
MCP_TOKEN = TOKEN                    # в тестах ниже имя TOKEN занято ключом шлюза
from test_mcp_sections import service
from test_ui_browser import (browser, live, signin, READY, NO_OVERFLOW, problems, put_audio, put_files, set_value,
                             code_online, code_requests, add_voice, add_gateway, open_new_chat, API, ANSWERED,
                             LAST_ANSWER, BADGE, go, is_open, wav_of)
from test_ui_section_limits import SERVICE, TEXT, UPLOADED, choose, serve, start

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.mark.parametrize("width", [320, 1280])
def test_admin_can_add_shared_models_and_mcp_for_an_ordinary_user(browser, live, width, monkeypatch, request):
    login = f"shared-user-{width}"
    model, mcp_name = f"shared-model-{width}", f"Shared tools {width}"
    api_name = f"Shared API {width}"

    def clear_shared():
        connection = db.connect(live.db_path)
        try:
            connection.execute("DELETE FROM mcp_servers WHERE name=? AND user_id IS NULL", (mcp_name,))
            connection.execute("DELETE FROM connections WHERE name=? AND user_id IS NULL", (api_name,))
        finally:
            connection.close()

    request.addfinalizer(clear_shared)
    live.add_user(login)
    service(live.gw, monkeypatch=monkeypatch)
    panel = "#shared-models-dialog"
    card = f"{panel} .link:last-child"
    result = browser.drive(live.url + "/", [*signin(), ("wait", READY), *go("admin"),
        ("click", "#admin-shared-models"), ("wait", f"document.querySelector('{panel} #link-add')"),
        ("click", f"{panel} #link-add"), ("wait", is_open("#link-dialog")),
        ("type", "#link-dialog [name=name]", api_name),
        ("type", "#link-dialog [name=base_url]", "http://10.77.1.15:4000/v1"),
        ("type", "#link-dialog [name=token]", MODEL_KEY), ("click", "#link-save"),
        ("wait", f"!{is_open('#link-dialog')} && document.querySelector('{card} [name=model]')"),
        ("click", f"{card} .link-refresh"),
        ("wait", f"document.querySelector('{card} input.model-check[value=gemma4-abl]')"),
        ("type", f"{card} [name=model]", model), ("click", f"{card} form button"),
        ("wait", f"document.querySelector('{card} input.model-check[value={model}]')?.checked"),
        ("eval", "dialogFits", f"document.querySelector('{panel}').scrollWidth <= innerWidth"),
        ("click", f"{panel} .dialog-close"), ("click", "#admin-shared-mcp #mcp-add"),
        ("type", "#mcp-name", mcp_name), ("type", "#mcp-url", URL), ("type", "#mcp-token", TOKEN),
        ("click", "#mcp-save"),
        ("wait", f"!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers').textContent.includes('{mcp_name}')"),
        ("click", "#avatar"), ("click", "#menu-logout"), ("wait", "location.pathname === '/login'"),
        *signin(login), ("wait", READY), *go("settings/chat"),
        ("wait", f"document.querySelector('input.model-default[value={model}]') && "
                 f"document.querySelector('#mcp-servers')?.textContent.includes('{mcp_name}')"),
        ("eval", "readOnly", "!document.querySelector('.link-menu, .link-url, .model-check, #mcp-servers button')"),
        ("eval", "settingsFit", NO_OVERFLOW)], width=width, mobile=width == 320)
    assert not result.get("failed"), result
    assert all(result["values"].values()), result["values"]
    assert not problems(result), result["log"]


@pytest.mark.parametrize('width', [320, 1280])
def test_mcp_uses_the_existing_photo_video_and_translate_forms(browser, live, width, monkeypatch):
    login = 'owner' if width == 1280 else 'mcp-forms-mobile'
    if login != 'owner':
        live.add_user(login)
    calls = service(live.gw, monkeypatch=monkeypatch)
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
        # снимок MCP сохраняется в историю и встаёт в ленту «Фото», а не под форму; миниатюра (media.THUMB_SIDE)
        # не увеличивает: у снимка 64 px шириной та же ширина, что у исходника
        ('wait', "document.querySelector('#photo-feed .photo-card[data-state=done] img')?.naturalWidth === 64"),
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
    # модель чипа Студии инструменту MCP не уходит: у MCP — свой каталог моделей
    assert calls[0] == ('image_generate', {'prompt': 'A photo via MCP', 'size': '1024x1024', 'n': 1})
    assert calls[1][0] == 'artifact_upload' and calls[1][1]['mime_type'] == 'image/png'
    assert calls[2][0] == 'video_estimate'
    assert {key: value for key, value in calls[2][1].items() if key != 'artifact_id'} == {
        'prompt': 'A slow pan', 'seconds': 9, 'quality': 'high', 'smooth': False}
    assert calls[3:] == [('text_translate', {'text': 'Текст для перевода', 'source_lang': 'ru', 'target_lang': 'en'})]
    assert not problems(result), result.get('log')


def test_mcp_uses_the_existing_voice_audio_and_scan_inputs(browser, live, monkeypatch):
    login = 'mcp-upload-forms'
    live.add_user(login)
    calls = service(live.gw, uploads=True, clone_models=True, monkeypatch=monkeypatch)
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
def test_mcp_requiring_a_voice_sample_offers_to_add_it_in_speech(browser, live, width):
    login = f'mcp-first-voice-{width}'
    live.add_user(login)
    model = {'type': 'string', 'enum': ['qwen3-tts'], 'default': 'qwen3-tts'}
    engine = {'type': 'string', 'enum': ['clone'], 'default': 'clone'}
    reference = {'reference_artifact_id': TEXT, 'reference_text': TEXT}
    calls = serve(live.gw, {
        'speech_synthesize': {'properties': {'text': TEXT, 'model': model, 'engine': engine, **reference},
                              'required': ['text', *reference]},
        **SERVICE,
    })
    phrase = 'Какой чудесный день!'
    result = browser.drive(live.url + '/', [*start(login), add_gateway(), *go('speech'),
        ('wait', "document.querySelector('#section-mcp-tool option[value$=\":speech_synthesize\"]')"),
        choose('speech_synthesize'), ('type', '#speech-text', phrase),
        ('eval', 'hint', "document.querySelector('#speech-voice-hint:not([hidden])')?.textContent ?? ''"),
        ('eval', 'blocked', """(() => {
          document.querySelector('#speech-composer').requestSubmit();
          return [document.querySelector('#speech-go').disabled, document.querySelector('#section-mcp-result').textContent];
        })()"""),
        ('eval', 'fits', NO_OVERFLOW),
        ('click', '#speech-add-voice'), ('wait', is_open('#voice-dialog')),
        ('type', '#voice-dialog [name=name]', 'Мой первый голос'),
        put_audio('#voice-files', wav_of(6.0), 'sample.wav'),
        ('wait', "document.querySelector('#voice-dialog [name=text]').value === 'распознано' && "
                 "!document.querySelector('#voice-save').disabled"),
        ('click', '#voice-save'),
        ('wait', "document.querySelector('#chip-voice option:checked')?.textContent === 'Мой первый голос' && "
                 "document.querySelector('#speech-go')?.offsetWidth > 0"),
        ('eval', 'draft', "document.querySelector('#speech-text').value"),
        ('click', '#speech-go'), ('wait', "document.querySelector('#section-mcp-result pre')?.textContent === 'ok'")],
        width=width, mobile=width == 320)
    assert result['values']['blocked'] == [True, ''], result
    assert not result.get('failed'), result
    assert 'образец голоса' in result['values']['hint']
    assert result['values']['fits'] and result['values']['draft'] == phrase
    assert [name for name, _ in calls] == ['artifact_upload', 'speech_synthesize']
    assert calls[1][1] == {'text': phrase, 'model': 'qwen3-tts', 'engine': 'clone', 'reference_artifact_id': UPLOADED,
                           'reference_text': 'распознано'}
    assert not problems(result), result.get('log')


def test_scan_model_choice_hides_while_an_mcp_tool_is_chosen(browser, live, monkeypatch):
    """Два движка «Сканов» — выбор модели виден; выбран инструмент MCP — скрыт (на него не влияет), «Студия» — снова виден."""
    from fakes import TOKEN as GW_TOKEN
    login = 'mcp-ocr-model'
    live.add_user(login)
    service(live.gw, uploads=True, monkeypatch=monkeypatch)
    shown = "!document.querySelector('#ocr-model').closest('.pick').hidden"
    pick = lambda value: ('eval', 'pick', "(() => { const s = document.querySelector('#section-mcp-tool');"
                          f"s.value = {value}; s.dispatchEvent(new Event('change', {{ bubbles: true }})); }})()")
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), add_gateway(),
        ('eval', 's06', f"""(async () => {{ const m = {API};
          await m.api('POST', '/api/connections', {{ name: 's06', kind: 'openai', s06: true,
            base_url: 'http://10.77.1.15:4000', token: {GW_TOKEN!r}, sections: ['ocr'] }}); return true; }})()"""),
        ('eval', 'settings', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-add')"),
        ('click', '#mcp-add'), ('type', '#mcp-name', 'Tool server'), ('type', '#mcp-url', URL),
        ('type', '#mcp-token', TOKEN), ('click', '#mcp-save'),
        ('wait', "!document.querySelector('#mcp-editor') && document.querySelector('#mcp-servers [data-mcp-id]')"),
        ('eval', 'ocr', "location.hash='#/ocr'"),
        ('wait', "document.querySelector('#ocr-model option:nth-child(2)')"
                 " && document.querySelector('#section-mcp-tool option[value$=\":document_read\"]')"),
        ('eval', 'native', shown),
        pick("[...s.options].find(o => o.value.endsWith(':document_read')).value"), ('eval', 'mcp', shown),
        pick("''"), ('eval', 'back', shown)], width=1280)
    assert not result.get('failed'), result
    assert result['values']['native'] is True and result['values']['mcp'] is False and result['values']['back'] is True
    assert not problems(result), result.get('log')


def test_unreachable_mcp_server_is_named_in_the_chat_answer_and_in_sections(browser, live):
    """Лежащий MCP-сервер: ответ чата идёт без него — под ответом «Вызовы инструментов (1)» с его именем, а не
    «Источники (0)»; раздел «Фото» говорит, что MCP недоступен, со ссылкой на настройки."""
    login = 'mcp-down'
    live.add_user(login)

    async def unreachable(request):
        raise httpx.ConnectError('down')

    live.gw.on['/down'] = unreachable
    live.gw.chat_script = [['Готово']]
    add_down = ('eval', 'mcp', f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/mcp', {{ name: 'Down', url: 'http://10.77.1.15:4000/down' }}); return true; }})()""")
    toggle = "document.querySelector('#mcp-toggle')"
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), add_gateway(), add_down,
            ('eval', 'nav', "location.hash='#/settings'"), ('wait', "document.querySelector('.settings-nav')"),
            *open_new_chat(), ('wait', "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ('wait', f"{toggle} && !{toggle}.hidden"), ('click', '#mcp-toggle'),
            # недоступный сервер окно показывает с его словами, включить его всё равно можно
            ('wait', "document.querySelector('#chat-mcp-dialog .chat-mcp-error')"
                     " && !document.querySelector('#chat-mcp-save').disabled"),
            ('eval', 'down', "document.querySelector('#chat-mcp-dialog .chat-mcp-error').textContent"),
            ('eval', 'pick', "document.querySelector('#chat-mcp-dialog .chat-mcp-error').closest('.chat-mcp-item')"
                             ".querySelector('input.chat-mcp-server').click()"),
            ('click', '#chat-mcp-save'), ('wait', "!document.querySelector('#chat-mcp-dialog')"),
            ('eval', 'chip', f"{toggle}.textContent === 'MCP (1)'"),
            ('type', '#chat-input', 'Привет'), ('press', 'Enter'),
            ('wait', f"{ANSWERED} && {LAST_ANSWER}.querySelector('details.web-steps')"),
            ('eval', 'answer', f"{LAST_ANSWER}.querySelector('.md').textContent.trim()"),
            ('eval', 'badge', f"[{BADGE}.textContent, {BADGE}.className]"),
            ('eval', 'summary', f"{LAST_ANSWER}.querySelector('details.web-steps summary').textContent"),
            ('eval', 'step', f"{LAST_ANSWER}.querySelector('details.web-steps li').textContent"),
            ('eval', 'photo', "location.hash='#/photo'"),
            ('wait', "document.querySelector('#section-mcp-notice:not([hidden])')?.offsetParent"),
            ('eval', 'notice', "[document.querySelector('#section-mcp-notice').textContent,"
                               " document.querySelector('#section-mcp-notice a').getAttribute('href')]")])
    finally:
        live.gw.chat_script = []
        live.gw.on.pop('/down', None)
    assert not result.get('failed'), result
    values = result['values']
    assert values['answer'] == 'Готово'
    assert values['down'] == 'MCP недоступен или вернул неверный ответ' and values['chip'] is True
    assert values['badge'] == ['MCP: 1 — 1 ошибка', 'web-badge bad']
    assert values['summary'] == 'Вызовы инструментов (1)'
    assert values['step'] == 'MCP · Down — MCP недоступен или вернул неверный ответ'
    assert values['notice'] == ['MCP недоступен: «Down» Настроить подключения MCP', '#/settings/chat']
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
        ('eval', 'plainLabel', "document.querySelector('#smtp-tls option[value=plain]')?.textContent"),
        set_value('#smtp-tls', 'plain'), set_value('#smtp-port', '25'), set_value('#smtp-username', ''),
        ('click', '#smtp-clear-password'), ('click', '#smtp-save'),
        ('wait', "document.querySelector('#smtp-status')?.textContent === 'Настройки SMTP сохранены'"
                 " && !document.querySelector('#smtp-clear-password').checked"),
        ('eval', 'plain', "[document.querySelector('#smtp-tls').value, document.querySelector('#smtp-port').value]"),
        ('eval', 'smtpFits', NO_OVERFLOW),
        ('eval', 'nav', "location.hash='#/settings/chat'"), ('wait', "document.querySelector('#mcp-servers [data-mcp-id]')"),
        ('eval', 'savedName', "document.querySelector('#mcp-servers b').textContent")]
    result = browser.drive(live.url + '/', steps, width=width, mobile=width == 320)
    assert not result.get('failed'), result
    assert result['values']['mcpFits'] and result['values']['smtpFits'] and result['values']['passwordCleared']
    assert result['values']['plainLabel'] == 'Без шифрования' and result['values']['plain'] == ['plain', '25']
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
        # окно диалога читает каталог: мок MCP прошлых тестов на /mcp требует свой ключ
        server = client.post('/api/mcp', json={'name':'Tools','url':URL,'token':MCP_TOKEN})
        sid = server.json()['id']
        created = client.post('/api/chats', json={'connection_id':connection.json()['id'],'model':'local-gemma-abl'})
        assert created.status_code == 201, created.text
        cid = created.json()['id']
        # общие MCP прошлых тестов того же сервера включены в диалоге по умолчанию
        shared = client.get(f'/api/chats/{cid}').json()['chat']['mcp_ids']
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
            ('eval','nav',f"location.hash='#/chat/{cid}'"), ('wait', "document.querySelector('#mcp-toggle')?.hidden === false"),
            ('click','#mcp-toggle'), ('wait', f"document.querySelector('#chat-mcp-dialog')?.open && document.querySelector('#chat-mcp-{sid}')"
                                              " && !document.querySelector('#chat-mcp-save').disabled"),
            ('click',f'#chat-mcp-{sid}'), ('click','#chat-mcp-save'),
            ('wait',"!document.querySelector('#chat-mcp-dialog') && document.querySelector('#mcp-toggle').textContent"
                    f" === 'MCP ({1 + len(shared)})'"),
            ('eval','fits',NO_OVERFLOW)], width=320, mobile=True)
        assert not result.get('failed'), result
        assert result['values']['fits']
        assert sorted(client.get(f'/api/chats/{cid}').json()['chat']['mcp_ids']) == sorted([sid, *shared])
