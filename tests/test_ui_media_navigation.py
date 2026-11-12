"""Просмотр фото/видео: порядок, действия, страницы, клавиши и жесты во всех трёх движках."""
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from fakes import MP4, PNG_REAL
from test_ui_browser import live
from test_ui_desktop import desktop_browser
from test_ui_design_tokens import cookie_for
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def gallery(page, section, count=4):
    rows = [dict(id=f'{i:032x}', created=1700000000 - i, state='done', media_id=f'media-{i}',
                 model='lustify', size='832x1216', seed=i, prompt=f'Файл {i}', elapsed=1,
                 seconds=1, quality='fast', progress=100, prompt_source='given') for i in range(count)]
    if len(rows) > 1:
        rows[1].update(state='error' if section == 'photo' else 'failed', media_id=None)
    api_section = 'images' if section == 'photo' else 'videos'
    calls = {'deleted': [], 'pages': [], 'fail': False}

    def api(route):
        url = urlsplit(route.request.url)
        tail = url.path.split(f'/api/{api_section}', 1)[1]
        if not tail:
            query = parse_qs(url.query)
            calls['pages'].append(query)
            if calls['fail']:
                route.fulfill(status=503, json={'detail': {'code': 'offline', 'message': 'Проверка связи'}})
                return
            before = query.get('before_id', [None])[0]
            at = next((i + 1 for i, row in enumerate(rows) if row['id'] == before), 0)
            route.fulfill(json={'items': rows[at:at + 30]})
        elif tail.endswith('/variants'):
            route.fulfill(json={'siblings': [], 'children': []})
        elif route.request.method == 'DELETE':
            calls['deleted'].append(tail[1:])
            route.fulfill(status=204)
        else:
            row = next(row for row in rows if row['id'] == tail[1:])
            kind = 'generation' if section == 'photo' else 'video'
            route.fulfill(json={**row, 'chain': [{**row, 'kind': kind, 'image_id': row['id']}]})

    page.route(f'**/api/{api_section}**', api)
    page.route('**/api/media/**', lambda route: route.fulfill(
        content_type='video/mp4' if section == 'video' else 'image/png',
        body=MP4 if section == 'video' else PNG_REAL))
    return rows, calls


@pytest.fixture
def media_page(desktop_browser, live, request):
    section, width = request.param
    name = desktop_browser.browser_type.name
    login = 'gallery-' + hashlib.sha256(request.node.name.encode()).hexdigest()[:16]
    cookie = cookie_for(live, login)
    mobile = width < 760
    with desktop_browser.new_context(viewport={'width': width, 'height': 850}, has_touch=mobile,
            is_mobile=mobile and name != 'firefox', service_workers='block',
            extra_http_headers={'Cookie': cookie}) as context:
        page = context.new_page()
        page.set_default_timeout(10000)
        yield page, section, width


def open_first(page, live, section):
    page.goto(live.url + '/#/' + section)
    page.locator('html[data-ready="1"]').wait_for(state='attached')
    selector = '.card-open' if section == 'photo' else '.card-viewer'
    opener = page.locator(selector).first
    opener.click()
    page.locator('.viewer[open]').wait_for()
    return opener


def current(page, row):
    pw.expect(page.locator('.viewer[open]')).to_have_attribute('data-item', row['id'])
    pw.expect(page.locator('.viewer .chain-prompt')).to_have_text(row['prompt'])
    node = '.viewer-stage img' if page.locator('#viewer').count() else '.viewer-stage video'
    pw.expect(page.locator(node)).to_have_attribute('src', '/api/media/' + row['media_id'])


