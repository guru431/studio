"""Language selection across pages, sections, devices and browser engines."""
import json
from pathlib import Path

import httpx
import pytest

import code_actions

from test_ui_browser import (CODE_CHAT_A, READY, browser, code_chat, code_free_machine,
                             code_online, code_session, go, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize('width', [320, 1280])
def test_language_choice_persists_across_login_sections_and_reload(browser, live, width, tmp_path):
    login = f'english-{width}'
    live.add_user(login, is_admin=True)
    steps = [
        ('wait', "document.querySelector('#login-language')"),
        ('eval', 'default', 'document.documentElement.lang'),
        set_value('#login-language', 'en'),
        ('wait', "document.documentElement.lang === 'en' && document.querySelector('#login-language')?.value === 'en'"),
        ('eval', 'login', "[document.title, document.querySelector('#submit').textContent]"),
        *signin(login, 'incorrect-password')[1:],
        ('wait', "document.querySelector('p.error')?.textContent === 'Incorrect login or password'"),
        set_value('#login', login), set_value('#password', 'password-123'), ('click', '#submit'), ('wait', READY),
        ('eval', 'rail', "[...document.querySelectorAll('#rail a.tab')].map(a => a.title)"),
    ]
    sections = [('photo', 'Photos'), ('video', 'Video'), ('speech', 'Speech'),
                ('transcribe', 'Transcription'), ('ocr', 'Scans'), ('translate', 'Translation'),
                ('code', 'Agents'), ('search', 'Search'), ('settings', 'Settings'), ('admin', 'Administration')]
    for section, name in sections:
        steps += [*go(section), ('wait', f"document.title === {json.dumps(name + ' — Studio')} && !document.querySelector('#view').inert"),
                  ('eval', f'overflow-{section}', 'document.documentElement.scrollWidth > innerWidth')]
        if section == 'translate':
            steps += [('wait', "document.querySelector('#tl-target')?.options.length > 0"),
                      ('eval', 'languages', "[...document.querySelector('#tl-target').options].map(o=>o.textContent)")]
    steps += [*go('settings/appearance'), ('wait', "document.querySelector('#interface-language')"),
        ('eval', 'choice', "document.querySelector('#interface-language').value"),
        ('screenshot', str(tmp_path / f'english-appearance-{width}.png')),
        ('eval', 'reload', 'location.reload(); true'), ('wait', READY),
        ('wait', "document.querySelector('#interface-language')?.value === 'en'"),
        ('eval', 'contracts', """(async () => {
          const {el, plural} = await import('/static/js/ui.js');
          const {tr} = await import('/static/js/i18n.js');
          const {dateOnly} = await import('/static/js/time.js');
          const user = el('p', {}, 'Чат'); document.querySelector('#view').append(user);
          return [user.textContent, tr('Назвать устройство: {0}', 'Чат'),
            [1,2,5,21].map(n => plural(n, ['file','files','files'])),
            dateOnly(1767216645, {month:'long', day:'numeric', year:'numeric'})];
        })()"""),
        set_value('#interface-language', 'ru'), ('wait', READY),
        ('wait', "document.documentElement.lang === 'ru' && document.querySelector('#interface-language')?.value === 'ru'"),
        ('eval', 'restored', "[document.title, localStorage.getItem('studio-language')]"),
        ('eval', 'blockStorage', "Storage.prototype.setItem = () => { throw new Error('blocked'); }; true"),
        set_value('#interface-language', 'en'),
        ('eval', 'storageFailure', "[document.querySelector('#interface-language').value, document.querySelector('.interface-language .error').textContent]")]
    result = browser.drive(live.url + '/login#/settings', steps, width=width, mobile=width == 320, touch=width == 320)
    assert not result.get('failed'), result
    values = result['values']
    assert values['default'] == 'ru'
    assert values['login'] == ['Studio — sign in', 'Sign in']
    assert values['choice'] == 'en'
    assert 'English' in values['languages'] and 'Russian' in values['languages']
    assert values['contracts'] == ['Чат', 'Name device: Чат', ['file', 'files', 'files', 'files'], 'January 1, 2026']
    assert values['restored'] == ['Настройки — Студия', 'ru']
    assert values['storageFailure'] == ['ru', 'Браузер не разрешает сохранить язык. Разрешите хранилище сайта и повторите.']
    assert not any(value for key, value in values.items() if key.startswith('overflow-'))
    unexpected = [line for line in problems(result)
                  if not ('status of 401' in line and f'{live.url}/login' in line)]
    assert not unexpected, unexpected


@pytest.mark.parametrize('engine', ['webkit', 'firefox'])
def test_english_touch_settings_errors_and_public_pages_in_other_engines(live, engine, tmp_path):
    pw = pytest.importorskip('playwright.sync_api')
    login = f'english-{engine}'
    live.add_user(login)
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
    cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    with pw.sync_playwright() as driver:
        browser_type = getattr(driver, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip(f'{engine} unavailable')
        with browser_type.launch() as instance:
            with instance.new_context(viewport={'width': 320, 'height': 740}, has_touch=True,
                                      extra_http_headers={'Cookie': cookie}, service_workers='block') as context:
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(live.url + '/#/settings/appearance')
                page.locator('#interface-language').select_option('en')
                page.wait_for_function("() => document.documentElement.lang === 'en'")
                page.locator('#interface-language').wait_for()
                assert page.locator('#interface-language').input_value() == 'en'
                page.keyboard.press('Tab')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#interface-language').scroll_into_view_if_needed()
                page.screenshot(path=str(tmp_path / f'english-{engine}.png'))
                assert page.evaluate("""async () => {
                  const {api} = await import('/static/js/api.js');
                  try { await api('POST', '/api/me/password', {current:'incorrect', new:'long-password-123'}); }
                  catch (error) { return error.message; }
                }""") == 'current password incorrect'
                for route, title in [('/offline.html', 'Studio — offline'),
                                     ('/account/forgot', 'Studio — account'), ('/share', 'Studio — shared chat')]:
                    page.goto(live.url + route)
                    page.wait_for_function('() => document.readyState === "complete"')
                    assert page.title() == title
                    assert page.evaluate('document.documentElement.lang') == 'en'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#share-pin').fill('1234')
                page.locator('#share-open').tap()
                assert page.locator('#share-error').inner_text() == 'Invalid link'
                page.goto(live.url + '/#/settings/appearance')
                page.locator('#interface-language').select_option('ru')
                page.wait_for_function("() => document.documentElement.lang === 'ru'")
                assert not errors, errors


def test_english_default_action_labels_keep_prompts_and_custom_titles(browser, live):
    login = 'english-actions'
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Пустой чат')})
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *code_chat(aid, CODE_CHAT_A),
            ('wait', "!document.querySelector('#code-quick-actions').hidden"),
            ('eval', 'defaults', "[...document.querySelectorAll('.code-quick-action')].map(b=>b.textContent)"),
            *go('settings/code'), ('wait', "document.querySelector('.code-quick-action-row input')"),
            set_value('.code-quick-action-row input', 'Чат'), ('click', '#code-quick-actions-save'),
            ('wait', "document.querySelector('#code-quick-actions-status').textContent === 'Quick actions saved'"),
            *code_chat(aid, CODE_CHAT_A), ('wait', "!document.querySelector('#code-quick-actions').hidden"),
            ('eval', 'custom', "document.querySelector('.code-quick-action').textContent"),
            ('eval', 'visualization', """(async () => {
              const {renderMarkdownInto} = await import('/static/js/markdown.js');
              const host = document.createElement('div'); document.querySelector('#view').append(host);
              const marker = 'visualize' + JSON.stringify({path:'dashboard.html'}) + '';
              await renderMarkdownInto(host, marker + '\\n\\nParagraph.\\n\\n'.repeat(800));
              const button = host.querySelector('.code-visualization-open');
              const result = [button.textContent, button.title]; host.remove(); return result;
            })()"""),
            ('eval', 'texts', "fetch('/api/me').then(r=>r.json()).then(m=>m.code_quick_actions.map(a=>a.text))")],
            init="localStorage.setItem('studio-language', 'en')")
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['defaults'] == ['Resolve issues and ideas', 'Deep project audit', 'Independent critics']
    assert result['values']['custom'] == 'Чат'
    assert result['values']['visualization'] == ['Open visualization', 'Visualization']
    assert result['values']['texts'] == [action['text'] for action in code_actions.DEFAULTS]
    assert not problems(result), problems(result)
