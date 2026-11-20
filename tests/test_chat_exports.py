"""Whole-chat exports preserve owner/PIN access through asynchronous PDF rendering."""
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import subprocess

from pypdf import PdfReader
import pytest
from fastapi import HTTPException

import chat_exports
import chat_shares
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


@pytest.mark.parametrize('selection', [None, '<p>Фрагмент</p>'])
def test_agent_export_refuses_bad_project_and_chat_before_the_bridge(studio, selection):
    """FINDINGS 07:F5: `pid` не 16 hex и `chat` не UUID — 422 до моста и с выделением, как без него."""
    aid, machine, _ = setup(studio)
    machine.requests.clear()
    body = {'format': 'pdf', **({'selection': selection} if selection else {})}
    for pid, chat, code in (('NOT-HEX', A, 'project'), (ALPHA, 'not-a-uuid', 'chat')):
        response = studio.c.post(f'/api/code/agents/{aid}/projects/{pid}/chats/{chat}/export', json=body)
        assert response.status_code == 422 and response.json()['detail']['code'] == code
    assert machine.requests == []


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
    # a chat md2pdf cannot print within its budget: a retry would not help
    assert response.status_code == 503 and response.json()['detail']['code'] == 'pdf_timeout'
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
    first = chat_exports.pdf_html({'title': 'Чат', 'messages': [
        {'role': 'user', 'text': 'вопрос\n\n![u](http://u.invalid/u)\n\n# не заголовок'},
        {'role': 'assistant', 'text': f'```\n{code}```\n\n> цитата\n>\n> ```\n> a\n>\n> ![q](http://evil.invalid/q)\n> ```\n\n'
                                      '| A | B |\n|---|---|\n| `x` | ![t](http://t.invalid/t) |\n\n'
                                      '- один\n\n- два\n\n      отступ\n\n      ![i](http://i.invalid/i)\n',
         'reasoning': 'шаг\n\n![r](http://r.invalid/r)\n# заголовок\n',
         'steps': [{'tool': 'Bash', 'input': {'command': 'echo'}, 'text': 'out\n\n![s](https://s.invalid/s)\n*курсив*'}]}]})
    chat_exports._printable(first)
    parser = MarkdownIt('commonmark', {'html': True, 'typographer': False}).enable('table').enable('strikethrough')
    second = parser.render(first)
    assert second == first
    assert '<img' not in second and second.count('<h1>') == 1 and '<em>' not in second
    blocks = [htmllib.unescape(b) for b in re.findall(r'<pre[^>]*><code[^>]*>(.*?)</code></pre>', second, re.S)]
    assert code in blocks
    assert 'шаг\n\n![r](http://r.invalid/r)\n# заголовок\n\n' in blocks


MD2PDF_PARSER = None


def md2pdf_render(text):
    """What md2pdf makes of chat.md: CommonMark with raw HTML, CR and CRLF are line ends."""
    from markdown_it import MarkdownIt
    global MD2PDF_PARSER
    MD2PDF_PARSER = MD2PDF_PARSER or MarkdownIt(
        'commonmark', {'html': True, 'typographer': False}).enable('table').enable('strikethrough')
    return MD2PDF_PARSER.render(text)


CARRIAGE = ['a\r\r![x](http://127.0.0.1:9/p)', 'a&#13;&#13;![x](http://127.0.0.1:9/p)',
            'a\r\n\r\n![x](http://127.0.0.1:9/p)', 'a&#13;&#10;&#13;&#10;![x](http://127.0.0.1:9/p)']


@pytest.mark.parametrize('text', CARRIAGE)
def test_carriage_returns_do_not_end_the_safe_html_block(text):
    first = chat_exports._safe_html(text)
    assert md2pdf_render(first) == first and '<img' not in md2pdf_render(first)
    chat_exports._printable(first)