def swipe(page, *, dx=-120, dy=0, multi=False, cancel=False, controls=False):
    page.locator('.viewer-stage').evaluate("""(stage, o) => {
      const target=stage.firstElementChild, r=target.getBoundingClientRect();
      const x=r.left+r.width*.6, y=o.controls ? r.bottom-15 : r.top+r.height*.4;
      const a={identifier:1,clientX:x,clientY:y};
      const b={...a,clientX:x+o.dx,clientY:y+o.dy};
      const send=(type,touches,changedTouches=touches) => {
        const e=new Event(type,{bubbles:true,cancelable:true});
        Object.defineProperties(e,{touches:{value:touches},changedTouches:{value:changedTouches}});
        target.dispatchEvent(e);
      };
      send('touchstart',[a]);
      if(o.multi) send('touchstart',[a,{...a,identifier:2}]);
      send('touchmove',[b]);
      if(o.cancel) send('touchcancel',[],[b]);
      send('touchend',[],[b]);
    }""", dict(dx=dx, dy=dy, multi=multi, cancel=cancel, controls=controls))


def swipe_file(page, dx=-120):
    if page.context.browser.browser_type.name != 'chromium':
        swipe(page, dx=dx)
        return
    # Trusted touch input also checks native pan handling and suppression of the following click.
    session = page.context.new_cdp_session(page)
    try:
        box = page.locator('.viewer-stage').bounding_box()
        x = box['x'] + box['width'] * (.7 if dx < 0 else .3)
        y = box['y'] + box['height'] * .4
        session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
        for step in range(1, 7):
            session.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x + dx * step / 6, 'y': y}]})
            page.evaluate('() => new Promise(requestAnimationFrame)')
        session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
    finally:
        session.detach()


@pytest.mark.parametrize('media_page', [('photo', 320), ('photo', 1280), ('video', 320), ('video', 1280)], indirect=True)
def test_buttons_keyboard_touch_and_actions_follow_the_current_file(media_page, live, tmp_path):
    page, section, width = media_page
    rows, calls = gallery(page, section)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    opener = open_first(page, live, section)
    current(page, rows[0])
    pw.expect(page.locator('.viewer-prev')).to_be_disabled()
    page.locator('.viewer-next').click()
    current(page, rows[2])  # failed row is skipped
    if section == 'video':
        page.locator('video.viewer-video').evaluate("p => {window.oldPlayer=p; window.pauses=0; p.pause=()=>window.pauses++;}")
        page.locator('video.viewer-video').focus()
        page.keyboard.press('ArrowLeft')
        current(page, rows[2])  # native player keys retain their meaning
    page.locator('.viewer-next').focus()
    page.keyboard.press('ArrowRight')
    current(page, rows[3])
    if section == 'video':
        assert page.evaluate('window.pauses') >= 1
        assert page.evaluate('!window.oldPlayer.isConnected')
    if page.locator('.viewer-next').is_enabled():
        page.locator('.viewer-next').click()  # prove the end when the feed has not requested an empty page
        pw.expect(page.locator('.viewer-position')).to_have_text('3 / 3')
        current(page, rows[3])
    pw.expect(page.locator('.viewer-next')).to_be_disabled()
    page.keyboard.press('ArrowLeft')
    current(page, rows[2])
    if width == 320:
        for options in [dict(dx=10, dy=120), dict(multi=True), dict(cancel=True)]:
            swipe(page, **options)
            current(page, rows[2])
        if section == 'photo':
            page.locator('.viewer-stage img').tap()
            pw.expect(page.locator('#viewer')).to_have_class('viewer full')
            swipe(page)
            current(page, rows[2])
            page.locator('.viewer-stage img').tap()
        else:
            swipe(page, controls=True)
            current(page, rows[2])
        swipe_file(page, dx=120)
        current(page, rows[0])
        swipe_file(page)
        current(page, rows[2])
    for theme in ('light', 'dark'):
        page.evaluate('theme => document.documentElement.dataset.theme=theme', theme)
        assert page.locator('.viewer-top').evaluate("e=>[...e.querySelectorAll('button')].every(b=>{const r=b.getBoundingClientRect();"
            "return r.left>=0 && r.right<=innerWidth && r.width>=40 && r.height>=40;})")
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'{section}-{width}-{theme}.png'))
    page.locator('.viewer-close').click()
    pw.expect(opener).to_be_focused()
    opener.click()
    page.locator('.viewer-next').click()
    current(page, rows[2])
    page.locator('.viewer .act-delete').click()
    page.keyboard.press('ArrowRight')
    current(page, rows[2])  # confirmation above the viewer owns keyboard input
    page.locator('dialog:not(.viewer) .btn-danger').click()
    pw.expect(page.locator('.viewer')).to_have_count(0)
    assert calls['deleted'] == [rows[2]['id']]
    assert not errors, errors


