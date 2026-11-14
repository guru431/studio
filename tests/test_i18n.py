"""Localization contracts: catalog coverage, safe interpolation and device language selection."""
import json
import re
import shutil
import subprocess

import pytest

from conftest import APP

STATIC = APP / 'static'


def catalog():
    source = (STATIC / 'en.js').read_text(encoding='utf-8')
    def unique(pairs):
        assert len(dict(pairs)) == len(pairs), 'duplicate translation key'
        return dict(pairs)
    return json.JSONDecoder(object_pairs_hook=unique).raw_decode(source.split('Object.freeze(', 1)[1])[0]


def test_interface_catalog_has_every_explicit_translation_and_matching_parameters():
    english = catalog()
    assert english and all(isinstance(value, str) and value for value in english.values())
    for source, translated in english.items():
        assert sorted(re.findall(r'\{\d*\}', source)) == sorted(re.findall(r'\{\d*\}', translated)), source
    # Calls use literal source keys; values such as filenames and messages remain separate arguments.
    for path in [*STATIC.glob('*.js'), *(STATIC / 'js').glob('*.js')]:
        if path.name == 'en.js':
            continue
        for literal in re.findall(r'\btr\(("(?:[^"\\]|\\.)*")', path.read_text(encoding='utf-8')):
            assert json.loads(literal) in english, (path.name, literal)


def test_language_runtime_defaults_validation_and_safe_error_interpolation():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = process.argv[1];
function runtime(saved, broken = false) {
  const context = vm.createContext({ localStorage: {
    getItem() { if (broken) throw new Error('storage blocked'); return saved; },
  }});
  for (const file of ['en.js', 'i18n.js']) vm.runInContext(fs.readFileSync(root + '/' + file, 'utf8'), context);
  return context.StudioI18n;
}
for (const saved of [null, 'ru', 'fr', '<script>', 'EN']) {
  const i18n = runtime(saved);
  assert.equal(i18n.language(), 'ru');
  assert.equal(i18n.tr('Чат'), 'Чат');
}
assert.equal(runtime(null, true).locale(), 'ru-RU');
const en = runtime('en');
assert.equal(en.language(), 'en');
assert.equal(en.locale(), 'en-US');
assert.equal(en.tr('Чат'), 'Chat');
assert.equal(en.tr('Unknown'), 'Unknown');
assert.equal(en.tr('Файл {0}', '$& {1} <script>'), 'Файл $& {1} <script>');
assert.equal(en.tr('Назвать устройство: {0}', 'Чат'), 'Name device: Чат');
assert.equal(en.message('текущий пароль неверен'), 'current password incorrect');
assert.equal(en.message('Текущий пароль неверен'), 'Current password incorrect');
assert.equal(en.message('пароль короче 10 символов'), 'password shorter than 10 characters');
assert.equal(en.message('нет связи с подключением «Чат»'), 'no connection to “Чат”');
assert.equal(en.message('карта занята дольше 15 минут — попробуйте позже'), 'GPU busy for more than 15 minutes. Try again later');
assert.equal(en.message('Custom provider error: Чат'), 'Custom provider error: Чат');
assert.equal(en.tr('__proto__'), '__proto__');
"""
    result = subprocess.run([node, '-e', program, str(STATIC)], capture_output=True,
                            encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
