"""Browser network audit with an isolated database, gateway and agent bridge.

Run with the project's .venv Python; --output is required. No production login,
model calls, credentials, personal conversations or running bridges are used.
CDP throttles browser HTTP traffic; it does not emulate packet loss or the
server-to-gateway/agent link. Run serially, as tests/live_app changes process state.
"""
import argparse
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'tests')]

from browser import find_browser
from fake_bridge import FakeMachine, T0, pid
from live_app import start_live_app
from PIL import Image
from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright
from test_ui_browser import code_online

PROFILES = [
    {'name': 'baseline', 'download_kbps': 0, 'upload_kbps': 0, 'latency_ms': 0},
    {'name': 'mobile', 'download_kbps': 1600, 'upload_kbps': 750, 'latency_ms': 150},
    {'name': 'slow_3g', 'download_kbps': 400, 'upload_kbps': 100, 'latency_ms': 400},
    {'name': 'edge', 'download_kbps': 128, 'upload_kbps': 64, 'latency_ms': 1500},
]
A, B = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
READY = "() => document.documentElement.dataset.ready === '1'"
INIT = """window.__audit={};
new MutationObserver(() => {
  if (!window.__audit.ready && document.documentElement.dataset.ready==='1')
    window.__audit.ready=performance.now();
}).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['data-ready']});
"""


def elapsed(start):
    return round((time.perf_counter() - start) * 1000, 1)


def throttle(cdp, profile, *, offline=False):
    cdp.send('Network.emulateNetworkConditions', {
        'offline': offline, 'latency': profile['latency_ms'],
        'downloadThroughput': profile['download_kbps'] * 1000 / 8 or -1,
        'uploadThroughput': profile['upload_kbps'] * 1000 / 8 or -1,
    })


def goto_hash(page, route):
    page.evaluate('route => {location.hash=route}', '#/' + route)


def wait(page, expression, **options):
    page.wait_for_function('() => (' + expression + ')', **options)


def chat_ready(page, cid):
    page.wait_for_function("""cid => document.querySelector('#code-chat')?.dataset.chat===cid
        && document.querySelector('#code-chat').dataset.loaded==='1'""", arg=cid)


def fixture_chat(label):
    return {'title': label, 'engine': 'codex', 'created': T0, 'modified': T0, 'messages': [
        {'id': f'm-{i}', 'role': 'assistant' if i % 2 else 'user', 'time': T0 + i,
         'text': label + ' synthetic history ' + ('word ' * 80), 'steps': []} for i in range(20)]}


def signed_context(browser, live, *, service_workers='block', width=393):
    context = browser.new_context(viewport={'width': width, 'height': 852},
                                  service_workers=service_workers)
    response = context.request.post(live.url + '/login',
                                    form={'login': 'owner', 'password': 'password-123'},
                                    max_redirects=0)
    if response.status != 303:
        context.close()
        raise RuntimeError(f'Isolated fixture login returned {response.status}')
    context.add_init_script(INIT)
    return context


