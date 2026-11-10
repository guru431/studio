"""Размеры кнопок статуса, поэлементный перенос и подписанное меню на сенсоре."""
import httpx
import pytest

from fake_bridge import T0, pid
from fakes import PNG_REAL
from test_ui_browser import CODE_CHAT_A, code_free_machine, code_online, code_session, live
from test_ui_desktop import desktop_browser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 393, 600, 1280])
def test_status_buttons_keep_shape_and_overflow_labels_and_counts(desktop_browser, live, width, tmp_path):
    engine = desktop_browser.browser_type.name
    login = f'status-fit-{engine}-{width}'
    live.add_user(login)
    message = {'id': 'answer', 'role': 'assistant', 'text': 'Готово', 'time': T0, 'steps': [],
        'progress': {'changes': {'files': [{'path': f'file-{n}.py'} for n in range(5)]}}}
    machine = code_free_machine({CODE_CHAT_A: code_session('Статус', messages=[message])})
    machine.subagents = machine.chat_fork = True
    aid, channel = code_online(live, login, machine)
    # Сервер фикстуры — локальный HTTP; загрузка системного TLS-хранилища Windows не требуется.
    signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'}, verify=False)
    cookies = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
    try:
        with desktop_browser.new_context(viewport={'width': width, 'height': 850},
                has_touch=width < 760, is_mobile=width < 760 and engine != 'firefox',
                extra_http_headers={'Cookie': cookies}) as context:
            if engine == 'firefox':
                context.add_cookies([{'name': key, 'value': value, 'url': live.url, 'secure': True,
                    'httpOnly': True, 'sameSite': 'Lax'} for key, value in signed.cookies.items()])
            page = context.new_page()
            page.set_default_timeout(10000)
            page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}/{CODE_CHAT_A}')
            page.locator('#code-chat[data-loaded="1"]').wait_for()
            page.wait_for_function("() => document.querySelector('#code-changed-files .code-changed-count').textContent === '5'")
            for scale, font in [(1, 1), (1, 1.5), (.75, 1.25)]:
                page.evaluate("({scale,font}) => {const r=document.documentElement; r.style.zoom=scale;"
                    "r.style.setProperty('--ui-scale',scale); r.style.setProperty('--font-scale',font);}",
                    {'scale': scale, 'font': font})
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                shape = page.locator('.code-status-controls').evaluate("""controls => {
                  const bar=controls.closest('.code-status-bar').getBoundingClientRect();
                  const circle=document.querySelector('#code-context').getBoundingClientRect();
                  const fixed=[...controls.querySelectorAll('.code-status-fixed > button')]
                    .filter(n=>!n.hidden).map(n=>n.getBoundingClientRect());
                  const visible=[...controls.querySelectorAll('button')].filter(n=>!n.hidden && n.getClientRects().length);
                  const rects=visible.map(n=>n.getBoundingClientRect());
                  return {round:Math.abs(circle.width-circle.height)<1, minimum:fixed.length>=6,
                    files:!!controls.querySelector('.code-status-fixed > #code-changed-files'),
                    half:rects.every(r=>r.left>=bar.left+bar.width/2-1 && r.right<=bar.right+1),
                    separate:rects.every((r,i)=>!i || r.left>=rects[i-1].right-1)};
                }""")
                assert all(shape.values()), shape
            page.evaluate("document.documentElement.style.zoom=1; document.documentElement.style.setProperty('--ui-scale',1);"
                "document.documentElement.style.setProperty('--font-scale',1)")
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            more = page.locator('#code-status-more')
            if width == 600:
                assert page.locator('.code-chat-actions > button:not([hidden])').count() == 2
            if more.is_visible():
                more.tap() if width < 760 and engine != 'firefox' else more.click()
                panel = page.locator('#code-status-menu:popover-open')
                panel.wait_for()
                assert panel.locator('.code-status-action-label:visible').count() == panel.locator('.code-chat-actions > button:visible').count()
                assert all(panel.locator('.code-status-action-label:visible').all_text_contents())
                assert panel.locator('#code-changed-files').count() == 0
                page.screenshot(path=str(tmp_path / f'status-menu-{engine}-{width}.png'))
            page.keyboard.press('Escape')
            page.keyboard.press('Escape')
            changed = page.locator('.code-status-fixed > #code-changed-files')
            assert changed.locator('.code-changed-count').inner_text() == '5'
            changed.tap() if width < 760 and engine != 'firefox' else changed.click()
            page.locator('#code-changed-dialog').wait_for()
            assert page.locator('.code-changed-file').count() == 5
            page.locator('#code-changed-dialog .dialog-close').click()
            page.locator('#code-image-input').set_input_files({'name': 'example.png', 'mimeType': 'image/png', 'buffer': PNG_REAL})
            page.locator('#code-text').fill('Длинное уточнение к изображению.\n' * 25)
            for height in [850, 580]:
                page.set_viewport_size({'width': width, 'height': height})
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                bounds = page.locator('#code-composer').evaluate("""form => {
                  const r=form.getBoundingClientRect(), area=form.closest('.code-compose-area').getBoundingClientRect();
                  const nav=document.querySelector('#rail').getBoundingClientRect();
                  return {left:r.left>=area.left, right:r.right<=area.right,
                    top:r.top>=area.top, bottom:r.bottom<=area.bottom,
                    screen:r.left>=0 && r.right<=innerWidth && r.bottom<=(innerWidth<=760 ? nav.top : innerHeight),
                    horizontal:form.scrollWidth<=form.clientWidth};
                }""")
                assert all(bounds.values()), bounds
            page.screenshot(path=str(tmp_path / f'composer-frame-{engine}-{width}.png'))
            page.evaluate("document.querySelectorAll('.code-chat-actions > button').forEach(button => {button.hidden=true;})")
            page.wait_for_function("() => document.querySelector('#code-status-more').hidden")
    finally:
        channel.stop()
