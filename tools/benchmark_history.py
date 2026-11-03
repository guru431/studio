"""Изолированный замер истории и Markdown: только синтетические данные, без вызовов моделей.

Запуск и ограничения описаны в CLAUDE.md. Нужны зависимости браузерных тестов и Edge/Chromium.
"""
import argparse
import hashlib
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'tests')]

from browser import find_browser
from fake_bridge import FakeMachine, T0, pid
from live_app import start_live_app
from playwright.sync_api import sync_playwright
from test_ui_browser import code_online


def messages():
    return [{'id': f'm-{i}', 'role': 'user' if i % 2 == 0 else 'assistant', 'time': T0 + i,
             'text': 'Synthetic question' if i % 2 == 0 else
             'Synthetic response.\n\n```python\n' + 'def calculate(x):\n    return x * 2\n' * 20 + '```',
             'steps': [] if i % 2 == 0 else [
                 {'id': str(j), 'tool': 'Bash', 'input': {'command': 'synthetic-read-only-command'},
                  'text': 'x' * 1000, 'error': False} for j in range(20)]} for i in range(100)]


def run():
    executable = find_browser()
    if not executable:
        raise SystemExit('Нужен Edge/Chromium из браузерного тестового стенда.')
    sources = ['app/claude_code.py', 'app/static/css/app.css', 'app/static/js/app.js',
               'app/static/js/code-chat.js', 'app/static/js/markdown.js', 'app/static/js/markdown-worker.js']
    result = {'viewport': [393, 852], 'cpu_rate': 4, 'source_messages': 100, 'source_steps': 1000,
              'sources': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources},
              'history': [], 'markdown': []}
    with tempfile.TemporaryDirectory(prefix='studio-history-bench-') as folder:
        live = start_live_app(Path(folder))
        chats = {str(uuid.UUID(int=i + 1)): {'title': 'Synthetic', 'created': T0, 'modified': T0,
                                           'messages': messages()} for i in range(4)}
        machine = FakeMachine({'project-000': {'chats': chats}})
        aid, bridge = code_online(live, 'owner', machine)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=executable, headless=True, args=['--no-proxy-server'])
                result['browser'] = browser.version
                context = browser.new_context(viewport={'width': 393, 'height': 852})
                context.add_init_script("""window.__longtasks=[];
                    new PerformanceObserver(list => window.__longtasks.push(...list.getEntries()
                      .map(e=>({start:e.startTime,ms:e.duration}))))
                      .observe({type:'longtask',buffered:true});""")
                page = context.new_page()
                page.goto(live.url + '/login')
                page.locator('#login').fill('owner')
                page.locator('#password').fill('password-123')
                page.locator('#submit').click()
                page.wait_for_function("() => document.documentElement.dataset.ready==='1'")
                for index, cid in enumerate(chats):
                    if index == 1:
                        context.new_cdp_session(page).send('Emulation.setCPUThrottlingRate', {'rate': 4})
                    page.evaluate('window.__longtasks=[]')
                    start = time.perf_counter()
                    page.evaluate('(route) => {location.hash=route}', f'#/code/{aid}/{pid("project-000")}/{cid}')
                    page.wait_for_function("""cid => document.querySelector('#code-chat')?.dataset.chat===cid
                        && document.querySelector('#code-chat').dataset.loaded==='1'""", arg=cid)
                    elapsed = (time.perf_counter() - start) * 1000
                    page.wait_for_timeout(150)
                    if index:
                        result['history'].append({'ready_ms': round(elapsed, 1), **page.evaluate("""() => ({
                            messages:document.querySelectorAll('.code-msg').length,
                            steps:document.querySelectorAll('.code-step').length,longtasks:window.__longtasks})""")})
                for chars in (50000, 100000):
                    result['markdown'].append(page.evaluate(r'''async chars => {
                        const {renderMarkdownInto} = await import('/static/js/markdown.js');
                        const text = ('Explanation.\n\n```python\ndef calculate(x):\n    return x * 2\n```\n\n')
                          .repeat(Math.ceil(chars/75));
                        const host=document.createElement('div');host.className='md';document.body.append(host);
                        window.__longtasks=[];const start=performance.now();await renderMarkdownInto(host,text);
                        const total=performance.now()-start;
                        await new Promise(requestAnimationFrame);await new Promise(r=>setTimeout(r,100));
                        const value={characters:text.length,total_ms:total,longtasks:window.__longtasks};
                        host.remove();return value;
                    }''', chars))
                browser.close()
        finally:
            bridge.stop()
            live.stop()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = run()
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for kind in ('history', 'markdown'):
        for row in data[kind]:
            print(kind, {**row, 'longtasks': len(row['longtasks']),
                         'max_task_ms': max((task['ms'] for task in row['longtasks']), default=0)})