def profile_run(browser, live, aid, profile, attachment, *, loading_only=False):
    context = signed_context(browser, live)
    page = context.new_page()
    page.set_default_timeout(120000)
    cdp = context.new_cdp_session(page)
    cdp.send('Network.enable')
    cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})
    cdp.send('Network.setBypassServiceWorker', {'bypass': True})
    throttle(cdp, profile)
    errors, finished, encodings = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: encodings.append(response.headers.get('content-encoding'))
            if '/static/' in response.url else None)
    cdp.on('Network.loadingFinished', lambda row: finished.append(row['encodedDataLength']))
    row = {'profile': profile, 'viewport': [393, 852], 'switches': [], 'sections': []}
    try:
        for cache in ('cold', 'warm'):
            if cache == 'warm':
                cdp.send('Network.setCacheDisabled', {'cacheDisabled': False})
                # Populate the HTTP cache with exactly the same modules first.
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function(READY)
                page.locator('#code-projects li[data-pid]').first.wait_for(state='attached')
            finished.clear()
            start = time.perf_counter()
            if cache == 'cold':
                page.goto(live.url + '/#/code', wait_until='domcontentloaded', timeout=120000)
            else:
                page.reload(wait_until='domcontentloaded', timeout=120000)
            page.wait_for_function(READY)
            page.locator('#code-projects li[data-pid]').first.wait_for(state='attached')
            row[cache] = {'useful_ms': elapsed(start), 'bytes_finished': sum(finished),
                         **page.evaluate("""() => ({ready_ms:window.__audit.ready,
                         dom_ms:performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd,
                         js_count:performance.getEntriesByType('resource').filter(r=>/\\.js(?:[?]|$)/.test(r.name)).length,
                         static_transfer_bytes:performance.getEntriesByType('resource')
                           .filter(r=>new URL(r.name).pathname.startsWith('/static/'))
                           .reduce((sum,r)=>sum+r.transferSize,0)})""")}
        if loading_only:
            return row
        for project, cid, label, cached in [('alpha', A, 'Alpha', False),
                                           ('beta', B, 'Beta', False), ('alpha', A, 'Alpha', True)]:
            start = time.perf_counter()
            goto_hash(page, f'code/{aid}/{pid(project)}/{cid}')
            page.wait_for_function("""label => document.querySelector('#code-feed .md')
                ?.textContent.startsWith(label)""", arg=label)
            first = elapsed(start)
            chat_ready(page, cid)
            wait(page, "!document.querySelector('#code-send').disabled")
            row['switches'].append({'project': project, 'cached': cached,
                                    'first_text_ms': first, 'send_ready_ms': elapsed(start)})
        sections = [('photo', '#photo-feed'), ('video', '#video-feed'),
                    ('speech', '#speech-composer'), ('transcribe', '#transcribe-add'),
                    ('ocr', '#ocr-add'), ('translate', '#tl-input'),
                    ('settings/code', '#agents')]
        for route, selector in sections:
            start = time.perf_counter()
            goto_hash(page, route)
            page.locator(selector).wait_for(state='attached', timeout=30000)
            row['sections'].append({'route': route, 'mounted_ms': elapsed(start),
                'overflow': page.evaluate('document.documentElement.scrollWidth > innerWidth')})
        goto_hash(page, 'chat')
        page.locator('#chat-input').wait_for()
        page.locator('#model-chip').wait_for()
        page.evaluate("document.querySelector('#chat-new').click()")
        wait(page, "location.hash==='#/chat' && !document.querySelector('#messages .msg')")
        # Submit through the UI, including a genuine multipart attachment upload.
        page.locator('#chat-input').fill('Synthetic slow network check ' + profile['name'])
        file_input = page.locator('#attach')
        file_input.set_input_files({'name': 'synthetic.png', 'mimeType': 'image/png', 'buffer': attachment})
        page.locator('#composer .att-chip').wait_for()
        before = len(live.gw.chat_requests)
        start = time.perf_counter()
        page.locator('#send').click()
        wait(page, "document.querySelector('#messages .msg.assistant .md')?.textContent.includes('Здравствуйте')")
        first = elapsed(start)
        wait(page, "document.querySelector('#stop').hidden && !!document.querySelector('#messages .msg.assistant .msg-actions button')")
        row['chat'] = {'attachment_bytes': len(attachment), 'first_text_ms': first,
                       'done_ms': elapsed(start), 'model_calls': len(live.gw.chat_requests) - before,
                       'messages': page.locator('#messages .msg').count()}
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function(READY)
        wait(page, "document.querySelectorAll('#messages .msg').length===2")
        row['chat']['survives_reload'] = 'Здравствуйте' in page.locator('#messages').inner_text()
    except Exception as error:
        row['error'] = str(error)[:1000]
        row['where'] = page.evaluate("({hash:location.hash, view:document.querySelector('#view')?.textContent.slice(0,300)})")
    finally:
        row['page_errors'] = errors
        row['gzip_static_responses'] = encodings.count('gzip')
        context.close()
    return row


