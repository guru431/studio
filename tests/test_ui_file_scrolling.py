"""Дерево, редактор и предпросмотр файлов прокручиваются независимо."""
from pathlib import Path
import re

import httpx
import pytest

from fake_bridge import FakeMachine, pid
from scans import scan_pdf
from test_ui_browser import live, code_online

pw = pytest.importorskip('playwright.sync_api')
pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [320, 1280])
def test_file_tree_and_content_keep_independent_scroll_positions(live, engine, width, tmp_path):
    login = f'file-scroll-{engine}-{width}'
    live.add_user(login, theme='dark' if width == 320 else 'light')
    files = {f'file-{number:03}.txt': b'line\n' * 400 for number in range(100)}
    files['preview.md'] = ('# Preview\n\n' + 'A paragraph.\n\n' * 200).encode()
    files['preview.pdf'] = scan_pdf('first page', 'second page')
    machine = FakeMachine({'alpha': {'files': files}})
    aid, bridge = code_online(live, login, machine)
    try:
        signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
        assert signed.status_code == 303
        cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
        with pw.sync_playwright() as driver:
            kind = getattr(driver, engine)
            if engine == 'webkit' and not Path(kind.executable_path).exists():
                pytest.skip('WebKit is not installed')
            browser = kind.launch(**({'channel': 'chrome'} if engine == 'chromium' else {}))
            try:
                with browser.new_context(viewport={'width': width, 'height': 800},
                        is_mobile=width == 320, has_touch=width == 320,
                        extra_http_headers={'Cookie': cookie}) as context:
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'{live.url}/#/code/{aid}/{pid("alpha")}?tab=files')
                    pw.expect(page.locator('#code-files > li')).to_have_count(len(files))
                    tree = page.locator('.code-file-table')
                    tree.evaluate('(node) => { node.scrollTop = 300; }')
                    before = tree.evaluate('(node) => node.scrollTop')
                    assert before > 0, 'The tree must scroll within the available height'
                    page.locator('#code-files li[data-name="file-012.txt"] > button').click()
                    page.locator('#code-editor').wait_for()
                    assert page.locator('#view').evaluate('(node) => node.scrollTop') == 0
                    if width == 1280:
                        assert abs(tree.evaluate('(node) => node.scrollTop') - before) <= 1
                        page.locator('#code-editor').evaluate('(node) => { node.scrollTop = 300; }')
                        assert tree.evaluate('(node) => node.scrollTop') == before
                        assert page.locator('#code-editor').evaluate('(node) => node.scrollTop') > 0
                        tree.evaluate('(node) => { node.scrollTop += 200; }')
                        assert page.locator('#code-editor').evaluate('(node) => node.scrollTop') == 300
                    else:
                        assert not tree.is_visible()
                        page.locator('#code-path button').first.click()
                        assert tree.is_visible()
                        assert abs(tree.evaluate('(node) => node.scrollTop') - before) <= 1
                    page.locator('#code-files li[data-name="preview.md"] > button').click()
                    pw.expect(page.locator('#code-editor')).to_be_hidden()
                    pw.expect(page.locator('#code-md-view button')).to_have_text(['Разметка', 'Исходный'])
                    page.locator('#code-md p').first.wait_for()
                    tree_before_preview = tree.evaluate('(node) => node.scrollTop')
                    page.locator('#code-file').evaluate('(node) => { node.scrollTop = 300; }')
                    assert page.locator('#code-file').evaluate('(node) => node.scrollTop') > 0
                    if width == 1280:
                        assert tree.evaluate('(node) => node.scrollTop') == tree_before_preview
                    assert page.locator('#view').evaluate('(node) => node.scrollTop') == 0
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(tmp_path / f'file-scroll-{engine}-{width}.png'))
                    page.locator('#code-path button').first.click()
                    page.set_viewport_size({'width': width, 'height': 500})
                    page.evaluate("document.documentElement.style.setProperty('--font-scale', '1.5')")
                    page.locator('#code-files li[data-name="preview.pdf"] > button').click()
                    image = page.locator('#code-pdf img')
                    pw.expect(image).to_have_attribute('src', re.compile(r'^blob:'))
                    pw.expect(image).to_have_js_property('complete', True)
                    assert image.evaluate('(node) => node.naturalWidth') > 0
                    pdf_tree = tree.evaluate('(node) => node.scrollTop')
                    page.locator('#code-file').evaluate('(node) => { node.scrollTop = 200; }')
                    assert page.locator('#code-file').evaluate('(node) => node.scrollTop') > 0
                    if width == 1280:
                        assert tree.evaluate('(node) => node.scrollTop') == pdf_tree
                    assert page.locator('#view').evaluate('(node) => node.scrollTop') == 0
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not errors
            finally:
                browser.close()
    finally:
        bridge.stop()
