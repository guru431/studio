import { locale, tr } from './i18n.js';
/* Навыки «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 1, 2, 17): меню «/» поля ввода и чип
 * «Навык: имя» над полем (slash.js), навык в ходе и сообщение с навыком в ленте.
 *
 * chatSkills({ input, host, changed }) — меню «/» по навыкам учётки (GET /api/skills?scope=chat&lang=, список — на
 * каждое открытие меню) и чип `#skill-chip` в `host` (`#chat-skill`, скрыт без навыка). `changed()` — навык выбран или
 * снят. В ход уходит `body()`: `{skill_id}` выбранного навыка; при правке сообщения с навыком (`keep(m.skill)`) —
 * `{skill_keep: true}`: сервер оставит прежний текст навыка, даже если в библиотеке его изменили или удалили; без
 * чипа — `{}`. Текст навыка странице не нужен: его вписывает сервер.
 * skillMessage(m) — сообщение пользователя в ленте: `head` — свёрнутое «Навык: имя» (`details.msg-skill`, внутри — текст
 * навыка), `text` — то, что дописал человек (humanText). */
import { api } from './api.js';
import { skillChip, slashMenu } from './slash.js';
import { el } from './ui.js';

const lang = () => (locale().toLowerCase().startsWith('en') ? 'en' : 'ru');

/** То, что дописал человек: `content` после текста навыка без ведущей пустой строки (как chat_skills.human). */
export function humanText(m) {
  const content = typeof m?.content === 'string' ? m.content : '';
  const chars = Number.isInteger(m?.skill?.chars) ? m.skill.chars : 0;
  return m?.skill ? content.slice(chars).replace(/^\n\n/, '') : content;
}

export function skillMessage(m) {
  if (!m?.skill) return { head: null, text: humanText(m) };
  const own = (m.content || '').slice(0, m.skill.chars || 0);
  const head = el('details', { class: 'msg-skill' },
    el('summary', { title: m.skill.name }, tr("Навык: {0}", m.skill.name)),
    own ? el('div', { class: 'msg-skill-text' }, own) : null);
  return { head, text: humanText(m) };
}

export function chatSkills({ input, host, changed }) {
  let kept = null;             // навык сообщения, которое правят: строка чипа без id
  const chip = skillChip(host, { changed: () => {
    kept = null;
    sync();
    changed?.();
  } });

  function sync() {
    host.hidden = chip.node.hidden;
  }

  async function items() {
    const { items: rows = [] } = await api('GET', `/api/skills?scope=chat&lang=${lang()}`);
    return rows.map((row) => ({ kind: 'skill', id: row.id, name: row.name, description: row.description || '',
      parts: !!row.parts }));
  }

  const menu = slashMenu(input, { items, onPick: (item) => {
    kept = null;
    chip.set(item);
    sync();
    changed?.();
  } });
  sync();

  function clear() {
    kept = null;
    chip.clear();
    sync();
  }

  return {
    chosen: () => chip.value(),
    /** Поля хода: навык библиотеки, прежний навык правки или ничего. */
    body() {
      const item = chip.value();
      if (!item) return {};
      return item === kept ? { skill_keep: true } : { skill_id: item.id };
    },
    /** `skill` показанного до ответа сервера сообщения: текст навыка — у сервера, `chars` — 0. */
    view() {
      const item = chip.value();
      return item ? { name: item.name, chars: 0, parts: !!item.parts } : null;
    },
    /** Правка сообщения: его навык — снова чипом; снять крестиком — правка без навыка. */
    keep(skill) {
      if (!skill) { clear(); return; }
      kept = { kind: 'skill', id: null, name: skill.name, description: '', parts: !!skill.parts };
      chip.set(kept);
      sync();
    },
    clear,
    /** Чип, выбранный до правки (`chosen()` тогда), — снова; null — без чипа. */
    restore(item) {
      if (!item) { clear(); return; }
      kept = null;
      chip.set(item);
      sync();
    },
    close: () => menu.close(),
    /** Список навыков читается при каждом открытии меню: закрыть — следующее откроет свежий. */
    reload: () => menu.close(),
    unmount() {
      menu.unmount();
      chip.node.remove();
    },
  };
}
