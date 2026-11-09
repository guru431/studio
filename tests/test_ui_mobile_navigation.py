"""Контраст, увеличенный текст и сохранение оформления в настоящем браузере."""
import pytest

from test_ui_browser import READY, browser, go, live, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def large_font(back='chat'):
    """Размер шрифта 150% — в «Настройки → Оформление» (настройка этого устройства), затем раздел `back`."""
    return [*go('settings/appearance'), ('wait', "document.querySelector('#appearance-font')"),
            ('eval', 'font', "document.querySelector('#appearance-font').value = '150';"
                             "document.querySelector('#appearance-font').dispatchEvent(new Event('input'))"),
            *go(back), ('wait', f"document.querySelector('#rail [data-section={back}][aria-current=page]')")]


NAV_GEOMETRY = """(() => {
  const tabs = [...document.querySelectorAll('#rail .tab, #mobile-more')].filter(e => e.getBoundingClientRect().width);
  const labels = tabs.map(e => {
    const node = e.querySelector(':scope > span:last-child');
    const range = document.createRange(); range.selectNodeContents(node);
    const r = range.getBoundingClientRect(), b = e.getBoundingClientRect();
    return {label: node.textContent, left:r.left, right:r.right, top:r.top, bottom:r.bottom,
      inside:r.left >= b.left - 1 && r.right <= b.right + 1 && r.bottom <= b.bottom + 1,
      hit:b.width >= 44 && b.height >= 44};
  });
  return {labels, overlaps: labels.some((a,i) => labels.some((b,j) => i < j
    && a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top))};
})()"""


def test_mobile_labels_do_not_overlap_at_large_font(browser, live):
    r = browser.drive(live.url + '/', [*signin(), ('wait', READY), *large_font(),
        ('idle',), ('eval', 'geometry', NAV_GEOMETRY)], width=320, mobile=True)
    assert not r.get('failed'), r
    geometry = r['values']['geometry']
    assert not geometry['overlaps'], geometry
    assert all(label['inside'] and label['hit'] for label in geometry['labels']), geometry


CONTRAST = """(() => {
  const s = getComputedStyle(document.documentElement);
  function rgb(token) {
    const node = document.createElement('span'); node.style.color = s.getPropertyValue(token);
    document.body.append(node);
    const color = getComputedStyle(node).color.match(/[\\d.]+/g).slice(0,3).map(Number); node.remove();
    return color.map(c => c / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
      .reduce((sum,c,i) => sum + c * [.2126,.7152,.0722][i], 0);
  }
  return [['--accent-text','--accent'], ...['--bg','--side','--rail','--panel','--panel-2','--bubble']
    .flatMap(bg => ['--text','--muted','--accent','--danger','--ok','--git-modified'].map(fg => [fg,bg]))]
    .map(([fg,bg]) => { const a = rgb(fg), b = rgb(bg); return {fg,bg,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)}; });
})()"""


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_palette_text_has_minimum_contrast(browser, live, theme):
    r = browser.drive(live.url + '/', [*signin(), ('wait', READY),
        ('eval', 'theme', f"document.documentElement.dataset.theme = '{theme}'"),
        ('eval', 'contrast', CONTRAST)])
    assert not r.get('failed'), r
    assert all(pair['ratio'] >= 4.5 for pair in r['values']['contrast']), r['values']['contrast']


