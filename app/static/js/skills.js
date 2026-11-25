import { language, tr } from './i18n.js';
/* Навыки (skills.py, спека 2026-11-25-studio-skills-status-local-models, п. 1, 5): именованный текст, который человек
 * вызывает через «/», а Studio подставляет в сообщение.
 *
 * `skillsBlock` — «Настройки → Навыки», блок `#skills-settings`: список по порядку, создать, изменить (окно читает
 * полный текст по id), выше и ниже, удалить, «Добавить навыки по умолчанию». Первое чтение засевает библиотеку
 * умолчаниями языка страницы и переносит в неё свои быстрые действия «Агентов». После каждой записи — `changed()`:
 * кнопки пустого чата «Агентов» читают профиль (`me.code_quick_actions`). `skillDefaultsBlock` — панель
 * администратора, блок `#admin-skill-defaults`: умолчания по языкам; меняют только посев новых учёток и «Добавить
 * навыки по умолчанию». */
import { api } from './api.js';
import { icon } from './icons.js';
import { NAMES } from './sections-meta.js';
import { button, el, field, openDialog, words } from './ui.js';

const NAME_MAX = 80;            // как skills.NAME_MAX
const DESCRIPTION_MAX = 200;    // как skills.DESCRIPTION_MAX
const TEXT_MAX = 100000;        // как skills.TEXT_MAX

const lang = () => (language() === 'en' ? 'en' : 'ru');
const scopeNames = () => ({ chat: NAMES.chat, agents: NAMES.code, both: tr("{0} и {1}", NAMES.chat, NAMES.code) });

/** Окно навыка; `save(body)` — Promise записи; отказ — словами в окне. */
function skillDialog(host, { title, skill = null, save }) {
  const name = el('input', { class: 'input', id: 'skill-name', maxlength: String(NAME_MAX), autocomplete: 'off',
    value: skill ? skill.name : '' });
  const description = el('input', { class: 'input', id: 'skill-description', maxlength: String(DESCRIPTION_MAX),
    autocomplete: 'off', value: skill ? skill.description : '' });
  const text = el('textarea', { class: 'input', id: 'skill-text', rows: '10', maxlength: String(TEXT_MAX) });
  text.value = skill ? skill.text : '';
  const count = el('p', { class: 'muted small skill-count', 'aria-live': 'polite' });
  const showCount = () => { count.textContent = tr("{0} из {1} знаков", text.value.length, TEXT_MAX); };
  text.addEventListener('input', showCount);
  showCount();
  const names = scopeNames();
  const scope = el('select', { class: 'input', id: 'skill-scope' },
    ...['chat', 'agents', 'both'].map((id) => el('option', { value: id }, names[id])));
  scope.value = skill ? skill.scope : 'both';
  const quick = el('input', { type: 'checkbox', id: 'skill-quick' });
  const parts = el('input', { type: 'checkbox', id: 'skill-parts' });
  quick.checked = !!skill?.quick;
  parts.checked = !!skill?.parts;
  // кнопка пустого чата — только у навыков «Агентов», «по частям» — только у навыков «Чата»
  const fit = () => {
    quick.disabled = scope.value === 'chat';
    parts.disabled = scope.value === 'agents';
  };
  scope.addEventListener('change', fit);
  fit();
  const fail = el('p', { class: 'error', id: 'skill-status', 'aria-live': 'polite' });
  const ok = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'skill-save' });
  const dialog = openDialog(host, { id: 'skill-dialog', title, actions: [ok], body: [
    field(tr("Название"), name, 'name'), field(tr("Описание"), description, 'description'),
    field(tr("Текст навыка"), text, 'text'), count, field(tr("Где доступен"), scope, 'scope'),
    el('label', { class: 'check' }, quick, tr("Кнопкой в пустом чате «{0}»", NAMES.code)),
    el('label', { class: 'check' }, parts, tr("По частям")),
    el('p', { class: 'muted small' }, tr("«По частям» даёт модели раздела «{0}» инструмент, который выполняет каждую часть задачи отдельным запросом.", NAMES.chat)),
    fail] });
  ok.addEventListener('click', async () => {
    if (ok.disabled) return;
    fail.textContent = '';
    if (!name.value.trim() || !text.value.trim()) {
      fail.textContent = tr("Заполните название и текст навыка");
      return;
    }
    ok.disabled = true;
    try {
      await save({ name: name.value.trim(), description: description.value.trim(), text: text.value, scope: scope.value,
        quick: quick.checked && !quick.disabled, parts: parts.checked && !parts.disabled });
      dialog.close();
    } catch (err) {
      fail.textContent = words(err);
      ok.disabled = false;
    }
  });
  name.focus();
  return dialog;
}

function removeDialog(host, skill, remove) {
  const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'skill-remove-confirm' });
  const fail = el('p', { class: 'error' });
  const dialog = openDialog(host, { id: 'skill-remove', title: tr("Удалить навык?"), actions: [confirm],
    body: [el('p', {}, tr("«{0}» удалится из библиотеки.", skill.name)), fail] });
  confirm.addEventListener('click', async () => {
    confirm.disabled = true;
    try {
      await remove();
      dialog.close();
    } catch (err) {
      fail.textContent = words(err);
      confirm.disabled = false;
    }
  });
}

/** Подпись под именем: где доступен и отметки. */
function meta(skill) {
  return [scopeNames()[skill.scope], ...(skill.quick ? [tr("кнопка в пустом чате")] : []),
    ...(skill.parts ? [tr("по частям")] : [])].join(' · ');
}

