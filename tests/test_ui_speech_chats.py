"""Озвучка: несколько записей, голоса, правка и устойчивый плеер на телефоне и компьютере."""
import pytest

from test_ui_browser import (API, NO_OVERFLOW, READY, add_gateway, add_voice, browser, go, live,  # noqa: F401
                             problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


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