@pytest.mark.parametrize('failure', ['server', 'network'])
def test_failed_theme_save_survives_profile_refresh(browser, live, failure):
    login = f'theme-save-failure-{failure}'
    live.add_user(login, theme='light')
    refusal = ("Promise.resolve(new Response('{}', {status:500, headers:{'Content-Type':'application/json'}}))"
               if failure == 'server' else "Promise.reject(new TypeError('Synthetic network failure'))")
    r = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('eval', 'inject', "window.realFetch = window.fetch; window.fetch = (url, opts) =>"
            " url === '/api/me' && opts?.method === 'PATCH'"
            f" ? {refusal} : window.realFetch(url, opts)"),
        ('click', '#avatar'), ('click', '#menu-theme label:has(> #menu-theme-dark)'), ('idle',),
        ('eval', 'changed', "document.documentElement.dataset.theme"),
        ('eval', 'status', "document.querySelector('#theme-save-status')?.textContent || ''"),
        ('eval', 'refresh', "document.dispatchEvent(new Event('visibilitychange'))"), ('idle',),
        ('eval', 'afterRefresh', "document.documentElement.dataset.theme"),
        ('eval', 'restore', 'window.fetch = window.realFetch'), ('click', '#theme-save-retry'),
        ('wait', "document.querySelector('#theme-save-status').textContent.includes('сохранено')"),
        ('eval', 'saved', "fetch('/api/me').then(r => r.json()).then(m => m.theme)"),
        ('click', '#menu-theme label:has(> #menu-theme-auto)'),
        ('wait', "!document.documentElement.hasAttribute('data-theme')"
                 " && document.querySelector('#theme-save-status').textContent.includes('сохранено')"),
        ('eval', 'savedAuto', "fetch('/api/me').then(r => r.json()).then(m => m.theme)"),
        ('eval', 'reload', 'location.reload()'), ('wait', READY),
        ('eval', 'restoredAuto', "document.querySelector('#menu-theme-auto').checked"
                               " && localStorage.getItem('studio-theme') === null")], width=320, mobile=True)
    assert not r.get('failed'), r
    assert r['values']['changed'] == r['values']['afterRefresh'] == 'dark', r['values']
    assert 'Не удалось' in r['values']['status'], r['values']
    assert r['values']['saved'] == 'dark' and r['values']['savedAuto'] == 'auto'
    assert r['values']['restoredAuto']


