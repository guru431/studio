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
// /share (data-language-default="browser"): язык браузера, пока выбор не сохранён; остальные страницы — русский
function page(saved, browserDefault, browserLanguage) {
  const html = { dataset: browserDefault ? { languageDefault: 'browser' } : {} };
  const context = vm.createContext({ localStorage: { getItem: () => saved }, navigator: { language: browserLanguage },
    document: { documentElement: html, addEventListener() {} } });
  for (const file of ['en.js', 'i18n.js']) vm.runInContext(fs.readFileSync(root + '/' + file, 'utf8'), context);
  return context.StudioI18n.language();
}
assert.equal(page(null, true, 'en-GB'), 'en');
assert.equal(page('ru', true, 'en-US'), 'ru');
assert.equal(page(null, true, 'de-DE'), 'ru');
assert.equal(page(null, false, 'en-US'), 'ru');
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
// «В работе» (jobs.py): заголовки строк — шаблоном, имя внутри — как есть
assert.equal(en.message('Расшифровка «Встреча.m4a»'), 'Transcription “Встреча.m4a”');
assert.equal(en.message('Ролик · 5 с'), 'Video · 5 s');
assert.equal(en.message('Озвучка · Привет, мир…'), 'Speech · Привет, мир…');
assert.equal(en.message('Ответ · новый диалог'), 'Response · new conversation');
assert.equal(en.message('Ответ · Планы'), 'Response · Планы');
assert.equal(en.message('пишет ответ'), 'writing response');
// Составные слова: голая подстановка — тоже слова сервера, «имя» — как есть, раздел в «» — переводом
assert.equal(en.message('жду карту · озвучиваю 2 из 5'), 'waiting for GPU · synthesizing 2 of 5');
assert.equal(en.message('кусок 5 из 17: модель вернула пустой ответ'), 'chunk 5 of 17: model returned an empty response');
assert.equal(en.message('сбой службы: TimeoutError'), 'service failure: TimeoutError');
assert.equal(en.message('выберите модель в настройках раздела «Голосовой ввод»'), 'choose a model in Settings → “Voice input”');
assert.equal(en.message('нет связи с подключением «Голосовой ввод»'), 'no connection to “Голосовой ввод”');
assert.equal(en.message('Ответ · Чат'), 'Response · Чат');
assert.equal(['Чат'].map(en.message)[0], 'Chat');
assert.equal(en.message('Имя — от 1 до 196 знаков'), 'Name must be 1–196 characters');
// Слова моста: составные подписи окон лимитов «{limitName} · {label}», имя подзадачи, отказ с именем CLI
assert.equal(en.message('codex_other · 5 часов'), 'codex_other · 5 hours');
assert.equal(en.message('gpt · Неделя'), 'gpt · Week');
assert.equal(en.message('gpt · spark · 300 мин'), 'gpt · spark · 300 min');
assert.equal(en.message('Агент 019a2b3c'), 'Agent 019a2b3c');
assert.equal(en.message('Codex не найден на компьютере'), 'Codex not found on the computer');
assert.equal(en.tr('__proto__'), '__proto__');
"""
    result = subprocess.run([node, '-e', program, str(STATIC)], capture_output=True,
                            encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
