"""Whole-chat exports preserve owner/PIN access through asynchronous PDF rendering."""
from html.parser import HTMLParser
import io
from pathlib import Path
import subprocess

from pypdf import PdfReader
import pytest
from fastapi import HTTPException

import chat_exports
from chat_export_selection import selection_html
import state
from conftest import make_user, signin
from test_chat_shares import regular
from test_code import A, ALPHA, setup


def test_markdown_is_a_utf8_download_without_creating_a_share(studio):
    _, uid = regular(studio)
    state.DB.execute("UPDATE chats SET title='Разговор о коде' WHERE id='chat'")
    state.DB.execute("UPDATE messages SET content='## Заголовок\n\n```python\nprint(1)\n```' WHERE id='chat-a'")
    response = studio.c.post('/api/chats/chat/export', json={'format': 'md'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/markdown')
    assert 'filename*=UTF-8' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store'
    assert '# Разговор о коде\n\n## Пользователь\n\nPrivate question' in response.text
    assert '```python\nprint(1)\n```' in response.text
    assert state.DB.execute('SELECT count(*) FROM chat_shares').fetchone()[0] == 0
    make_user(studio, 'friend')
    signin(studio, 'friend')
    assert studio.c.post('/api/chats/chat/export', json={'format': 'md'}).status_code == 404


def test_exports_reject_unknown_formats_and_locked_sections(studio):
    _, uid = regular(studio)
    assert studio.c.post('/api/chats/chat/export', json={'format': '../../secret'}).status_code == 422
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"chat\"]' WHERE id=?", (uid,))
    state.DB.execute('UPDATE sessions SET seen=0')
    assert studio.c.post('/api/chats/chat/export', json={'format': 'md'}).status_code == 423


@pytest.mark.parametrize('selection', [None, '<p>Выделено</p>'])
@pytest.mark.parametrize('action', ['delete_chat', 'logout', 'lock', 'disable'])
def test_pdf_does_not_escape_a_closed_session_or_deleted_source(studio, monkeypatch, action, selection):
    _, uid = regular(studio)

    async def invalidate():
        if action == 'delete_chat':
            state.DB.execute("DELETE FROM chats WHERE id='chat'")
        elif action == 'logout':
            state.DB.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        elif action == 'lock':
            state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"chat\"]' WHERE id=?", (uid,))
            state.DB.execute('UPDATE sessions SET seen=0')
        else:
            state.DB.execute('UPDATE users SET disabled=1 WHERE id=?', (uid,))

    def render(_, **kwargs):
        studio.c.portal.call(invalidate)
        return b'%PDF-private document'

    monkeypatch.setattr(chat_exports, 'render_pdf', render)
    response = studio.c.post('/api/chats/chat/export', json={'format': 'pdf', 'selection': selection})
    assert response.status_code in (404, 409)
    assert b'private document' not in response.content
    assert not chat_exports.RUNNING


def test_selected_pdf_uses_only_the_fragment_and_checks_ownership(studio, monkeypatch):
    regular(studio)
    fragment = '<h2>Выбрано</h2><p><strong>Жирный</strong> и <em>курсив</em></p>'
    calls = []

    def render(text, **kwargs):
        calls.append((text, kwargs))
        return b'%PDF-selected'

    monkeypatch.setattr(chat_exports, 'render_pdf', render)
    response = studio.c.post('/api/chats/chat/export', json={'format': 'pdf', 'selection': fragment})
    assert response.status_code == 200 and response.content == b'%PDF-selected'
    assert calls == [(fragment, {'selection': True})]
    assert state.DB.execute('SELECT count(*) FROM chat_shares').fetchone()[0] == 0
    assert studio.c.post('/api/chats/chat/export', json={'format': 'md', 'selection': fragment}).status_code == 422
    assert studio.c.post('/api/chats/chat/export', json={'format': 'pdf', 'selection': 'x' * 1_000_001}).status_code == 422
    make_user(studio, 'friend')
    signin(studio, 'friend')
    assert studio.c.post('/api/chats/chat/export', json={'format': 'pdf', 'selection': fragment}).status_code == 404
    assert len(calls) == 1


def test_selected_agent_pdf_checks_pin_without_loading_the_entire_history(studio, monkeypatch):
    aid, machine, _ = setup(studio, pin='1357')
    url = f'/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/export'

    def render(text, **kwargs):
        assert text == '<p>Фрагмент</p>' and kwargs == {'selection': True}
        studio.c.portal.call(machine.open_sessions.clear)
        return b'%PDF-private'

    monkeypatch.setattr(chat_exports, 'render_pdf', render)
    machine.requests.clear()
    response = studio.c.post(url, json={'format': 'pdf', 'selection': '<p>Фрагмент</p>'})
    assert response.status_code == 409 and b'%PDF-private' not in response.content
    history = [r for r in machine.requests if r.get('op') == 'history']
    assert history and all(r['limit'] == 1 for r in history)