@pytest.mark.parametrize('width', [320, 393, 1280])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_mobile_navigation_preferences_and_geometry(browser, live, width, theme, tmp_path):
    login = f'nav-{width}-{theme}'
    uid = live.add_user(login, theme=theme)
    steps = [*signin(login), ('wait', READY), *large_font()]
    if width < 760:
        steps += [('click', '#mobile-more'),
            ('eval', 'remaining', "[...document.querySelectorAll('#mobile-section-links a')].map(e => e.dataset.section)"),
            ('click', '#mobile-section-links a[data-section=transcribe]'),
            ('wait', "document.querySelector('#mobile-more').getAttribute('aria-current') === 'page'"),
            ('eval', 'activeLabel', "document.querySelector('#mobile-more-label').textContent"),
            ('idle',), ('eval', 'activeGeometry', NAV_GEOMETRY),
            ('click', '#mobile-more'), ('click', '#mobile-nav-edit')]
        for section in ['chat', 'photo', 'code', 'speech', 'transcribe', 'ocr', 'translate']:
            steps.append(('click', f'#mobile-nav-editor input[value={section}]'))
        steps += [('click', '#mobile-nav-save'), ('click', '#mobile-sections-close'), ('idle',),
            ('eval', 'geometry', NAV_GEOMETRY),
            ('eval', 'stored', f"JSON.parse(localStorage.getItem('studio-mobile-nav:{uid}'))"),
            ('screenshot', str(tmp_path / f'navigation-{width}-{theme}.png')),
            ('eval', 'reload', 'location.reload()'), ('wait', READY),
            ('eval', 'kept', "[...document.querySelectorAll('#rail a.tab:not(.mobile-secondary)')].map(e => e.dataset.section)"),
            ('click', '#mobile-more'), ('click', '#mobile-nav-edit'), ('click', '#mobile-nav-reset'),
            ('click', '#mobile-sections-close'),
            ('eval', 'reset', "[...document.querySelectorAll('#rail a.tab:not(.mobile-secondary)')].map(e => e.dataset.section)")]
    else:
        steps += [('eval', 'desktop', "[...document.querySelectorAll('#rail a.tab')].filter(e => e.getBoundingClientRect().width).length"),
            ('eval', 'moreHidden', "document.querySelector('#mobile-more').getBoundingClientRect().width === 0"),
            ('screenshot', str(tmp_path / f'navigation-{width}-{theme}.png'))]
    steps += [('eval', 'services', "['#search-open','#jobs','#avatar'].every(s => document.querySelector(s).getBoundingClientRect().width > 0)"),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth')]
    r = browser.drive(live.url + '/', steps, width=width, mobile=width < 760)
    assert not r.get('failed'), r
    values = r['values']
    assert values['services'] and not values['overflow'], values
    if width < 760:
        assert values['remaining'] == ['video', 'speech', 'transcribe', 'ocr', 'translate']
        assert values['activeLabel'] == 'Ещё: Разговоры'
        assert not values['activeGeometry']['overlaps'] and all(
            label['inside'] and label['hit'] for label in values['activeGeometry']['labels']), values['activeGeometry']
        assert values['stored'] == values['kept'] == ['speech', 'transcribe', 'ocr', 'translate']
        assert values['reset'] == ['chat', 'photo', 'code']
        geometry = values['geometry']
        assert not geometry['overlaps'] and all(label['inside'] and label['hit'] for label in geometry['labels']), geometry
    else:
        assert values['desktop'] == 8 and values['moreHidden']


@pytest.mark.parametrize('width', [320, 393])
@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_mobile_navigation_switches_between_all_icons_and_more(browser, live, width, theme, tmp_path):
    login = f'all-tabs-{width}-{theme}'
    uid = live.add_user(login, theme=theme)
    visible = "[...document.querySelectorAll('#rail a.tab')].filter(t => t.getBoundingClientRect().width).map(t => t.dataset.section)"
    r = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('click', '#mobile-more'), ('click', '#mobile-nav-edit'),
        *[('click', f'#mobile-nav-editor input[value={section}]')
          for section in ('chat', 'photo', 'speech', 'transcribe', 'ocr')],
        ('click', '#mobile-nav-save'), ('click', '#mobile-nav-edit'),
        ('click', '#mobile-nav-mode-all'), ('click', '#mobile-nav-save'), ('idle',),
        ('eval', 'allTabs', visible),
        ('eval', 'mode', f"localStorage.getItem('studio-mobile-nav-mode:{uid}')"),
        ('eval', 'moreHidden', "document.querySelector('#mobile-more').getBoundingClientRect().width === 0"),
        *large_font(), ('idle',), ('eval', 'geometry', NAV_GEOMETRY),
        ('eval', 'buttonRow', "[...document.querySelectorAll('#rail a.tab')].map(t => {const b=t.getBoundingClientRect(); return {top:b.top, width:b.width, height:b.height, iconTop:t.querySelector('svg').getBoundingClientRect().top};})"),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth'),
        ('screenshot', str(tmp_path / f'all-tabs-{width}-{theme}.png')),
        ('click', '#rail a[data-section=translate]'),
        ('wait', "document.querySelector('#rail a[data-section=translate]').getAttribute('aria-current') === 'page'"),
        ('eval', 'reload', 'location.reload()'),
        ('wait', f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ('eval', 'kept', visible),
        *go('settings/appearance'), ('click', '#appearance-mobile-nav-open'),
        ('click', '#mobile-nav-mode-compact'), ('click', '#mobile-nav-save'),
        ('click', '#mobile-sections-close'), ('wait', "!document.querySelector('#mobile-sections').open"),
        ('eval', 'focusBack', "document.activeElement.id"), *go('translate'),
        ('wait', "document.querySelector('#mobile-more').getAttribute('aria-current') === 'page'"),
        ('eval', 'compact', visible),
        ('eval', 'moreActive', "document.querySelector('#mobile-more-label').textContent"),
        ('eval', 'compactGeometry', NAV_GEOMETRY),
        ('eval', 'savedCompact', f"localStorage.getItem('studio-mobile-nav-mode:{uid}')")], width=width, mobile=True)
    assert not r.get('failed'), (r.get('failed'), list(r.get('values', {})), r.get('log'))
    values = r['values']
    assert values['allTabs'] == values['kept'] == ['chat', 'photo', 'video', 'speech', 'transcribe', 'ocr', 'translate', 'code']
    assert values['mode'] == 'all' and values['moreHidden'] and not values['overflow'], values
    geometry = values['geometry']
    assert not geometry['overlaps'] and all(label['inside'] for label in geometry['labels']), geometry
    assert len({round(button['top']) for button in values['buttonRow']}) == 1
    assert len({round(button['iconTop']) for button in values['buttonRow']}) == 1
    assert all(button['width'] >= width / 8 - 1 and button['height'] >= 44 for button in values['buttonRow'])
    assert values['compact'] == ['speech', 'transcribe', 'ocr', 'code'] and values['savedCompact'] == 'compact'
    assert values['focusBack'] == 'appearance-mobile-nav-open'
    assert values['moreActive'] == 'Ещё: Перевод'
    assert all(label['hit'] for label in values['compactGeometry']['labels'])