@pytest.mark.parametrize('text', CARRIAGE + [t.replace('a', '<b>a</b>', 1) for t in CARRIAGE])
@pytest.mark.parametrize('wrap', ['<p>{}</p>', '<pre><code>{}</code></pre>', '<p title="{}">t</p>'])
def test_carriage_returns_do_not_end_the_selection_block(text, wrap):
    first = chat_exports._html_block(selection_html(wrap.format(text)))
    assert md2pdf_render(first) == first and '<img' not in md2pdf_render(first)
    chat_exports._printable(first)


@pytest.mark.parametrize('text', [
    '<div>\na\n\n![x](http://127.0.0.1:9/p)</div>\n',
    '<div>\n<img src="file:///etc/passwd"></div>\n',
    '<div>\n<p style="background:url(http://127.0.0.1:9/p)">x</p></div>\n',
    '<div>\n<picture><source srcset="http://127.0.0.1:9/p"></picture></div>\n',
    '<div>\n<style>body{background:url(http://127.0.0.1:9/p)}</style></div>\n',
    '<div>\n<video poster="http://127.0.0.1:9/p"></video></div>\n',
])
def test_printable_refuses_what_studio_did_not_write(text):
    with pytest.raises(ValueError):
        chat_exports._printable(text)


def test_render_pdf_refuses_before_starting_the_converter(studio, monkeypatch, tmp_path):
    tool = tmp_path / 'converter.py'
    tool.write_text('# converter fixture\n', encoding='utf-8')
    monkeypatch.setattr(chat_exports, 'converter', lambda: tool)
    monkeypatch.setattr(chat_exports, '_html_block', lambda body: '<div>\n<img src="http://127.0.0.1:9/p"></div>\n')
    monkeypatch.setattr(chat_exports.subprocess, 'run', lambda *a, **k: pytest.fail('converter started'))
    with pytest.raises(HTTPException) as refused:
        chat_exports.render_pdf('<p>x</p>', selection=True)
    assert refused.value.status_code == 503 and refused.value.detail['code'] == 'pdf_failed'
    assert not list(state.CFG.tmp_dir.glob('chat-export-*'))


@pytest.mark.parametrize('platform', ['linux', 'win32'])
def test_server_chromium_gets_no_network(studio, monkeypatch, tmp_path, platform):
    tool = tmp_path / 'converter.py'
    tool.write_text('# converter fixture\n', encoding='utf-8')
    monkeypatch.setattr(chat_exports, 'converter', lambda: tool)
    monkeypatch.delenv('MD2PDF_BROWSER', raising=False)
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs['env'].get('MD2PDF_BROWSER'))
        Path(argv[3]).write_bytes(b'%PDF-' + b'x' * 2048)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(chat_exports.subprocess, 'run', run)
    monkeypatch.setattr(chat_exports.sys, 'platform', platform)
    assert chat_exports.render_pdf('<p>x</p>', selection=True).startswith(b'%PDF-')
    assert seen == [str(chat_exports.OFFLINE_BROWSER) if platform == 'linux' else None]


USER_TEXT = 'Адрес:\nул. Ленина, 1\r\nкв. 5\n# не заголовок\n1. не список\n## Помощник\n<b>не html</b> *не курсив*'


def test_user_message_keeps_its_lines_and_never_becomes_markdown():
    """FINDINGS 06:F1: the chat shows a user's message line by line (pre-wrap); PDF and .md must too."""
    snapshot = {'title': 'Чат', 'messages': [{'role': 'user', 'text': USER_TEXT},
                                            {'role': 'assistant', 'text': '# Ответ\n\n*курсив*'}]}
    document = chat_exports.pdf_html(snapshot)
    chat_exports._printable(document)
    assert md2pdf_render(document) == document
    plain = re.search(r'<div class="chat-export-plain">(.*?)</div>', document)[1]
    assert plain == ('Адрес:&#10;ул. Ленина, 1&#10;кв. 5&#10;# не заголовок&#10;1. не список&#10;## Помощник&#10;'
                     '&lt;b&gt;не html&lt;/b&gt; *не курсив*')
    assert document.count('<h2>') == 2 and '<ol>' not in document and '<b>' not in document
    assert '<h1>Ответ</h1>' in document and '<em>курсив</em>' in document     # the model's Markdown stays Markdown
    text = chat_exports.markdown(snapshot)
    assert ('Адрес:  \nул. Ленина, 1  \nкв. 5  \n\\# не заголовок  \n1\\. не список  \n\\## Помощник  \n'
            '\\<b>не html</b> *не курсив*\n\n## Помощник\n\n# Ответ') in text
    assert text.count('\n## Помощник\n') == 1