def test_selection_html_keeps_formatting_but_cannot_load_resources_or_run_code():
    from markdown_it import MarkdownIt
    source = '''<h2>Заголовок</h2><p><strong>Жирный</strong> <em>Курсив</em> <del>Удалено</del></p>
<ol start="4"><li>Четвёртый<ul><li>Вложенный</li></ul></li></ol>
<table><tbody><tr><td colspan="2" align="right" style="background:url(file:///secret)">Ячейка</td></tr></tbody></table>
<pre><code class="hljs language-python"><span class="hljs-keyword">if</span> True:

    print(&quot;Тест&quot;)
</code></pre><a href="https://example.com/docs">Ссылка</a>
<a href="file:///etc/passwd">Локальная</a><img src="http://127.0.0.1/private">
<script>private-script</script><style>private-style</style><iframe src="file:///secret">private-frame</iframe>
<svg><image href="file:///secret"/></svg><p onclick="alert(1)" class="evil">Конец</p>
<div class="chat-export-plain">Первый

Второй</div>'''
    output = chat_exports._html_block(selection_html(source))
    assert MarkdownIt('commonmark', {'html': True}).render(output) == output
    for kept in ['<h2>', '<strong>', '<em>', '<del>', 'start="4"', 'colspan="2"', 'align="right"',
                 'class="hljs-keyword"', 'href="https://example.com/docs"', 'print(&quot;Тест&quot;)']:
        assert kept in output
    for removed in ['file:', '127.0.0.1', '<img', '<svg', '<script', '<iframe', 'onclick',
                    'private-script', 'private-style', 'private-frame', 'class="evil"', 'url(']:
        assert removed not in output
    assert 'True:&#10;&#10;    print' in output
    assert 'Первый&#10;&#10;Второй' in output
    with pytest.raises(HTTPException) as empty:
        selection_html('<script>hidden</script>')
    assert empty.value.status_code == 422


def test_agent_export_reads_all_pages_and_checks_computer_pin_after_render(studio, monkeypatch):
    aid, machine, _ = setup(studio, pin='1357')
    url = f'/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/export'
    response = studio.c.post(url, json={'format': 'md', 'title': 'Полная история'})
    assert response.status_code == 200
    assert response.text.count('## Пользователь\n') == 125
    assert response.text.count('## Помощник\n') == 125
    assert '\n\n0\n\n' in response.text and response.text.endswith('249\n')
    assert state.DB.execute('SELECT count(*) FROM chat_shares').fetchone()[0] == 0

    async def close():
        machine.open_sessions.clear()

    def render(_):
        studio.c.portal.call(close)
        return b'%PDF-private document'

    monkeypatch.setattr(chat_exports, 'render_pdf', render)
    assert studio.c.post(url, json={'format': 'pdf'}).status_code == 409


@pytest.mark.parametrize('selection', [False, True])
@pytest.mark.parametrize('engine', ['codex', 'opencode'])
def test_export_keeps_the_selected_agent_engine(studio, monkeypatch, engine, selection):
    import bridge
    from test_codex import CATALOG as CODEX
    from test_opencode import CATALOG as OPENCODE
    aid, machine, link = setup(studio)
    setattr(machine, engine, CODEX if engine == 'codex' else OPENCODE)
    link.hello = bridge.parse_hello(machine.hello)
    machine.projects['alpha']['chats'][A]['engine'] = engine
    url = f'/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/export'
    monkeypatch.setattr(chat_exports, 'render_pdf', lambda *args, **kwargs: b'%PDF-test')
    body = {'format': 'pdf', 'selection': '<p>Фрагмент</p>'} if selection else {'format': 'md'}
    response = studio.c.post(url + '?engine=' + engine, json=body)
    assert response.status_code == 200
    assert all(item['engine'] == engine for item in machine.requests if item.get('op') == 'history')
    assert studio.c.post(url, json=body).status_code == 404


def test_pdf_limit_does_not_block_markdown(studio, monkeypatch):
    _, uid = regular(studio)
    monkeypatch.setattr(chat_exports, 'RUNNING', {uid})
    assert studio.c.post('/api/chats/chat/export', json={'format': 'pdf'}).status_code == 429
    assert studio.c.post('/api/chats/chat/export', json={'format': 'md'}).status_code == 200


def test_pdf_timeout_cleans_work_and_never_passes_data_key(studio, monkeypatch, tmp_path):
    regular(studio)
    tool = tmp_path / 'converter.py'
    tool.write_text('# converter fixture\n', encoding='utf-8')
    monkeypatch.setattr(chat_exports, 'converter', lambda: tool)

    def timeout(argv, **kwargs):
        assert 'STUDIO_DATA_KEY' not in kwargs['env']
        assert len(argv) == 4 and argv[2].endswith('chat.md')
        directory = Path(argv[2]).parent
        assert kwargs['env']['HOME'] == str(directory)
        assert kwargs['env']['XDG_CONFIG_HOME'] == str(directory / 'config')
        assert kwargs['env']['XDG_CACHE_HOME'] == str(directory / 'cache')
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr(chat_exports.subprocess, 'run', timeout)
    response = studio.c.post('/api/chats/chat/export', json={'format': 'pdf'})
    assert response.status_code == 503 and response.json()['detail']['code'] == 'pdf_failed'
    assert not list(state.CFG.tmp_dir.glob('chat-export-*'))
    assert not chat_exports.RUNNING


