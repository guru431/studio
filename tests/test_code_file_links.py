"""Ссылки на файлы в ответах помощника: настоящий модуль code-file-links.js в node, без браузера."""
import shutil
import subprocess

import pytest

from conftest import APP

MODULE = APP / 'static' / 'js' / 'code-file-links.js'


def run(program: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    prelude = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
globalThis.location = { origin: 'https://studio.example.com' };
(async () => {
  const links = await import('data:text/javascript,' + encodeURIComponent(fs.readFileSync(process.argv[1], 'utf8')));
"""
    result = subprocess.run([node, '-e', prelude + program + '\n})().catch((e) => { console.error(e); process.exit(1); });',
                             str(MODULE)], capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr


def test_windows_paths_stay_local_and_resolve_inside_the_project():
    run(r"""
  const agent = { projects_root: 'C:\\work\\projects' }, project = { name: 'alpha' };
  assert.equal(links.localFileHref('C:\\work\\projects\\alpha\\a.py'), '/C:/work/projects/alpha/a.py');
  assert.equal(links.projectFilePath('C:\\work\\projects\\alpha\\src\\a.py:12', agent, project), 'src/a.py');
  assert.equal(links.projectFilePath('file:///c:/WORK/projects/alpha/a.py', agent, project), 'a.py');
  assert.equal(links.projectFilePath('C:\\work\\projects\\beta\\a.py', agent, project), null);
  assert.equal(links.localFileHref('https://example.com/a'), null);
""")


def test_posix_paths_never_become_http_links_and_resolve_inside_the_project():
    run(r"""
  const agent = { projects_root: '/Users/owner/projects' }, project = { name: 'alpha' };
  for (const value of ['/Users/owner/projects/alpha/a.py', 'file:///Users/owner/projects/alpha/a.py',
                       '/home/owner/notes%20one.md', 'https://studio.example.com/C:/x.py']) {
    assert.ok(links.localFileHref(value), value);
  }
  assert.equal(links.localFileHref('//cdn.example.com/x.js'), null);
  assert.equal(links.projectFilePath('/Users/owner/projects/alpha/src/a.py:3:1', agent, project), 'src/a.py');
  assert.equal(links.projectFilePath('file:///Users/owner/projects/alpha/a.py', agent, project), 'a.py');
  // POSIX различает регистр: чужая папка с тем же именем в другом регистре — вне проекта.
  assert.equal(links.projectFilePath('/Users/owner/projects/Alpha/a.py', agent, project), null);
  assert.equal(links.projectFilePath('/Users/owner/other/a.py', agent, project), null);
  assert.equal(links.visualizationFilePath('/Users/owner/projects/alpha/report.html'),
               '/Users/owner/projects/alpha/report.html');
  assert.equal(links.visualizationFilePath('C:\\work\\report.html'), 'C:/work/report.html');
""")
