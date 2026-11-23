import { tr } from './i18n.js';
/* «Настройки → Перевод», блок `#translate-glossary`: тон перевода и глоссарий учётки (translate.py
 * `GET/PUT /api/translate/glossary`). Глоссарий — поле строками «термин = перевод» (разделитель — первый «=», «→» или
 * табуляция); сервер хранит пары по порядку и отдаёт их обратно, в указание модели идут только встретившиеся в куске. */
import { api } from './api.js';
import { button, el, field, words } from './ui.js';

const SPLIT = /\s*(?:=|→|\t)\s*/;

function parse(text) {
  const pairs = [];
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line) continue;
    const m = SPLIT.exec(line);
    if (!m || !line.slice(0, m.index).trim() || !line.slice(m.index + m[0].length).trim()) {
      throw new Error(tr("Строка {0}: нужно «термин = перевод»", i + 1));
    }
    pairs.push({ term: line.slice(0, m.index), translation: line.slice(m.index + m[0].length) });
  }
  return pairs;
}

export function glossaryBlock() {
  const tone = el('select', { class: 'input', id: 'translate-tone' },
    el('option', { value: '' }, tr("Как в исходнике")), el('option', { value: 'formal' }, tr("Официально")),
    el('option', { value: 'plain' }, tr("Просто")));
  const terms = el('textarea', { class: 'input', id: 'translate-glossary-terms', rows: '6', spellcheck: 'false',
    placeholder: tr("Acme Corp = АО «Акме»") });
  const note = el('p', { class: 'status', id: 'translate-glossary-status', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn', id: 'translate-glossary-save' });
  const say = (text, kind) => {
    note.textContent = text;
    note.classList.toggle('error', kind === 'error');
    note.classList.toggle('ok', kind === 'ok');
  };
  const show = (data) => {
    tone.value = data.tone;
    terms.value = data.glossary.map((p) => `${p.term} = ${p.translation}`).join('\n');
  };
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      show(await api('PUT', '/api/translate/glossary', { tone: tone.value, glossary: parse(terms.value) }));
      say(tr("Сохранено"), 'ok');
    } catch (err) {
      say(words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  api('GET', '/api/translate/glossary').then(show, (err) => say(words(err), 'error'));
  return el('section', { class: 'block', id: 'translate-glossary' }, el('h2', {}, tr("Глоссарий и тон")),
    field(tr("Тон перевода"), tone, 'tone'), field(tr("Термины — по одному на строке: «термин = перевод»"), terms, 'glossary'),
    el('p', { class: 'muted small' }, tr("Термины и имена переводятся одинаково во всех кусках документа. Модель получает только пары, которые встретились в переводимом куске, — через Cleaner вместе с текстом и его очисткой.")),
    el('div', { class: 'row' }, save, note));
}