@pytest.mark.parametrize('width', [320, 1280])
def test_theme_menu_targets_remain_usable_at_small_scale(browser, live, width):
    r = browser.drive(live.url + '/', [*signin(), ('wait', READY), *go('settings/appearance'),
        ('wait', "document.querySelector('#appearance-scale')"),
        ('eval', 'scale', "const scale = document.querySelector('#appearance-scale');"
                          "scale.value = '50'; scale.dispatchEvent(new Event('change'))"), ('idle',),
        ('eval', 'device', "[...document.querySelectorAll('#appearance-controls .btn-link, .segment-option, .accent-choice')]"
            ".filter(e => e.getBoundingClientRect().width).map(e => ({label:e.textContent.trim(),"
            "height:e.getBoundingClientRect().height}))"),
        ('click', '#avatar'), ('wait', "!document.querySelector('#menu').hidden"),
        ('eval', 'targets', "[...document.querySelectorAll('#menu .menu-item, #menu .segment-option')]"
            ".filter(e => e.getBoundingClientRect().width).map(e => ({label:e.textContent.trim(),"
            "height:e.getBoundingClientRect().height}))"),
        ('eval', 'inside', "(() => { const b = document.querySelector('#menu').getBoundingClientRect();"
            "return b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight; })()")],
        width=width, mobile=width < 760)
    assert not r.get('failed'), r
    assert r['values']['inside'] and all(target['height'] >= 44 for target in r['values']['targets']), r['values']
    assert len(r['values']['targets']) >= 5 and all(target['height'] >= 44 for target in r['values']['device']), r['values']


def test_profile_read_started_during_theme_save_cannot_roll_back_success(browser, live):
    """Старый GET отвечает после успешного PATCH; сохранённый выбор уже не pending."""
    live.add_user('theme-save-refresh-race', theme='light')
    inject = """(async () => {
      const stale = await fetch('/api/me').then(r => r.json());
      window.originalFetch = window.fetch;
      window.fetch = (url, opts) => {
        if (url === '/api/me' && opts?.method === 'PATCH') return new Promise(resolve => {
          window.releaseThemePatch = () => window.originalFetch(url, opts).then(resolve);
        });
        if (url === '/api/me' && opts?.method === 'GET') return new Promise(resolve => {
          window.releaseThemeGet = () => resolve(new Response(JSON.stringify(stale),
            {headers:{'Content-Type':'application/json'}}));
        });
        return window.originalFetch(url, opts);
      };
      return true;
    })()"""
    r = browser.drive(live.url + '/', [*signin('theme-save-refresh-race'), ('wait', READY),
        ('eval', 'inject', inject), ('click', '#avatar'), ('click', '#menu-theme label:has(> #menu-theme-dark)'),
        ('eval', 'refresh', "document.dispatchEvent(new Event('visibilitychange'))"),
        ('wait', '!!window.releaseThemeGet'), ('eval', 'save', 'window.releaseThemePatch()'),
        ('wait', "document.querySelector('#theme-save-status').textContent.includes('сохранено')"),
        ('eval', 'lateRead', 'window.releaseThemeGet()'), ('idle',),
        ('eval', 'theme', 'document.documentElement.dataset.theme'),
        ('eval', 'serverTheme', "window.originalFetch('/api/me').then(r => r.json()).then(m => m.theme)")])
    assert not r.get('failed'), r
    assert r['values']['theme'] == r['values']['serverTheme'] == 'dark', r['values']
