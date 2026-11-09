"""Возврат во вкладку: немедленный показ до истечения защиты, сохранение показанной истории."""
import pytest

from fake_bridge import T0, pid
from test_ui_browser import (CODE_CHAT_A, CURTAIN_OPEN, LOCK_OPEN, READY, browser, code_free_machine,
                             code_online, code_session, code_tab_ready, go, hide, live, problems, show, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

ENABLE_LOCK = """(async () => {
  const {api, loadMe} = await import('/static/js/api.js');
  await api('POST', '/api/me/screen-lock', {enabled: true, timeout: 600});
  await loadMe(); document.dispatchEvent(new Event('studio-me')); return true;
})()"""

DELAY_MARK = """(() => {
  const real = window.fetch;
  const pending = new Promise(resolve => { window.resumeMark = resolve; });
  window.fetch = async (url, options) => {
    if (String(url) === '/api/lock/alive') {
      window.markPending = true;
      await pending;
      if (window.markOffline) { window.markFinished = true; throw new TypeError('Offline'); }
    }
    const response = await real(url, options);
    if (String(url) === '/api/lock/alive') window.markFinished = true;
    return response;
  };
  return true;
})()"""


@pytest.mark.parametrize('width', [320, 1280])
@pytest.mark.parametrize('result', ['open', 'locked', 'offline'])
def test_return_reveals_unexpired_screen_before_mark_finishes(browser, live, width, result):
    login = f'resume-{width}-{result}'
    live.add_user(login)
    lock = ("import('/static/js/api.js').then(m => m.api('POST', '/api/lock/now'))"
            if result == 'locked' else 'true')
    response = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('eval', 'enabled', ENABLE_LOCK), ('eval', 'delay', DELAY_MARK), hide(), ('wait', CURTAIN_OPEN),
        show(), ('wait', 'window.markPending'),
        ('eval', 'revealed', f'!{CURTAIN_OPEN} && !document.documentElement.classList.contains("shrouded")'),
        ('eval', 'serverLock', lock),
        ('eval', 'release', f'window.markOffline = {str(result == "offline").lower()}; window.resumeMark(); true'),
        ('wait', 'window.markFinished'),
        ('wait', LOCK_OPEN if result == 'locked' else f'!{CURTAIN_OPEN} && !{LOCK_OPEN}')],
        width=width, mobile=width < 760)
    assert not response.get('failed'), response
    assert response['values']['revealed'] is True
    # `locked`: тест запирает сервер посреди работы страницы — фоновые опросы (/api/me, /api/jobs,
    # /api/code/activity) штатно получают 423 и ждут отпирания (`api.js` `hooks.locked`), это не сбой.
    faults = [line for line in problems(response)
              if not (result == 'locked' and 'status of 423' in str(line))]
    assert not faults, faults


@pytest.mark.parametrize('width', [320, 1280])
def test_return_keeps_unchanged_history_and_refreshes_changed_text(browser, live, width):
    login = f'resume-history-{width}'
    live.add_user(login)
    messages = [{'id': f'm{i}', 'role': 'assistant', 'text': f'Answer {i}\n\n' + 'Details\n\n' * 4,
                 'steps': [{'id': f's{i}', 'tool': 'Read', 'input': {'file_path': 'example.txt'}, 'text': 'Result'}],
                 'time': T0 + i} for i in range(20)]
    machine = code_free_machine({CODE_CHAT_A: code_session('Resume history', messages=messages)})
    aid, channel = code_online(live, login, machine)
    watch = """(() => {
      const real = window.fetch;
      window.historyReads = 0;
      window.fetch = async (url, options) => {
        const path = new URL(String(url), location.href);
        const response = await real(url, options);
        if (!path.pathname.endsWith('/chats/CHAT') || path.search || options?.method !== 'GET') return response;
        const page = await response.json();
        if (window.changeHistory) page.items.at(-1).text = 'Updated while hidden';
        window.historyReads++;
        return new Response(JSON.stringify(page), {status: 200, headers: {'content-type': 'application/json'}});
      };
      const feed = document.querySelector('#code-feed');
      window.resumeNodes = [...feed.children];
      window.resumeStep = feed.querySelector('.code-step'); window.resumeStep.open = true;
      const input = document.querySelector('#code-text'); input.value = 'Unsent draft';
      input.dispatchEvent(new Event('input', {bubbles: true}));
      const range = document.createRange(); range.selectNodeContents(feed.querySelector('.md'));
      getSelection().removeAllRanges(); getSelection().addRange(range);
      window.resumeSelection = getSelection().toString();
      document.querySelector('#view').scrollTop = 0;
      return true;
    })()""".replace('CHAT', CODE_CHAT_A)
    unchanged = """(() => {
      const feed = document.querySelector('#code-feed');
      return feed.children.length === window.resumeNodes.length
        && window.resumeNodes.every((node, i) => feed.children[i] === node)
        && window.resumeStep.open && getSelection().toString() === window.resumeSelection
        && document.querySelector('#view').scrollTop < 2
        && document.querySelector('#code-more').hidden
        && document.querySelector('#code-text').value === 'Unsent draft';
    })()"""
    try:
        response = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
            *go(f'code/{aid}/{pid("alpha")}/{CODE_CHAT_A}'), code_tab_ready(CODE_CHAT_A),
            ('click', '#code-more'), ('wait', 'document.querySelector("#code-more").hidden'),
            ('eval', 'watch', watch), hide(), show(), ('wait', 'window.historyReads === 1'),
            ('eval', 'unchanged', unchanged),
            ('eval', 'change', 'window.changeHistory = true; true'), hide(), show(),
            ('wait', "document.querySelector('#code-feed').textContent.includes('Updated while hidden')"),
            ('eval', 'refreshed', '!window.resumeNodes.at(-1).isConnected')], width=width, mobile=width < 760)
    finally:
        channel.stop()
    assert not response.get('failed'), response
    assert response['values']['unchanged'] and response['values']['refreshed']
    assert not problems(response), problems(response)
