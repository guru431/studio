"""Возвращённые элементы оболочки: меню размеров, кнопки загрузки и индикаторы разделов."""
import pytest

from fake_bridge import FakeMachine
from test_ui_browser import add_gateway, code_online, live
from test_ui_design_tokens import cookie_for, engine, shell
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize("width", [320, 1280])
def test_avatar_sizes_are_immediate_synchronized_and_persistent(engine, live, width):
    cookie = cookie_for(live, f"restored-sizes-{engine.browser_type.name}-{width}")
    context, page = shell(engine, live, cookie, width=width, touch=width == 320)
    with context:
        page.locator('#avatar').click()
        pw.expect(page.locator('#menu-font')).to_be_visible()
        pw.expect(page.locator('#menu-scale')).to_be_visible()
        assert page.evaluate("document.querySelector('#menu-theme').getBoundingClientRect().bottom <= "
                             "document.querySelector('#menu-size-controls').getBoundingClientRect().top")
        page.locator('#menu-font').evaluate("e => {e.value='125'; e.dispatchEvent(new Event('input'));}")
        page.locator('#menu-scale').select_option('75')
        page.keyboard.press('Escape')
        page.evaluate("location.hash='#/settings/appearance'")
        pw.expect(page.locator('#appearance-font')).to_have_value('125')
        pw.expect(page.locator('#appearance-scale')).to_have_value('75')
        page.locator('#appearance-font').evaluate("e => {e.value='115'; e.dispatchEvent(new Event('input'));}")
        page.locator('#avatar').click()
        pw.expect(page.locator('#menu-font')).to_have_value('115')
        page.keyboard.press('Escape')
        if width == 320:
            page.touchscreen.tap(250, 150)
            page.touchscreen.tap(250, 150)
        else:
            page.locator('#view h1').dblclick()
        assert page.evaluate("visualViewport.scale === 1 && getComputedStyle(document.documentElement).touchAction === 'pan-x pan-y'")
        page.reload()
        page.locator('html[data-ready="1"]').wait_for(state='attached')
        assert page.evaluate("document.documentElement.style.getPropertyValue('--font-scale') === '1.15' "
                             "&& document.documentElement.style.getPropertyValue('--ui-scale') === '0.75'")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 393])
@pytest.mark.parametrize("section,upload,aside,name", [
    ('ocr', 'ocr-upload', 'ocr-aside', 'Документы'),
    ('transcribe', 'transcribe-upload', 'tr-aside', 'Записи'),
])
def test_scan_and_transcript_upload_and_list_are_visible_on_iphone(engine, live, width, section, upload, aside, name):
    cookie = cookie_for(live, f"rc-u-{engine.browser_type.name}-{section}-{width}")
    context, page = shell(engine, live, cookie, width=width, touch=True)
    with context:
        page.evaluate(add_gateway()[2])
        page.evaluate("section => location.hash='#/' + section", section)
        trigger = page.locator('#' + upload)
        pw.expect(trigger).to_be_visible()
        pw.expect(page.locator('.split-toolbar .split-toggle')).to_have_text(name)
        with page.expect_file_chooser():
            trigger.click()
        page.locator('.split-toolbar .split-toggle').click()
        pw.expect(page.locator('#' + aside)).to_be_visible()
        pw.expect(page.locator('#' + aside + ' .tr-add')).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 1280])
def test_every_section_updates_running_badges_and_more_from_jobs(engine, live, width):
    cookie = cookie_for(live, f"restored-badges-{engine.browser_type.name}-{width}")
    init = """(() => {
      const fetchOriginal=window.fetch;
      window.jobSections=['chat','photo','video','speech','transcribe','ocr','translate'];
      window.fetch=(url, options) => new URL(String(url), location.href).pathname === '/api/jobs'
        ? Promise.resolve(new Response(JSON.stringify({now:1000, push:false, finished:[],
            running:window.jobSections.map(section => ({id:section, section, state:'running', title:section,
              detail:'', started:999, finished:null, href:section+'?id=job'}))}),
              {headers:{'Content-Type':'application/json'}})) : fetchOriginal(url, options);
    })();"""
    context, page = shell(engine, live, cookie, width=width, touch=width == 320, init=init)
    with context:
        pw.expect(page.locator('#jobs')).to_have_attribute('data-running', '7')
        for section in ('chat', 'photo', 'video', 'speech', 'transcribe', 'ocr', 'translate'):
            badge = page.locator(f'#rail .tab[data-section={section}] .section-running-count')
            assert badge.text_content() == '1' and not badge.evaluate('e => e.hidden')
        if width == 320:
            pw.expect(page.locator('#mobile-running-count')).to_be_visible()
            assert page.locator('#mobile-running-count').text_content() == '5'
            page.locator('#mobile-more').click()
            pw.expect(page.locator('#mobile-section-links a[data-section=ocr] .section-running-count')).to_be_visible()
            page.locator('#mobile-sections-close').click()
        page.evaluate("async () => {window.jobSections=[]; (await import('/static/js/jobs.js')).kick();}")
        pw.expect(page.locator('#jobs')).to_have_attribute('data-running', '0')
        assert page.evaluate("[...document.querySelectorAll('.section-running-count')].every(e => e.hidden)")


@pytest.mark.parametrize("width", [320, 1280])
def test_project_search_is_first_in_sorting_panel_and_keeps_project_links_visible(engine, live, width):
    login = f"rc-project-{engine.browser_type.name}-{width}"
    cookie = cookie_for(live, login)
    machine = FakeMachine({'alpha': {}, 'beta': {}})
    machine.project_summary = True
    aid, server = code_online(live, login, machine)
    try:
        context, page = shell(engine, live, cookie, width=width, touch=width == 320, route=f'#/code/{aid}')
        with context:
            page.locator('#code-projects li').first.wait_for()
            page.keyboard.press('Control+k')
            search = page.locator('#code-project-search')
            pw.expect(search).to_be_focused()
            assert page.evaluate("document.querySelector('#code-project-sort-menu').firstElementChild.id === 'code-project-search'")
            assert page.evaluate("getComputedStyle(document.querySelector('#code-project-search')).fontSize === "
                                 "getComputedStyle(document.querySelector('.code-project-sort-option')).fontSize")
            search.fill('ALP')
            pw.expect(page.locator('#code-projects li:not([hidden]) .code-project-name')).to_have_text('alpha')
            page.keyboard.press('Escape')
            pw.expect(search).to_have_value('')
            pw.expect(page.locator('#code-project-sort-open')).to_have_attribute('aria-expanded', 'true')
            page.keyboard.press('Escape')
            pw.expect(page.locator('#code-project-sort-open')).to_have_attribute('aria-expanded', 'false')
            pw.expect(page.locator('#code-project-sort-open')).to_be_focused()
            page.locator('#code-project-sort-open').click()
            page.locator('#code-project-sort-options input[value=findings]').check()
            pw.expect(page.locator('.code-project-sort-text')).to_have_text('По количеству проблем')
            assert page.evaluate("[...document.querySelectorAll('#code-projects .code-project a')].every(link => {"
                "const r=link.getBoundingClientRect(), p=link.closest('.code-project').getBoundingClientRect();"
                "return r.left>=p.left-1 && r.right<=p.right+1;})")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        server.stop()
