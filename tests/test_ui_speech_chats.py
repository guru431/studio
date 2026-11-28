"""Озвучка: несколько записей, голоса, правка и устойчивый плеер на телефоне и компьютере."""
import pytest

from fakes import WAV_FAKE
from test_ui_browser import (API, NO_OVERFLOW, READY, add_gateway, add_voice, browser, go, is_open, live,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def test_drop_text_into_speech_uses_the_composer_draft(browser, live):
    login = 'speech-drop'
    live.add_user(login)
    drop = """(() => {
      const data = new DataTransfer();
      const target = document.querySelector('.speech .tr-view');
      const event = (type) => new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: data});
      data.items.add(new File(['Вторая строка.'], 'speech.txt', {type: 'text/plain'}));
      target.dispatchEvent(event('dragover'));
      const highlighted = document.querySelector('.speech .file-drop-active') !== null;
      return highlighted && !target.dispatchEvent(event('drop'));
    })()"""
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), add_gateway(),
        *go('speech'), ('wait', "document.querySelector('#speech-attach:not([disabled])')"),
        ('type', '#speech-text', 'Черновик.'), ('eval', 'drop', drop),
        ('wait', "document.querySelector('#speech-text').value === 'Черновик.\\n\\nВторая строка.'"),
        ('eval', 'cleared', "!document.querySelector('.speech .file-drop-active')")], width=1280)
    assert not result.get('failed') and not problems(result), result
    assert result['values']['drop'] and result['values']['cleared']


@pytest.mark.parametrize('width', [320, 1280])
def test_continue_and_edit_speech_chat(browser, live, width):
    login = f'speech-chat-{width}'
    live.add_user(login)
    choose_other = "document.querySelector('#chip-voice').value = [...document.querySelector('#chip-voice').options].find(o => o.textContent === 'Другой голос').value; true"
    r = browser.drive(live.url + '/', [*signin(login), ('wait', READY), add_gateway(), add_voice(), add_voice(),
        ('eval', 'rename', f"(async () => {{ const m = {API}; const v = (await m.api('GET','/api/voices')).items; return m.api('PATCH', '/api/voices/' + v[1].id, {{name:'Другой голос'}}); }})()"),
        *go('settings'), ('wait', "document.querySelector('.settings-nav')"),
        *go('speech'), ('wait', "document.querySelectorAll('#chip-voice option').length === 2"),
        ('eval', 'choose', "document.querySelector('#chip-voice').value = [...document.querySelector('#chip-voice').options].find(o => o.textContent === 'Мой голос').value; true"),
        ('type', '#speech-text', 'Первая запись.'), ('click', '#speech-go'),
        ('wait', "document.querySelector('.sp-record audio[src^=\"/api/media/\"]') && !document.querySelector('#speech-go').disabled"),
        ('eval', 'first', "(() => { window.firstAudio = document.querySelector('.sp-record audio'); return firstAudio.src; })()"),
        ('eval', 'time', f"(async () => {{ const m = {API}; const c = (await m.api('GET','/api/speeches/chats')).items[0]; const t = await import('/static/js/time.js'); return [document.querySelector('.sp-item .tr-state').textContent, t.timeOnly(c.created)]; }})()"),
        ('eval', 'other', choose_other), ('type', '#speech-text', 'Продолжение.'), ('click', '#speech-go'),
        ('wait', "document.querySelectorAll('.sp-record audio[src^=\"/api/media/\"]').length === 2 && !document.querySelector('#speech-go').disabled"),
        ('eval', 'continued', "({rows:document.querySelectorAll('.sp-item').length, voices:[...document.querySelectorAll('.sp-voice-name')].map(n=>n.textContent), kept:firstAudio === document.querySelector('.sp-record audio') && firstAudio.isConnected})"),
        ('type', '#speech-text', 'Мой черновик'), ('click', '.sp-record .sp-edit'),
        ('wait', "!document.querySelector('#speech-edit-cancel').hidden"),
        ('eval', 'editing', "[document.querySelector('#speech-text').value, document.querySelector('#chip-voice').selectedOptions[0].textContent]"),
        ('click', '#speech-edit-cancel'), ('eval', 'draft', "document.querySelector('#speech-text').value"),
        ('click', '.sp-record .sp-edit'), ('wait', "!document.querySelector('#speech-edit-cancel').hidden"),
        ('eval', 'replace', "(() => { const t = document.querySelector('#speech-text'); t.value='Исправленная запись.'; t.dispatchEvent(new Event('input', {bubbles:true})); return true; })()"),
        ('eval', 'voice', choose_other), ('click', '#speech-go'),
        ('wait', "document.querySelector('.sp-text')?.textContent === 'Исправленная запись.' && document.querySelectorAll('.sp-record audio').length === 2 && !document.querySelector('#speech-go').disabled"),
        ('eval', 'edited', "({texts:[...document.querySelectorAll('.sp-text')].map(n=>n.textContent), voices:[...document.querySelectorAll('.sp-voice-name')].map(n=>n.textContent), src:document.querySelector('.sp-record audio').src, draft:document.querySelector('#speech-text').value})"),
        ('click', '#sp-new'), ('wait', "!document.querySelector('.sp-record')"),
        *([('click', '.split-toggle')] if width == 320 else []), ('click', '.sp-item'),
        ('wait', "document.querySelectorAll('.sp-record audio').length === 2"),
        ('eval', 'fits', NO_OVERFLOW)], width=width, height=900, mobile=width == 320)
    assert not r.get('failed'), r
    v = r['values']
    assert v['time'][0] == v['time'][1]
    assert v['continued'] == {'rows': 1, 'voices': ['Голос: Мой голос', 'Голос: Другой голос'], 'kept': True}
    assert v['editing'] == ['Первая запись.', 'Мой голос'] and v['draft'] == 'Мой черновик'
    assert v['edited']['texts'] == ['Исправленная запись.', 'Продолжение.']
    assert v['edited']['voices'] == ['Голос: Другой голос'] * 2
    assert v['edited']['src'] != v['first'] and v['edited']['draft'] == 'Мой черновик'
    assert v['fits'] and not problems(r), problems(r)


