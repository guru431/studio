"""Whole-chat exports preserve owner/PIN access through asynchronous PDF rendering."""
from html.parser import HTMLParser
import io
from pathlib import Path
import subprocess

from pypdf import PdfReader
import pytest

import chat_exports
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


@pytest.mark.parametrize('action', ['delete_chat', 'logout', 'lock', 'disable'])
def test_pdf_does_not_escape_a_closed_session_or_deleted_source(studio, monkeypatch, action):
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

    def render(_):
        studio.c.portal.call(invalidate)
        return b'%PDF-private document'

    monkeypatch.setattr(chat_exports, 'render_pdf', render)
    response = studio.c.post('/api/chats/chat/export', json={'format': 'pdf'})
    assert response.status_code in (404, 409)
    assert b'private document' not in response.content
    assert not chat_exports.RUNNING


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


@pytest.mark.parametrize('engine', ['codex', 'opencode'])
def test_export_keeps_the_selected_agent_engine(studio, engine):
    import bridge
    from test_codex import CATALOG as CODEX
    from test_opencode import CATALOG as OPENCODE
    aid, machine, link = setup(studio)
    setattr(machine, engine, CODEX if engine == 'codex' else OPENCODE)
    link.hello = bridge.parse_hello(machine.hello)
    machine.projects['alpha']['chats'][A]['engine'] = engine
    url = f'/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/export'
    response = studio.c.post(url + '?engine=' + engine, json={'format': 'md'})
    assert response.status_code == 200
    assert all(item['engine'] == engine for item in machine.requests if item.get('op') == 'history')
    assert studio.c.post(url, json={'format': 'md'}).status_code == 404


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
