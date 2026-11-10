"""Основные поля разделов: единый вид, нижнее положение и настоящие действия вложений."""
import pytest

from fake_bridge import pid
from fakes import PNG_REAL
from test_ui_browser import (CODE_CHAT_A, add_gateway, add_translator, code_free_machine,
    code_online, code_session, live)
from test_ui_design_tokens import cookie_for, engine, shell
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.mark.parametrize('width', [320, 393, 1280])
def test_main_composers_share_styles_and_remain_below_scrolling_content(engine, live, width, tmp_path):
    login = f'composer-{engine.browser_type.name}-{width}'
    cookie = cookie_for(live, login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Поле ввода')})
    aid, channel = code_online(live, login, machine)
    try:
        context, page = shell(engine, live, cookie, width=width, touch=width < 760, route='#/photo')
        with context:
            page.evaluate(add_gateway()[2])
            page.evaluate(add_translator()[2])
            common = None
            for section, field, upload in [
                ('chat', '#chat-input', '.attach'), ('photo', '#photo-prompt', '#chip-photo'),
                ('video', '#video-prompt', '#video-pick'), ('speech', '#speech-text', None),
                ('translate', '#tl-input', '#tl-attach'),
                (f'code/{aid}/{pid("alpha")}/{CODE_CHAT_A}', '#code-text', '#code-attach'),
            ]:
                page.evaluate('section => {location.hash="#/"+section}', section)
                input_field = page.locator(field)
                pw.expect(input_field).to_be_visible()
                form = input_field.locator('xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," composer ")]')
                styles = input_field.evaluate('e => {const s=getComputedStyle(e),f=getComputedStyle(e.closest(".composer"));'
                    'return [s.fontFamily,s.fontSize,s.lineHeight,s.paddingTop,s.paddingBottom,s.minHeight,f.padding,f.borderRadius];}')
                if common is None:
                    common = styles
                assert styles == common, (section, styles, common)
                input_field.fill('Проверка')
                assert form.evaluate('f => {const t=f.querySelector(".field-tools"),c=[...t.children].filter(e=>!e.hidden);'
                    'return getComputedStyle(t).flexDirection==="column" && c.every((e,i)=>!i || '
                    'e.getBoundingClientRect().top>=c[i-1].getBoundingClientRect().bottom-1);}')
                if upload:
                    trigger = form.locator(upload)
                    if section == 'translate':
                        trigger.click()
                        trigger = page.locator('#tl-attach-menu').get_by_role('menuitem', name='Файл', exact=True)
                    with page.expect_file_chooser():
                        trigger.click()
                form.locator('.field-clear').click()
                pw.expect(input_field).to_have_value('')
                # A long result must scroll above the composer, preserving its position.
                page.evaluate('()=>{const s=document.querySelector(".messages,.photo-feed,.video-feed,.sp-view,.tl-view,.code-chat-scroll");'
                    'const n=document.createElement("div");n.style.height="2000px";n.style.flex="none";s.append(n);s.scrollTop=1000;}')
                for height, font, scale in [(852, 1, 1), (580, 1.25, 1), (580, 1.5, .75)]:
                    page.set_viewport_size({'width': width, 'height': height})
                    page.evaluate('({font,scale})=>{const r=document.documentElement;r.style.zoom=scale;'
                        'r.style.setProperty("--ui-scale",scale);r.style.setProperty("--font-scale",font);}',
                        {'font': font, 'scale': scale})
                    input_field.fill('Длинный текст запроса.\n' * 30)
                    page.evaluate('() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    bounds = form.evaluate('f=>{const r=f.getBoundingClientRect(),rail=document.querySelector("#rail").getBoundingClientRect();'
                        'return {left:r.left,right:r.right,bottom:r.bottom,nav:rail.top,width:innerWidth,height:innerHeight,'
                        'scroll:f.scrollWidth,client:f.clientWidth};}')
                    assert bounds['left'] >= 0 and bounds['right'] <= bounds['width'] + 1 and bounds['bottom'] <= (
                        bounds['nav'] if width <= 760 else bounds['height']) + 1 and bounds['scroll'] <= bounds['client'] + 1, f'{section} {height} {font} {scale} {bounds}'
                    assert form.evaluate('f=>{const r=f.getBoundingClientRect(),p=f.parentElement.getBoundingClientRect();'
                        'return Math.abs(r.bottom-p.bottom)<28;}'), (section, height)
                page.screenshot(path=str(tmp_path / f'composer-{section.split("/")[0]}-{width}.png'))
                page.evaluate('()=>{const r=document.documentElement;r.style.zoom=1;r.style.setProperty("--ui-scale",1);'
                    'r.style.setProperty("--font-scale",1);}')
                page.set_viewport_size({'width': width, 'height': 852})
    finally:
        channel.stop()


def test_translate_attachment_picker_and_clear_keep_source_actions(engine, live):
    cookie = cookie_for(live, f'composer-files-{engine.browser_type.name}')
    context, page = shell(engine, live, cookie, width=393, touch=True)
    with context:
        page.evaluate(add_translator()[2])
        page.evaluate('location.hash="#/translate"')
        page.locator('#tl-attach').wait_for()
        page.locator('#tl-photo-input').set_input_files({'name': 'source.png', 'mimeType': 'image/png', 'buffer': PNG_REAL})
        pw.expect(page.locator('#tl-chip')).to_contain_text('source.png')
        pw.expect(page.locator('#tl-attach')).to_be_visible()
        page.locator('#tl-chip-x').click()
        pw.expect(page.locator('#tl-input')).to_be_visible()
        pw.expect(page.locator('#tl-input')).to_have_value('')