LONG_MAX = 200_000          # speeches.LONG_MAX
FILL = "(() => {{ const t = document.querySelector('#speech-text'); t.value = {0!r}; t.dispatchEvent(new Event('input', {{bubbles:true}})); return true; }})()"


@pytest.mark.parametrize('width', [320, 1280])
def test_failed_revoice_long_text_new_chat_and_whole_chat_delete(browser, live, width):
    """Отказ «Озвучить заново» — прежний плеер со словами ошибки, «ошибка» в списке; длинный текст не режется,
    а закрывает «Озвучить»; «Новый чат» набранное не стирает; «Удалить чат» — весь чат одним окном (04:F4, F11, F12)."""
    login = f'speech-tidy-{width}'
    live.add_user(login)
    gw = live.gw
    gw.tts_replies = [(200, WAV_FAKE)] * (len(gw.tts_requests) + 1) + [(500, {'detail': 'сбой'})]
    try:
        r = browser.drive(live.url + '/', [*signin(login), ('wait', READY), add_gateway(), add_voice(),
            *go('settings'), ('wait', "document.querySelector('.settings-nav')"),
            *go('speech'), ('wait', "document.querySelector('#chip-voice option')"),
            ('type', '#speech-text', 'Первая запись.'), ('click', '#speech-go'),
            ('wait', "document.querySelector('.sp-record audio[src^=\"/api/media/\"]') && !document.querySelector('#speech-go').disabled"),
            ('eval', 'first', "document.querySelector('.sp-record audio').src"),
            ('click', '.sp-record .sp-edit'), ('wait', "!document.querySelector('#speech-edit-cancel').hidden"),
            ('eval', 'replace', FILL.format('Правка записи.')), ('click', '#speech-go'),
            ('wait', "document.querySelector('.sp-record p.error') && document.querySelector('.sp-item[data-state=error]')"),
            ('eval', 'failed', "({text: document.querySelector('.sp-text').textContent, src: document.querySelector('.sp-record audio')?.src,"
                               " state: document.querySelector('.sp-item .tr-state').textContent.includes(' · ')})"),
            ('eval', 'long', FILL.format('я' * (LONG_MAX + 1))),     # длиннее записи — частями; длиннее LONG_MAX — отказ
            ('eval', 'over', "({length: document.querySelector('#speech-text').value.length, go: document.querySelector('#speech-go').disabled,"
                             " words: !document.querySelector('#speech-over').hidden,"
                             " red: document.querySelector('#speech-count').classList.contains('error')})"),
            ('eval', 'short', FILL.format('Набранное')),
            ('click', '#sp-new'), ('wait', "!document.querySelector('.sp-record')"),
            ('eval', 'kept', "[document.querySelector('#speech-text').value, document.querySelector('#sp-delete-chat').hidden]"),
            *([('click', '.split-toggle')] if width == 320 else []), ('click', '.sp-item'),
            ('wait', "document.querySelector('.sp-record') && !document.querySelector('#sp-delete-chat').hidden"),
            ('eval', 'fits', NO_OVERFLOW),
            ('click', '#sp-delete-chat'), ('wait', is_open('#sp-delete-chat-dialog')),
            ('click', '#sp-delete-chat-confirm'),
            ('wait', "!document.querySelector('.sp-item') && !document.querySelector('.sp-record')"
                     " && document.querySelector('#sp-delete-chat').hidden")],
            width=width, height=900, mobile=width == 320)
    finally:
        gw.tts_replies = [(200, WAV_FAKE)]
    assert not r.get('failed'), r
    v = r['values']
    assert v['failed'] == {'text': 'Первая запись.', 'src': v['first'], 'state': True}
    assert v['over'] == {'length': LONG_MAX + 1, 'go': True, 'words': True, 'red': True}
    assert v['kept'] == ['Набранное', True] and v['fits']
    assert any(s.startswith('DELETE /api/speeches/chats/') for s in r['sent'])
    assert not problems(r), problems(r)