/** Список навыков; `base` — адрес API, `query()` — строка запроса списка, `full(item)` — навык с текстом,
 * `order` — есть ли порядок (у умолчаний администратора его нет). */
function skillList(host, { base, query, create, full, order = false, done = () => {}, extra = [] }) {
  const list = el('ul', { class: 'skill-list' });
  const note = el('p', { class: 'status', 'aria-live': 'polite' });
  let items = [];
  const write = async (step) => {
    await step();
    await reload();
    await done();
  };
  async function move(index, by) {
    const ids = items.map((s) => s.id);
    [ids[index], ids[index + by]] = [ids[index + by], ids[index]];
    note.textContent = '';
    try {
      await write(() => api('PUT', `${base}/order`, { ids }));
    } catch (err) {
      note.textContent = words(err);
      await reload();
    }
  }
  async function edit(skill) {
    let whole;
    try {
      whole = await full(skill);
    } catch (err) {
      note.textContent = words(err);
      return;
    }
    skillDialog(host, { title: tr("Изменить навык"), skill: whole,
      save: (body) => write(() => api('PUT', `${base}/${skill.id}`, body)) });
  }
  function row(skill, index) {
    const tools = [];
    if (order) {
      tools.push(button(icon('upward'), { class: 'icon-btn skill-up', 'aria-label': tr("Выше"), title: tr("Выше"),
        disabled: index === 0 }, () => move(index, -1)),
      button(icon('downward'), { class: 'icon-btn skill-down', 'aria-label': tr("Ниже"), title: tr("Ниже"),
        disabled: index === items.length - 1 }, () => move(index, 1)));
    }
    tools.push(button(tr("Изменить"), { class: 'btn btn-sm skill-edit' }, () => edit(skill)),
      button(tr("Удалить"), { class: 'btn btn-sm btn-danger skill-delete' }, () => removeDialog(host, skill,
        () => write(() => api('DELETE', `${base}/${skill.id}`)))));
    return el('li', { class: 'skill-item', 'data-skill': String(skill.id) },
      el('div', { class: 'skill-item-text' }, el('b', { class: 'skill-item-name' }, skill.name),
        skill.description ? el('p', { class: 'small skill-item-description' }, skill.description) : null,
        el('p', { class: 'muted small skill-item-meta' }, meta(skill))),
      el('div', { class: 'row skill-item-tools' }, ...tools));
  }
  async function reload() {
    try {
      items = (await api('GET', `${base}${query()}`)).items;
    } catch (err) {
      note.textContent = words(err);
      return;
    }
    if (!host.isConnected) return;
    list.replaceChildren(...(items.length ? items.map(row) : [el('li', { class: 'muted' }, tr("Навыков пока нет"))]));
  }
  const add = button(tr("Создать навык"), { class: 'btn', id: `${host.id}-add` }, () => skillDialog(host, {
    title: tr("Новый навык"), save: (body) => write(() => api('POST', base, create(body))) }));
  return { list, note, add, reload, write, row: el('div', { class: 'row' }, add, ...extra) };
}

/** «Настройки → Навыки»: библиотека навыков учётки. `changed()` — после каждой записи. */
export function skillsBlock({ changed = () => {} } = {}) {
  const root = el('section', { class: 'block', id: 'skills-settings' });
  const defaults = button(tr("Добавить навыки по умолчанию"), { class: 'btn', id: 'skill-defaults' }, async () => {
    defaults.disabled = true;
    view.note.textContent = '';
    try {
      await view.write(() => api('POST', `/api/skills/defaults?lang=${lang()}`));
    } catch (err) {
      view.note.textContent = words(err);
    } finally {
      defaults.disabled = false;
    }
  });
  const view = skillList(root, { base: '/api/skills', query: () => `?lang=${lang()}`, create: (body) => body,
    full: (skill) => api('GET', `/api/skills/${skill.id}`), order: true, done: changed, extra: [defaults] });
  root.append(el('h2', {}, tr("Навыки")),
    el('p', { class: 'muted' }, tr("Навык — готовое задание, которое вызывается через «/» в поле ввода: его текст уходит в сообщение, а то, что вы допишете, — уточнением. Навыки с отметкой «Кнопкой в пустом чате» показываются в новом чате «{0}» до первого сообщения.", NAMES.code)),
    view.list, view.row, view.note);
  queueMicrotask(view.reload);
  return root;
}

/** Панель администратора: умолчания навыков по языкам. */
export function skillDefaultsBlock() {
  const root = el('section', { class: 'block', id: 'admin-skill-defaults' });
  const choice = el('select', { class: 'input', id: 'skill-default-lang' },
    el('option', { value: 'ru' }, tr("Русский")), el('option', { value: 'en' }, 'English'));
  choice.value = lang();
  const view = skillList(root, { base: '/api/admin/skills', query: () => `?lang=${choice.value}`,
    create: (body) => ({ ...body, lang: choice.value }), full: async (skill) => skill });
  choice.addEventListener('change', view.reload);
  root.append(el('h2', {}, tr("Навыки по умолчанию")),
    el('p', { class: 'muted' }, tr("Новые учётки получают навыки своего языка при первом открытии навыков; «Добавить навыки по умолчанию» в настройках возвращает недостающие. Уже созданные навыки пользователей правка не меняет.")),
    field(tr("Язык"), choice, 'lang'), view.list, view.row, view.note);
  queueMicrotask(view.reload);
  return root;
}