def test_export_keeps_sources_error_words_and_marks_an_unfinished_answer(studio):
    """FINDINGS 06:F9: what the chat page shows under an answer reaches the snapshot, the link and the files."""
    _, uid = regular(studio)
    tools = [{'kind': 'search', 'query': 'погода [x]', 'results': [{'title': 'Пример *1*', 'url': 'https://example.org/a',
                                                                   'published': '2026-11-01'}], 'text': 'PRIVATE-MODEL-TEXT'},
             {'kind': 'page', 'url': 'https://bad.example/p', 'error': 'адрес не из переписки'},
             {'kind': 'mcp', 'server': 'files', 'tool': 'read', 'text': 'PRIVATE-TOOL-TEXT'}]
    state.DB.execute("UPDATE messages SET tools=? WHERE id='chat-a'", (json.dumps(tools),))
    state.DB.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created,error)"
                     " VALUES('chat-e','chat','assistant','','error','server',2,'шлюз не ответил: 502')")
    state.DB.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created)"
                     " VALUES('chat-s','chat','assistant','Половина отв','streaming','server',3)")
    snapshot, _ = chat_shares.regular_snapshot(uid, 'chat')
    assert [m.get('error') for m in snapshot['messages']] == [None, None, 'шлюз не ответил: 502', None]
    assert 'PRIVATE' not in json.dumps(snapshot, ensure_ascii=False)
    text = studio.c.post('/api/chats/chat/export', json={'format': 'md'}).text
    assert 'PRIVATE' not in text
    assert ('### Источники (1)\n\n- Поиск: «погода \\[x\\]»\n  - [Пример \\*1\\*](<https://example.org/a>) · 2026-11-01\n'
            '- Страница: https://bad.example/p — адрес не из переписки\n- MCP · files · read') in text
    assert 'Ошибка: шлюз не ответил: 502' in text
    assert 'Половина отв\n\n*(ответ не дописан)*' in text
    document = chat_exports.pdf_html(snapshot)
    chat_exports._printable(document)
    assert '<a href="https://example.org/a">' in document and 'bad.example/p</a>' not in document
    assert '<div class="chat-export-plain">Ошибка: шлюз не ответил: 502</div>' in document


def test_english_export_translates_studio_words_but_not_the_conversation(studio):
    """FINDINGS 06:F4: the file follows the device's interface language (`lang`); messages and names stay as is."""
    _, uid = regular(studio)
    tools = [{'kind': 'search', 'query': 'погода', 'results': [{'title': 'Пример', 'url': 'https://example.org/a'}]}]
    state.DB.execute("UPDATE messages SET tools=? WHERE id='chat-a'", (json.dumps(tools),))
    state.DB.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created)"
                     " VALUES('chat-s','chat','assistant','Половина отв','streaming','server',3)")
    text = studio.c.post('/api/chats/chat/export', json={'format': 'md', 'lang': 'en'}).text
    assert text.startswith('# Private title\n\n## User\n\nPrivate question') and '## Assistant' in text
    assert '### Sources (1)\n\n- Search: “погода”' in text and 'Половина отв\n\n*(answer not finished)*' in text
    assert not re.search('Пользователь|Помощник|Источники|Поиск|ответ не дописан', text)
    ru = studio.c.post('/api/chats/chat/export', json={'format': 'md'}).text
    assert '## Пользователь' in ru and '### Источники (1)' in ru
    assert studio.c.post('/api/chats/chat/export', json={'format': 'md', 'lang': 'de'}).status_code == 422
    snapshot, _ = chat_shares.regular_snapshot(uid, 'chat')
    document = chat_exports.pdf_html(snapshot, 'en')
    chat_exports._printable(document)
    assert '<h2>User</h2>' in document and 'Пользователь' not in document
    steps = chat_exports.markdown({'title': '', 'messages': [
        {'role': 'assistant', 'text': '', 'steps': [{'input': 'ls', 'text': ''}], 'reasoning': 'думаю',
         'error': 'отказ', 'attachments': [{'file': 'x'}, {'name': 'Отчёт.txt', 'file': 'y'}]}]}, 'en')
    assert steps.startswith('# Chat\n\n## Assistant\n\n') and '### Command' in steps and '### Reasoning' in steps
    assert 'Error: отказ' in steps and 'Attachment: File' in steps and 'Attachment: Отчёт.txt' in steps
    # чат без названия: снимок не подставляет русское «Чат» — заголовок на языке файла
    state.DB.execute("UPDATE chats SET title='' WHERE id='chat'")
    assert studio.c.post('/api/chats/chat/export', json={'format': 'md', 'lang': 'en'}).text.startswith('# Chat\n')