def outage_run(browser, live, aid, machine):
    context = signed_context(browser, live, width=320)
    page = context.new_page()
    page.set_default_timeout(30000)
    cdp = context.new_cdp_session(page)
    cdp.send('Network.enable')
    baseline = PROFILES[0]
    result = {}
    try:
        page.goto(live.url + f'/#/code/{aid}/{pid("alpha")}/{A}')
        page.wait_for_function(READY)
        chat_ready(page, A)
        wait(page, "!document.querySelector('#code-send').disabled")
        page.locator('#code-text').fill('Synthetic draft during outage')
        count = machine.turns
        throttle(cdp, baseline, offline=True)
        page.locator('#code-send').click()
        wait(page, "document.querySelector('#code-status')?.textContent.includes('Нет связи')")
        result['offline_send'] = {
            'draft_preserved': page.locator('#code-text').input_value() == 'Synthetic draft during outage',
            'turns': machine.turns - count,
            'status': page.locator('#code-status').inner_text(),
        }
        throttle(cdp, PROFILES[2])
        machine.script = [{'k': 'text', 'text': 'Recovered synthetic answer'}, {'k': 'done', 'status': 'done'}]
        start = time.perf_counter()
        page.locator('#code-send').click()
        wait(page, "document.querySelector('#code-feed').textContent.includes('Recovered synthetic answer')")
        result['recovery'] = {'ms': elapsed(start), 'turns': machine.turns - count,
                              'draft_cleared': page.locator('#code-text').input_value() == ''}
        # A live server-side answer continues while the phone temporarily has no network.
        goto_hash(page, 'chat')
        page.locator('#chat-input').wait_for()
        page.evaluate("document.querySelector('#chat-new').click()")
        wait(page, "location.hash==='#/chat' && !document.querySelector('#messages .msg')")
        live.gw.chat_reply = ['Before outage ', 'after recovery']
        live.gw.chat_hold = True
        page.evaluate("""() => {
            const real=window.fetch;
            window.fetch=async (...args) => {
                const response=await real(...args);
                if (!String(args[0]).endsWith('/turns')) return response;
                const reader=response.body.getReader();
                const body=new ReadableStream({
                    start(controller) {
                        window.__cutStream=()=>{controller.error(new TypeError('Synthetic stream reset'));reader.cancel();};
                        (async()=>{try {while(true) {const {done,value}=await reader.read();
                            if(done){controller.close();break;}controller.enqueue(value);}}
                            catch(error){try{controller.error(error);}catch{}}})();
                    },
                    cancel(){reader.cancel();}
                });
                return new Response(body,{status:response.status,headers:response.headers});
            };
        }""")
        page.locator('#chat-input').fill('Synthetic stream interruption')
        before = len(live.gw.chat_requests)
        page.locator('#send').click()
        wait(page, "document.querySelector('#messages .msg.assistant .md')?.textContent.includes('Before outage')")
        throttle(cdp, baseline, offline=True)
        page.evaluate('window.__cutStream()')
        page.wait_for_timeout(1200)
        live.gw.chat_hold = False
        page.wait_for_timeout(1200)
        throttle(cdp, PROFILES[2])
        start = time.perf_counter()
        try:
            wait(page, "document.querySelector('#messages .msg.assistant .md')?.textContent.includes('after recovery')", timeout=25000)
            result['stream_recovery'] = {'automatic': True, 'ms': elapsed(start)}
        except BrowserTimeout:
            result['stream_recovery'] = {'automatic': False, 'waited_ms': elapsed(start),
                'notice': page.locator('#chat-error').inner_text(),
                'text': page.locator('#messages .msg.assistant .md').inner_text()}
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function(READY)
            wait(page, "document.querySelector('#messages .msg.assistant .md')?.textContent.includes('after recovery')")
            result['stream_recovery']['reload_recovers'] = True
        result['stream_recovery']['model_calls'] = len(live.gw.chat_requests) - before
    except Exception as error:
        result['error'] = str(error)[:800]
        result['where'] = page.evaluate("({hash:location.hash,status:document.querySelector('#code-status')?.textContent,chat:document.querySelector('#messages')?.textContent.slice(-300)})")
    finally:
        live.gw.chat_hold = False
        context.close()
    return result


