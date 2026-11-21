import { language, tr } from './i18n.js';
/* Роли диалога (roles.py, спека 2026-11-21-studio-source-foundation-design §3): именованный текст, который чат
 * ставит в системное сообщение.
 *
 * `roleChooser` — чип `#role-chip` с меню `#role-menu` (menu.js) в `.composer-opts` чата: роли учётки, «Без роли»,
 * «Настроить роли». Выбор отдаёт `choose(role | null)` — раздел сам пишет его в диалог (PATCH {role_id}) или держит
 * черновиком нового диалога. Библиотека читается при сборке раздела: первое чтение засевает её умолчаниями языка
 * страницы (`lang`).
 *
 * `rolesBlock` — «Настройки → Чат», блок `#chat-roles-settings`: список, создать, изменить имя и текст, удалить,
 * «Добавить роли по умолчанию». `roleDefaultsBlock` — панель администратора, блок `#admin-role-defaults`: умолчания по
 * языкам ru и en; меняют только посев новых учёток и «Добавить роли по умолчанию». */
import { api } from './api.js';
import { icon } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { button, el, field, openDialog, words } from './ui.js';

const NAME_MAX = 60;     // как roles.NAME_MAX
const TEXT_MAX = 8000;   // как roles.TEXT_MAX
const SETTINGS = '#/settings/chat';

const lang = () => (language() === 'en' ? 'en' : 'ru');

/** Чип роли диалога. `current()` — {id, name} | null; `choose(role | null)` — Promise выбора. */
export function roleChooser({ current, choose, failed = () => {} }) {
  let roles = [];
  const name = el('span', { class: 'chip-name' });
  const chip = el('button', { type: 'button', id: 'role-chip', class: 'chip ctl-chip role-chip', title: tr("Роль диалога") },
    name, icon('down', 'ico caret'));
  const menu = el('div', { id: 'role-menu', class: 'role-menu', role: 'menu', 'aria-label': tr("Роль диалога") });
  const popup = popupMenu(chip, menu, { side: 'above', fill: (close) => {
    const chosen = current();
    const pick = (role) => async () => {
      close(true);
      try {
        await choose(role);
      } catch (err) {
        failed(err);
      }
      paint();
    };
    const items = [
      ...roles.map((role) => menuItem(role.name, { radio: true, checked: chosen?.id === role.id,
        attrs: { class: 'role-option', 'data-role': role.id }, onSelect: pick(role) })),
      menuItem(tr("Без роли"), { radio: true, checked: !chosen, attrs: { class: 'role-none' }, onSelect: pick(null) }),
      menuItem(tr("Настроить роли"), { attrs: { class: 'role-configure' }, onSelect: () => {
        close();
        location.hash = SETTINGS;
      } })];
    return [items, items];
  } });

  function paint() {
    const role = current();
    name.textContent = role ? role.name : tr("Роль");
    chip.setAttribute('aria-label', role ? tr("Роль диалога: {0}", role.name) : tr("Роль диалога: без роли"));
    chip.setAttribute('aria-pressed', String(!!role));
  }

  async function reload() {
    roles = (await api('GET', `/api/roles?lang=${lang()}`)).items;
  }

  paint();
  return { chip, menu, paint, reload, close: () => popup.close(), find: (id) => roles.find((r) => r.id === id) || null };
}

/** Окно роли: имя и текст; `save({name, text})` — Promise; отказ — словами в окне. */
function roleDialog(host, { title, role = null, save }) {
  const name = el('input', { class: 'input', id: 'role-name', maxlength: String(NAME_MAX), autocomplete: 'off',
    value: role ? role.name : '' });
  const text = el('textarea', { class: 'input', id: 'role-text', rows: '8', maxlength: String(TEXT_MAX) });
  text.value = role ? role.text : '';
  const count = el('p', { class: 'muted small role-count', 'aria-live': 'polite' });
  const showCount = () => { count.textContent = tr("{0} из {1} знаков", text.value.length, TEXT_MAX); };
  text.addEventListener('input', showCount);
  showCount();
  const fail = el('p', { class: 'error', id: 'role-status', 'aria-live': 'polite' });
  const ok = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'role-save' });
  const dialog = openDialog(host, { id: 'role-dialog', title, actions: [ok],
    body: [field(tr("Название"), name, 'name'), field(tr("Текст роли"), text, 'text'), count, fail] });
  ok.addEventListener('click', async () => {
    if (ok.disabled) return;
    fail.textContent = '';
    if (!name.value.trim() || !text.value.trim()) {
      fail.textContent = tr("Заполните название и текст роли");
      return;
    }
    ok.disabled = true;
    try {
      await save({ name: name.value.trim(), text: text.value });
      dialog.close();
    } catch (err) {
      fail.textContent = words(err);
      ok.disabled = false;
    }
  });
  name.focus();
  return dialog;
}

