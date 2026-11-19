"""Редактор «Файлов»: концы строк после правки — настоящий модуль line-endings.js в node."""
import shutil
import subprocess

import pytest

from conftest import APP

MODULE = APP / 'static' / 'js' / 'line-endings.js'


def test_edit_keeps_each_untouched_line_ending():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    program = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const { restoreEndings } = await import('data:text/javascript,' + encodeURIComponent(fs.readFileSync(process.argv[1], 'utf8')));
  const view = (text) => text.replace(/\r\n?/g, '\n');           // так значение отдаёт textarea
  const mixed = 'a\r\nb\nc\rd\r\ne\n';
  assert.equal(restoreEndings(mixed, view(mixed)), mixed);
  // Правка одного знака не трогает разделители остальных строк.
  assert.equal(restoreEndings(mixed, view(mixed).replace('c', 'C')), 'a\r\nb\nC\rd\r\ne\n');
  // Новая строка получает самый частый разделитель файла, соседние — свои.
  assert.equal(restoreEndings(mixed, 'a\nb\nnew\nc\nd\ne\n'), 'a\r\nb\nnew\r\nc\rd\r\ne\n');
  assert.equal(restoreEndings(mixed, 'a\nd\ne\n'), 'a\r\nd\r\ne\n');
  assert.equal(restoreEndings('x\r\ny\r\n', 'x\nz\ny\n'), 'x\r\nz\r\ny\r\n');
  assert.equal(restoreEndings('x\ny\n', 'x\nz\n'), 'x\nz\n');
  assert.equal(restoreEndings('', 'new'), 'new');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    result = subprocess.run([node, '-e', program, str(MODULE)], capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
