import { tr } from './i18n.js';
/* Меню «/» поля ввода «Чата» и «Агентов» и чип выбранного навыка.
 *
 * slashMenu(field, { items, onPick, host }) — блок `#slash-menu` в потоке над полем (перед `.field-box` поля или в
 * `host`), не popover: не уходит за экран. Открыт, пока текст до курсора — «/» и слово без пробелов; запрос — это
 * слово, отбор — вхождение в имя без учёта регистра, порядок — как отдал `items()`. `items()` зовётся раз на открытие
 * и отдаёт `[{kind: 'skill' | 'project' | 'command', id, name, description}]`; отказ — словами в строке статуса.
 * Клавиши ловятся раньше `enterSends` и `fileHints` (захват, `stopImmediatePropagation`): ↓ ↑ по кругу, Enter и Tab —
 * взять, Esc — закрыть и не открывать, пока начало поля подходит. Взять — убрать «/слово» из поля и отдать строку
 * `onPick` (команду хозяин вставит сам).
 * skillChip(host, { changed }) — чип «Навык: имя» с крестиком; крестик снимает навык и зовёт `changed()`. */
import { el, words } from './ui.js';
import { iconButton } from './icons.js';

const OPEN = /^\/(\S*)$/;
const TAKEN = /^\/\S*[ \t]*/;
const KINDS = { skill: () => tr("навык"), project: () => tr("проект"), command: () => tr("команда") };

export function slashMenu(field, { items, onPick, host = null }) {
  const list = el('div', { role: 'listbox', id: 'slash-options', 'aria-label': tr("Навыки и команды") });
  const status = el('p', { class: 'small muted', role: 'status' });
  const menu = el('div', { class: 'slash-menu', id: 'slash-menu', hidden: true }, list, status);
  if (host) host.append(menu);
  else (field.closest('.field-box') || field).before(menu);
  let open = false;       // меню показано
  let muted = false;      // Esc: не открывать, пока начало поля подходит
  let all = null;         // ответ items() этого открытия
  let failure = '';
  let rows = [];
  let selected = 0;
  let generation = 0;
  let controls = null;    // прежний aria-controls поля (подсказки файлов «Агентов»)

  function query() {
    if (field.selectionStart !== field.selectionEnd) return null;
    return field.value.slice(0, field.selectionStart).match(OPEN)?.[1] ?? null;
  }

  function close() {
    generation += 1;
    all = null;
    rows = [];
    menu.hidden = true;
    if (!open) return;
    open = false;
    if (controls) field.setAttribute('aria-controls', controls);
    else field.removeAttribute('aria-controls');
    field.setAttribute('aria-expanded', 'false');
    field.removeAttribute('aria-activedescendant');
  }

  function paint() {
    list.replaceChildren(...rows.map((item, index) => {
      const button = el('button', { type: 'button', role: 'option', id: `slash-option-${index}`, class: 'slash-option',
        tabindex: '-1', 'aria-selected': String(index === selected), 'data-kind': item.kind, 'data-id': item.id },
        el('span', { class: 'slash-name', title: item.name }, item.name),
        el('span', { class: 'tag slash-kind' }, KINDS[item.kind]?.() ?? ''),
        el('span', { class: 'slash-desc muted' }, item.description || ''));
      button.addEventListener('mousedown', (event) => event.preventDefault());   // фокус остаётся в поле
      button.addEventListener('click', () => pick(index));
      return button;
    }));
    if (rows.length) field.setAttribute('aria-activedescendant', `slash-option-${selected}`);
    else field.removeAttribute('aria-activedescendant');
    status.textContent = failure || (rows.length ? tr("Enter — выбрать · Esc — закрыть") : tr("Ничего не найдено"));
  }

  function filter(word) {
    const needle = word.toLocaleLowerCase();
    rows = all.filter((item) => String(item.name).toLocaleLowerCase().includes(needle));
    selected = 0;
    paint();
  }

  async function update() {
    const word = query();
    if (word === null) {
      muted = false;
      close();
      return;
    }
    if (muted) return;
    if (open) {
      if (all) filter(word);
      return;
    }
    open = true;
    failure = '';
    controls = field.getAttribute('aria-controls');
    field.setAttribute('aria-controls', list.id);
    field.setAttribute('aria-expanded', 'true');
    menu.hidden = false;
    list.replaceChildren();
    status.textContent = tr("Загрузка…");
    const mine = ++generation;
    let got;
    try {
      got = await items();
    } catch (err) {
      if (mine !== generation) return;
      failure = words(err);
      got = [];
    }
    if (mine !== generation) return;
    all = Array.isArray(got) ? got : [];
    const current = query();
    if (current === null) close();
    else filter(current);
  }

  function pick(index) {
    const item = rows[index];
    if (!item) return;
    field.value = field.value.replace(TAKEN, '');
    close();
    field.focus();
    field.dispatchEvent(new Event('input', { bubbles: true }));
    onPick(item);
  }

  function keydown(event) {
    if (!open || event.isComposing) return;
    const move = rows.length > 0 && ['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key);
    if (event.key !== 'Escape' && !move) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.key === 'Escape') {
      muted = true;
      close();
    } else if (event.key === 'Enter' || event.key === 'Tab') pick(selected);
    else {
      selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      paint();
      list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
    }
  }

  field.addEventListener('input', update);
  field.addEventListener('click', update);
  field.addEventListener('keydown', keydown, { capture: true });
  return {
    close,
    unmount() {
      close();
      field.removeEventListener('input', update);
      field.removeEventListener('click', update);
      field.removeEventListener('keydown', keydown, { capture: true });
      menu.remove();
    },
  };
}

export function skillChip(host, { changed } = {}) {
  const name = el('span', { class: 'skill-chip-name' });
  const remove = iconButton('x', tr("Убрать навык"));
  const node = el('div', { class: 'skill-chip', id: 'skill-chip', hidden: true }, name, remove);
  host.prepend(node);
  let value = null;

  function clear() {
    value = null;
    name.textContent = '';
    name.removeAttribute('title');
    node.hidden = true;
  }

  remove.addEventListener('click', () => {
    clear();
    changed?.();
  });
  return {
    node,
    set(item) {
      value = item;
      name.textContent = tr("Навык: {0}", item.name);
      name.title = item.name;
      node.hidden = false;
    },
    clear,
    value: () => value,
  };
}