function removeDialog(host, role, remove) {
  const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'role-remove-confirm' });
  const fail = el('p', { class: 'error' });
  const dialog = openDialog(host, { id: 'role-remove', title: tr("Удалить роль?"), actions: [confirm],
    body: [el('p', {}, tr("«{0}» удалится; диалоги с ней останутся без роли.", role.name)), fail] });
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

/** Список ролей с «Изменить» и «Удалить»; `base` — адрес API ролей, `query()` — строка запроса списка. */
function roleList(host, { base, query, create, extra = [] }) {
  const list = el('ul', { class: 'role-list' });
  const note = el('p', { class: 'status', 'aria-live': 'polite' });
  async function reload() {
    let items;
    try {
      items = (await api('GET', `${base}${query()}`)).items;
    } catch (err) {
      note.textContent = words(err);
      return;
    }
    if (!host.isConnected) return;
    list.replaceChildren(...(items.length ? items.map((role) => el('li', { class: 'role-item', 'data-role': String(role.id) },
      el('div', { class: 'role-item-text' }, el('b', { class: 'role-item-name' }, role.name),
        el('p', { class: 'muted small role-item-preview' }, role.text)),
      el('div', { class: 'row' },
        button(tr("Изменить"), { class: 'btn btn-sm role-edit' }, () => roleDialog(host, { title: tr("Изменить роль"), role,
          save: async (body) => { await api('PATCH', `${base}/${role.id}`, body); await reload(); } })),
        button(tr("Удалить"), { class: 'btn btn-sm btn-danger role-delete' }, () => removeDialog(host, role, async () => {
          await api('DELETE', `${base}/${role.id}`);
          await reload();
        }))))) : [el('li', { class: 'muted' }, tr("Ролей пока нет"))]));
  }
  const add = button(tr("Создать роль"), { class: 'btn', id: `${host.id}-add` }, () => roleDialog(host, {
    title: tr("Новая роль"), save: async (body) => { await api('POST', base, create(body)); await reload(); } }));
  return { list, note, add, reload, row: el('div', { class: 'row' }, add, ...extra) };
}

/** «Настройки → Чат»: библиотека ролей учётки. */
export function rolesBlock() {
  const root = el('section', { class: 'block', id: 'chat-roles-settings' });
  const defaults = button(tr("Добавить роли по умолчанию"), { class: 'btn', id: 'role-defaults' }, async () => {
    defaults.disabled = true;
    try {
      await api('POST', `/api/roles/defaults?lang=${lang()}`);
      await view.reload();
      view.note.textContent = '';
    } catch (err) {
      view.note.textContent = words(err);
    } finally {
      defaults.disabled = false;
    }
  });
  const view = roleList(root, { base: '/api/roles', query: () => `?lang=${lang()}`, create: (body) => body, extra: [defaults] });
  root.append(el('h2', {}, tr("Роли")),
    el('p', { class: 'muted' }, tr("Роль — текст, который модель получает в начале диалога. Выбирается в чате кнопкой рядом с полем ввода.")),
    view.list, view.row, view.note);
  queueMicrotask(view.reload);
  return root;
}

/** Панель администратора: умолчания ролей по языкам. */
export function roleDefaultsBlock() {
  const root = el('section', { class: 'block', id: 'admin-role-defaults' });
  const choice = el('select', { class: 'input', id: 'role-default-lang' },
    el('option', { value: 'ru' }, tr("Русский")), el('option', { value: 'en' }, 'English'));
  choice.value = lang();
  const view = roleList(root, { base: '/api/admin/roles', query: () => `?lang=${choice.value}`,
    create: (body) => ({ ...body, lang: choice.value }) });
  choice.addEventListener('change', view.reload);
  root.append(el('h2', {}, tr("Роли по умолчанию")),
    el('p', { class: 'muted' }, tr("Новые учётки получают роли своего языка при первом открытии чата; «Добавить роли по умолчанию» в настройках возвращает недостающие. Уже созданные роли пользователей правка не меняет.")),
    field(tr("Язык"), choice, 'lang'), view.list, view.row, view.note);
  queueMicrotask(view.reload);
  return root;
}