def test_export_refusals_say_what_happened(studio, monkeypatch):
    """FINDINGS 06:F15: a big chat is «too big for export», not «for a link»; a busy service is not «your PDF»."""
    _, uid = regular(studio)
    monkeypatch.setattr(chat_shares, 'MAX_SNAPSHOT', 10)
    big = studio.c.post('/api/chats/chat/export', json={'format': 'md'})
    assert big.status_code == 413 and big.json()['detail']['code'] == 'export_size'
    assert 'экспорта' in big.json()['detail']['message']
    monkeypatch.setattr(chat_shares, 'MAX_SNAPSHOT', 16 * 1024 * 1024)
    monkeypatch.setattr(chat_exports, 'RUNNING', {uid + 100, uid + 101})
    busy = studio.c.post('/api/chats/chat/export', json={'format': 'pdf'})
    assert busy.status_code == 429 and 'другие PDF' in busy.json()['detail']['message']
    monkeypatch.setattr(chat_exports, 'RUNNING', {uid})
    assert 'уже готовится' in studio.c.post('/api/chats/chat/export', json={'format': 'pdf'}).json()['detail']['message']


@pytest.mark.parametrize('locked', [2, 99])
def test_locked_work_directory_never_fails_a_finished_pdf(studio, monkeypatch, tmp_path, locked):
    """A browser outliving md2pdf holds its profile lockfile (WinError 32): retry, then leave it for clean_tmp."""
    tool = tmp_path / 'converter.py'
    tool.write_text('# converter fixture\n', encoding='utf-8')
    monkeypatch.setattr(chat_exports, 'converter', lambda: tool)

    def run(argv, **kwargs):
        Path(argv[3]).write_bytes(b'%PDF-' + b'x' * 2048)
        return subprocess.CompletedProcess(argv, 0)

    real, calls, pauses = chat_exports.shutil.rmtree, [], []

    def rmtree(path, ignore_errors=False):
        calls.append(path)
        if len(calls) <= locked:
            if ignore_errors:
                return
            raise PermissionError(32, 'The process cannot access the file')
        real(path)

    monkeypatch.setattr(chat_exports.subprocess, 'run', run)
    monkeypatch.setattr(chat_exports.shutil, 'rmtree', rmtree)
    monkeypatch.setattr(chat_exports, 'PAUSE', pauses.append)
    assert chat_exports.render_pdf('<p>x</p>', selection=True).startswith(b'%PDF-')
    left = list(state.CFG.tmp_dir.glob('chat-export-*'))
    if locked == 2:
        assert not left and pauses == list(chat_exports.CLEANUP_PAUSES[:2])
    else:
        assert len(left) == 1 and pauses == list(chat_exports.CLEANUP_PAUSES)
        monkeypatch.setattr(chat_exports.shutil, 'rmtree', real)
        chat_exports.clean_tmp()
        assert not list(state.CFG.tmp_dir.glob('chat-export-*'))


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