def offline_shell_run(browser, live):
    context = signed_context(browser, live, service_workers='allow', width=320)
    page = context.new_page()
    page.set_default_timeout(30000)
    try:
        page.goto(live.url + '/')
        page.wait_for_function(READY)
        page.evaluate('navigator.serviceWorker.ready')
        wait(page, '!!navigator.serviceWorker.controller')
        context.set_offline(True)
        page.reload(wait_until='domcontentloaded')
        offline = {'title': page.title(), 'message': 'Нет связи' in page.locator('body').inner_text(),
                   'overflow': page.evaluate('document.documentElement.scrollWidth>innerWidth')}
        context.set_offline(False)
        page.get_by_role('link', name='Повторить').click()
        page.wait_for_function(READY)
        offline['recovers'] = page.locator('#rail').is_visible()
        return offline
    finally:
        context.close()


def public_login_run(browser, url):
    """Read-only measurements; no credentials, cookies or authenticated API calls."""
    rows = []
    for profile in PROFILES:
        context = browser.new_context(viewport={'width': 393, 'height': 852}, service_workers='block')
        try:
            page = context.new_page()
            cdp = context.new_cdp_session(page)
            cdp.send('Network.enable')
            cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})
            throttle(cdp, profile)
            start = time.perf_counter()
            response = page.goto(url.rstrip('/') + '/login', wait_until='load', timeout=60000)
            page.locator('#submit').wait_for()
            rows.append({'profile': profile['name'], 'ms': elapsed(start), 'status': response.status,
                         **page.evaluate("""() => ({dom_ms:performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd,
                         transfer_bytes:performance.getEntriesByType('resource').reduce((s,r)=>s+r.transferSize,
                           performance.getEntriesByType('navigation')[0].transferSize),
                         overflow:document.documentElement.scrollWidth>innerWidth})""")})
        finally:
            context.close()
    return rows


def response_loss_run(browser, live):
    """Discard a POST response after the real app has accepted the operation."""
    context = signed_context(browser, live)
    page = context.new_page()
    page.set_default_timeout(30000)
    result = {}
    try:
        page.goto(live.url + '/#/chat')
        page.wait_for_function(READY)
        page.locator('#chat-input').wait_for()
        page.evaluate("document.querySelector('#chat-new').click()")
        wait(page, "location.hash==='#/chat' && !document.querySelector('#messages .msg')")
        page.evaluate("""() => {
            const real=window.fetch; let lost=false;
            window.fetch=async (...args) => {
                const response=await real(...args);
                if (!lost && String(args[0]).endsWith('/turns')) {
                    lost=true;
                    await response.arrayBuffer();
                    throw new TypeError('Synthetic connection reset after server accepted POST');
                }
                return response;
            };
        }""")
        live.gw.chat_reply = ['Synthetic accepted response']
        before = len(live.gw.chat_requests)
        page.locator('#chat-input').fill('Synthetic response loss check')
        page.locator('#send').click()
        wait(page, "document.querySelector('#chat-error').textContent.includes('Нет связи')")
        result['after_loss'] = {'model_calls': len(live.gw.chat_requests) - before,
                               'visible_messages': page.locator('#messages .msg').count(),
                               'draft': page.locator('#chat-input').input_value(),
                               'notice': page.locator('#chat-error').inner_text()}
        cid = page.url.split('#/chat/')[1]
        with closing(sqlite3.connect(live.db_path)) as conn:
            result['after_loss']['db_messages'] = conn.execute(
                'SELECT count(*) FROM messages WHERE chat_id=?', (cid,)).fetchone()[0]
        page.locator('#send').click()
        wait(page, "document.querySelector('#stop').hidden && !!document.querySelector('#messages .msg.assistant .msg-actions button')")
        result['after_retry'] = {'model_calls': len(live.gw.chat_requests) - before,
                                'visible_messages': page.locator('#messages .msg').count()}
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function(READY)
        wait(page, "document.querySelectorAll('#messages .msg').length>=2")
        result['after_reload'] = {'visible_messages': page.locator('#messages .msg').count(),
                                 'user_messages': page.locator('#messages .msg.user').count()}
    except Exception as error:
        result['error'] = str(error)[:800]
    finally:
        context.close()
    return result