def test_pdf_html_disables_raw_html_and_all_image_fetches():
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)

    output = chat_exports._safe_html('''# Кириллица

![photo](http://127.0.0.1/private)
![file](file:///etc/passwd)
<iframe src="file:///etc/passwd"></iframe><script>alert(1)</script>
<style>body{background:url(https://external.invalid/track)}</style>

| A | B |
|---|---|
| один | два |
''')
    parsed = Tags()
    parsed.feed(output)
    assert not {'img', 'iframe', 'script', 'style', 'object', 'embed'} & set(parsed.tags)
    assert 'table' in parsed.tags and '&lt;iframe' in output


def test_md2pdf_second_parse_keeps_the_safe_html_as_one_block():
    """md2pdf parses chat.md again as CommonMark with raw HTML: no blank line may end the block."""
    from markdown_it import MarkdownIt
    import html as htmllib
    import re

    code = 'line1\n\n![x](http://10.77.1.15:4000/slot)\n# comment\n\n  \ndef f(*args, **kw): *x*\n\n'
    text = chat_exports.markdown({'title': 'Чат', 'messages': [
        {'role': 'user', 'text': f'```\n{code}```\n\n> цитата\n>\n> ```\n> a\n>\n> ![q](http://evil.invalid/q)\n> ```\n\n'
                                 '| A | B |\n|---|---|\n| `x` | ![t](http://t.invalid/t) |\n\n'
                                 '- один\n\n- два\n\n      отступ\n\n      ![i](http://i.invalid/i)\n'},
        {'role': 'assistant', 'text': 'ok', 'reasoning': 'шаг\n\n![r](http://r.invalid/r)\n# заголовок\n',
         'steps': [{'tool': 'Bash', 'input': {'command': 'echo'}, 'text': 'out\n\n![s](https://s.invalid/s)\n*курсив*'}]}]})
    first = chat_exports._safe_html(text)
    parser = MarkdownIt('commonmark', {'html': True, 'typographer': False}).enable('table').enable('strikethrough')
    second = parser.render(first)
    assert second == first
    assert '<img' not in second and second.count('<h1>') == 1 and '<em>' not in second
    blocks = [htmllib.unescape(b) for b in re.findall(r'<pre[^>]*><code[^>]*>(.*?)</code></pre>', second, re.S)]
    assert code in blocks
    assert 'шаг\n\n![r](http://r.invalid/r)\n# заголовок\n\n' in blocks


def test_markdown_tool_fences_preserve_nested_code_and_attachment_names():
    result = chat_exports.markdown({'title': 'Чат', 'messages': [
        {'role': 'assistant', 'text': '**Ответ**', 'steps': [
            {'tool': 'Bash', 'input': {'command': 'echo ```'}, 'text': '```\noutput\n```'}],
         'attachments': [{'name': 'Файл.txt', 'file': 'private-media-id'}]}]})
    assert '````\n```\noutput\n```\n````' in result
    assert 'Файл.txt' in result and 'private-media-id' not in result


@pytest.mark.integration
@pytest.mark.timeout(90)
def test_real_pdf_export_is_readable_and_removes_temporary_work(studio, tmp_path):
    if not chat_exports.converter().is_file():
        pytest.skip('нет общего md2pdf.py')
    regular(studio)
    text = 'Кириллица и Markdown\n\n| Колонка | Значение |\n|---|---|\n| Тест | Работает |\n\n```python\nprint("hello")\n```'
    state.DB.execute("UPDATE messages SET content=? WHERE id='chat-a'", (text,))
    response = studio.c.post('/api/chats/chat/export', json={'format': 'pdf'})
    assert response.status_code == 200, response.text[:200]
    assert response.headers['content-type'] == 'application/pdf'
    data = response.content
    reader = PdfReader(io.BytesIO(data))
    extracted = '\n'.join(page.extract_text() for page in reader.pages)
    assert 'Кириллица' in extracted and 'Работает' in extracted and 'print' in extracted
    assert not list(state.CFG.tmp_dir.glob('chat-export-*'))
    target = tmp_path / 'chat-export.pdf'
    target.write_bytes(data)
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(data)
    try:
        for n in range(len(document)):
            page = document[n]
            try:
                bitmap = page.render(scale=1.5)
                try:
                    bitmap.to_pil().save(tmp_path / f'chat-export-{n+1}.png')
                finally:
                    bitmap.close()
            finally:
                page.close()
    finally:
        document.close()