@pytest.mark.parametrize('media_page', [('photo', 1280), ('video', 320)], indirect=True)
def test_a_single_file_has_no_navigation_and_escape_restores_focus(media_page, live):
    page, section, _ = media_page
    rows, _ = gallery(page, section, count=1)
    opener = open_first(page, live, section)
    if page.locator('.viewer-next').is_enabled():
        page.locator('.viewer-next').click()
    pw.expect(page.locator('.viewer-prev')).to_be_disabled()
    pw.expect(page.locator('.viewer-next')).to_be_disabled()
    page.keyboard.press('ArrowRight')
    page.keyboard.press('ArrowLeft')
    current(page, rows[0])
    page.keyboard.press('Escape')
    pw.expect(page.locator('.viewer')).to_have_count(0)
    pw.expect(opener).to_be_focused()


@pytest.mark.parametrize('media_page', [('photo', 320), ('video', 1280)], indirect=True)
def test_pagination_retries_and_late_page_cannot_reopen_closed_viewer(media_page, live):
    page, section, _ = media_page
    rows, calls = gallery(page, section, count=120)
    open_first(page, live, section)
    page.locator('.viewer-close').click()
    selector = '.photo-card .card-open' if section == 'photo' else '.video-card .card-viewer'
    loaded = page.locator(selector).count()
    assert loaded < len(rows) - 1
    last = page.locator(selector).last
    last.evaluate('e=>e.click()')  # opening does not scroll the feed or trigger its own pagination
    last_id = page.locator('.viewer').get_attribute('data-item')
    last_at = next(i for i, row in enumerate(rows) if row['id'] == last_id)
    calls['fail'] = True
    page.locator('.viewer-next').click()
    pw.expect(page.locator('.viewer-navigation-error')).to_have_text('Проверка связи')
    current(page, rows[last_at])
    calls['fail'] = False
    page.locator('.viewer-next').click()
    current(page, rows[last_at + 1])
    # The newly fetched page is retained for going back and forward.
    n = len(calls['pages'])
    page.locator('.viewer-prev').click()
    page.locator('.viewer-next').click()
    current(page, rows[last_at + 1])
    assert len(calls['pages']) == n
    page.locator('.viewer-close').click()
    # A request started for an old deep link must not reopen a viewer after Escape.
    page.evaluate("""() => {
      const real=window.fetch;
      window.galleryFetch=real;
      window.fetch=async (url, opts) => {
        const held=String(url).includes('before=');
        if(held) await new Promise(resolve=>window.releaseGallery=resolve);
        const response=await real(url,opts);
        if(held) window.galleryReturned=true;
        return response;
      };
    }""")
    page.evaluate('hash=>location.hash=hash', f'#/{section}?id={rows[-1]["id"]}')
    page.wait_for_function("() => typeof window.releaseGallery === 'function'")
    page.keyboard.press('Escape')
    page.evaluate('window.releaseGallery()')
    page.wait_for_function('() => window.galleryReturned')
    pw.expect(page.locator('.viewer')).to_have_count(0)
    page.evaluate('window.fetch=window.galleryFetch')
    page.evaluate('hash=>location.hash=hash', f'#/{section}?id={rows[-1]["id"]}')
    current(page, rows[-1])
    pw.expect(page.locator('.viewer-prev')).to_be_enabled()
    page.locator('.viewer-prev').click()
    current(page, rows[-2])