def run(args):
    executable = find_browser()
    if not executable:
        raise SystemExit('Edge/Chromium from the browser test stand is required.')
    output = {'head': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True, timeout=10).strip(),
              'started_at': datetime.now(timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'scope': 'Local real HTTP app; synthetic gateway and WebSocket agent; mobile viewport',
              'workspace': 'empty before each profile; restore is tested separately',
              'profiles': []}
    with tempfile.TemporaryDirectory(prefix='studio-network-audit-') as folder, ExitStack() as cleanup:
        live = start_live_app(Path(folder))
        cleanup.callback(live.stop)
        machine = FakeMachine({'alpha': {'chats': {A: fixture_chat('Alpha')}},
                               'beta': {'chats': {B: fixture_chat('Beta')}}})
        from test_codex import CATALOG
        machine.codex = CATALOG
        machine.project_summary = True
        aid, bridge = code_online(live, 'owner', machine)
        cleanup.callback(bridge.stop)
        image = Image.frombytes('RGB', (128, 128), random.Random(42).randbytes(128 * 128 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        with closing(image), closing(buffer):
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=executable, headless=True,
                                              args=['--no-proxy-server'])
                try:
                    output['browser'] = browser.version
                    if args.public_url:
                        output['production_login'] = public_login_run(browser, args.public_url)
                        print('PUBLIC ' + json.dumps(output['production_login']), flush=True)
                    for profile in PROFILES:
                        if args.faults_only or (args.profiles and profile['name'] not in args.profiles):
                            continue
                        # A previous profile must not silently restore its chat and download
                        # Markdown in the next profile's initial project-list measurement.
                        with closing(sqlite3.connect(live.db_path)) as conn:
                            conn.execute('DELETE FROM code_tabs')
                            conn.execute('DELETE FROM code_place')
                            conn.commit()
                        print('PROFILE ' + profile['name'], flush=True)
                        row = profile_run(browser, live, aid, profile, buffer.getvalue(),
                                          loading_only=args.loading_only)
                        output['profiles'].append(row)
                        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                        print(json.dumps(row), flush=True)
                    if not args.profiles and not args.loading_only:
                        output['response_loss'] = response_loss_run(browser, live)
                        print('LOSS ' + json.dumps(output['response_loss']), flush=True)
                        output['outage'] = outage_run(browser, live, aid, machine)
                        print('OUTAGE ' + json.dumps(output['outage']), flush=True)
                        output['offline_shell'] = offline_shell_run(browser, live)
                        print('OFFLINE ' + json.dumps(output['offline_shell']), flush=True)
                finally:
                    browser.close()
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return int(any(row.get('error') for row in output['profiles'])
               or any(isinstance(value, dict) and value.get('error') for value in output.values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--profiles', nargs='+', choices=[p['name'] for p in PROFILES])
    parser.add_argument('--public-url', help='Optional read-only /login checks, without authentication')
    parser.add_argument('--faults-only', action='store_true')
    parser.add_argument('--loading-only', action='store_true')
    args = parser.parse_args()
    if args.faults_only and (args.profiles or args.loading_only):
        parser.error('--faults-only cannot be combined with --profiles/--loading-only')
    raise SystemExit(run(args))
