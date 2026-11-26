import { api } from './api.js';
import { icon } from './icons.js';
import { tr } from './i18n.js';
import { popupPanel } from './menu.js';
import { dialogDateTime } from './time.js';
import { button, el, field, openDialog, words } from './ui.js';

export const PROJECTS_API = '/api/chat-projects';
export const projectName = (item) => item.inbox ? tr("Без проекта") : item.name;
const roles = [['viewer', "Наблюдатель"], ['editor', "Редактор"]];

function roleSelect(role, label) {
  const select = el('select', { class: 'input project-member-role', 'aria-label': label },
    ...roles.map(([value, name]) => el('option', { value }, tr(name))));
  select.value = role;
  return select;
}

export function projectAccess(host, project, changed) {
  const base = `${PROJECTS_API}/${project.id}/members`;
  const login = el('input', { class: 'input', name: 'login', maxlength: '254', required: true, autocomplete: 'off',
    autocapitalize: 'none', spellcheck: 'false' });
  const role = roleSelect('viewer', tr("Роль участника"));
  const add = button(tr("Дать доступ"), { class: 'btn btn-accent', type: 'submit' });
  const form = el('form', { class: 'stack' }, field(tr("Логин или email пользователя Студии"), login),
    el('div', { class: 'row' }, role, add));
  const list = el('ul', { class: 'project-members-list', 'aria-label': tr("Участники проекта") });
  const status = el('p', { class: 'muted small', role: 'status' });
  const fail = el('p', { class: 'error', role: 'alert' });
  const dialog = openDialog(host, { id: 'chat-project-access-dialog', title: tr("Доступ · {0}", project.name),
    cancelText: tr("Закрыть"), body: [el('p', { class: 'muted small' }, tr("Наблюдатель читает диалоги и файлы. Редактор также ведёт свои диалоги и меняет файлы проекта.")),
      el('p', { class: 'muted small' }, tr("Доступ распространяется на уже созданные и новые диалоги и файлы. Память, подключения и защищённые диалоги остаются личными.")),
      form, status, fail, list] });

  async function update(work = null, message = '') {
    if (!dialog.open || form.inert) return;
    form.inert = list.inert = true;
    fail.textContent = '';
    status.textContent = work ? tr("Сохраняю…") : tr("Загружаю участников…");
    try {
      if (work) await work();
      if (!dialog.open) {
        if (work) changed(null);
        return;
      }
      const { items } = await api('GET', base);
      if (work || dialog.open) changed(items.length);
      if (!dialog.open) return;
      list.replaceChildren(...items.map((member) => {
        const kind = roleSelect(member.role, tr("Роль · {0}", member.login));
        kind.addEventListener('change', () => update(async () => {
          try {
            await api('PATCH', `${base}/${member.id}`, { role: kind.value });
          } catch (err) {
            kind.value = member.role;
            throw err;
          }
        }, tr("Роль изменена")));
        const remove = button(tr("Отозвать"), { class: 'btn', 'aria-label': tr("Отозвать доступ · {0}", member.login) },
          () => update(() => api('DELETE', `${base}/${member.id}`), tr("Доступ отозван")));
        return el('li', {}, el('span', {}, member.login + (member.disabled ? tr(" · заблокирован") : '')), kind, remove);
      }));
      if (!items.length) list.append(el('li', { class: 'muted' }, tr("Доступ есть только у владельца")));
      status.textContent = message;
      if (work) dialog.querySelector('.dialog-close').focus({ preventScroll: true });
    } catch (err) {
      fail.textContent = words(err);
      status.textContent = '';
    } finally {
      form.inert = list.inert = false;
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const identity = login.value.trim();
    if (identity) update(async () => {
      await api('POST', base, { login: identity, role: role.value });
      login.value = '';
    }, tr("Доступ предоставлен"));
  });
  update();
}

export function projectEditor(host, item, done) {
  const name = el('input', { class: 'input', value: item ? projectName(item) : '', maxlength: '120',
    disabled: item?.inbox || null, required: true });
  const description = el('textarea', { class: 'input', rows: '2', maxlength: '2000' }, item?.description || '');
  const context = el('textarea', { class: 'input', rows: '5', maxlength: '8000' }, item?.context || '');
  const enabled = el('input', { type: 'checkbox', checked: item?.memory_on ?? true });
  const fail = el('p', { class: 'error', role: 'status' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent' });
  const dialog = openDialog(host, { id: 'chat-project-editor', title: item ? tr("Настройки проекта") : tr("Новый проект"),
    body: [field(tr("Название"), name), field(tr("Описание"), description), field(tr("Инструкции для диалогов"), context),
      el('label', { class: 'row' }, enabled, tr("Использовать память проекта")), fail], actions: [save] });
  save.addEventListener('click', async () => {
    if (!name.reportValidity()) return;
    save.disabled = true;
    try {
      const result = await api(item ? 'PUT' : 'POST', item ? `${PROJECTS_API}/${item.id}` : PROJECTS_API,
        { name: name.value, description: description.value, context: context.value, memory_on: enabled.checked });
      dialog.close();
      done(result);
    } catch (err) {
      fail.textContent = words(err);
      save.disabled = false;
    }
  });
}

export function projectMemory(host, item) {
  const base = `${PROJECTS_API}/${item.id}/memory`;
  const list = el('ul', { class: 'project-memory-list' });
  const fail = el('p', { class: 'error', role: 'status' });
  const input = el('textarea', { class: 'input', rows: '2', maxlength: '500' });
  const add = button(tr("Запомнить"), { class: 'btn btn-accent' });
  const dialog = openDialog(host, { id: 'chat-project-memory', title: tr("Память проекта"),
    body: [list, field(tr("Запись памяти"), input), fail], actions: [add] });
  async function refresh() {
    const { items } = await api('GET', base);
    if (!dialog.open) return;
    list.replaceChildren(...items.map(({ id, text }) =>
      el('li', { class: 'row' }, el('span', {}, text),
        button(tr("Удалить"), { class: 'btn btn-sm btn-danger' },
          () => act(() => api('DELETE', `${base}/${id}`))))));
  }
  async function act(work) {
    fail.textContent = '';
    add.disabled = true;
    try {
      await work();
      await refresh();
    } catch (err) {
      fail.textContent = words(err);
    } finally {
      add.disabled = false;
    }
  }
  add.addEventListener('click', () => act(async () => {
    await api('POST', base, { text: input.value });
    input.value = '';
  }));
  act(async () => {});
}

export function chooseProject(host, title, items, action) {
  const select = el('select', { class: 'input' }, ...items.map((item) =>
    el('option', { value: item.id }, projectName(item))));
  const fail = el('p', { class: 'error', role: 'status' });
  const save = button(title, { class: 'btn btn-accent', disabled: !items.length || null });
  const dialog = openDialog(host, { id: 'choose-chat-project', title,
    body: [field(tr("Проект"), select), fail], actions: [save] });
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await action(select.value);
      dialog.close();
    } catch (err) {
      fail.textContent = words(err);
      save.disabled = false;
    }
  });
}

export function projectFilePicker(host, item, pick) {
  const base = `${PROJECTS_API}/${item.id}/files`;
  const nav = el('div', { class: 'row' });
  const list = el('div', { class: 'project-file-picker' });
  const fail = el('p', { class: 'error', role: 'status' });
  const dialog = openDialog(host, { id: 'project-file-picker', title: tr("Файлы проекта"), body: [nav, list, fail] });
  const folders = [];
  let version = 0;

  async function load() {
    const mine = ++version;
    const parent = folders.at(-1)?.id;
    nav.replaceChildren(button(tr("К началу"), { class: 'btn btn-sm', disabled: !parent }, () => {
      folders.length = 0;
      load();
    }), ...folders.map((entry, i) => button(entry.name, { class: 'btn btn-sm' }, () => {
      folders.length = i + 1;
      load();
    })));
    list.replaceChildren();
    fail.textContent = '';
    try {
      const { entries } = await api('GET', parent ? `${base}?parent=${parent}` : base);
      if (!dialog.open || mine !== version) return;
      const choices = entries.filter((entry) => !entry.from_chat);
      list.replaceChildren(...(choices.length ? choices.map((entry) => button(entry.name, { class: 'btn' }, async (event) => {
        if (entry.dir) {
          folders.push(entry);
          load();
          return;
        }
        const control = event.currentTarget;
        control.disabled = true;
        try {
          const response = await fetch(entry.download, { credentials: 'same-origin' });
          if (!response.ok) throw new Error(tr("Не удалось открыть файл"));
          const blob = await response.blob();
          if (!dialog.open || mine !== version) return;
          pick(new File([blob], entry.name, { type: blob.type }));
          dialog.close();
        } catch (err) {
          if (dialog.open && mine === version) fail.textContent = words(err);
        } finally {
          control.disabled = false;
        }
      })) : [el('p', { class: 'muted' }, tr("Папка пуста"))]));
    } catch (err) {
      if (dialog.open && mine === version) fail.textContent = words(err);
    }
  }
  load();
}

export function projectsPanel({ user, select, create }) {
  const rows = el('ul', { class: 'code-projects project-list' });
  const orderKey = `studio-chat-project-sort:${user}`;
  const options = new Map([
    ['modified', tr("По дате изменения")], ['name', tr("По имени")], ['count', tr("По числу открытых вкладок")],
  ]);
  const activityLabels = {
    running: (n) => tr("В работе: {0}", n),
    asking: (n) => tr("Требуют вашего ответа: {0}", n),
    unread: (n) => tr("Завершены, не прочитаны: {0}", n),
  };
  let order = 'modified';
  try {
    const saved = localStorage.getItem(orderKey);
    if (options.has(saved)) order = saved;
  } catch { /* Сортировка доступна и без хранилища. */ }

  const sortText = el('span', { class: 'code-project-sort-text' }, options.get(order));
  const sort = button('', { id: 'chat-project-sort', class: 'input code-project-sort', 'aria-label': tr("Сортировка проектов") });
  sort.append(sortText);
  const sortOptions = el('fieldset', { class: 'code-project-sort-options' }, el('legend', {}, tr("Сортировка")));
  const sortMenu = el('div', { class: 'code-project-sort-menu' }, sortOptions);
  const popup = popupPanel(sort, sortMenu, { label: tr("Сортировка проектов"), side: 'above', followViewport: true });
  for (const [value, text] of options) {
    const radio = el('input', { type: 'radio', name: 'chat-project-order', value, checked: order === value });
    radio.addEventListener('change', () => {
      order = value;
      try { localStorage.setItem(orderKey, value); } catch { /* Хранилище закрыто. */ }
      sortText.textContent = text;
      render();
      popup.close(true);
    });
    sortOptions.append(el('label', { class: 'code-project-sort-option' }, radio, el('span', {}, text)));
  }
  const add = button('', { class: 'btn project-create code-project-create-button', 'aria-label': tr("Новый проект"),
    title: tr("Новый проект"), 'aria-haspopup': 'dialog' }, create);
  add.append(icon('plus'), el('span', { class: 'code-project-create-label' }, tr("Новый проект")));
  const node = el('aside', { id: 'chats', class: 'code-panel', 'aria-label': tr("Проекты") },
    el('div', { class: 'code-panel-body' }, rows), el('div', { class: 'code-panel-footer' }, add, sort, sortMenu));
  let projects = [], current = null, activity = new Map(), tabs = new Map(), drawn = null;

  function render() {
    const updatedOf = (item) => activity.get(item.id)?.updated || item.created || 0;
    const countOf = (item) => tabs.get(item.id) || 0;
    const byName = (a, b) => projectName(a).localeCompare(projectName(b), undefined, { numeric: true });
    const sorted = [...projects].sort((a, b) => Number(b.inbox) - Number(a.inbox)
      || (order === 'count' ? countOf(b) - countOf(a) : 0)
      || (order === 'modified' ? updatedOf(b) - updatedOf(a) : 0) || byName(a, b));
    const signature = JSON.stringify(sorted.map((item) =>
      [item.id, item.name, item.role, item.member_count, item.id === current?.id, activity.get(item.id), countOf(item)]));
    if (signature === drawn) return;
    drawn = signature;
    rows.replaceChildren(...sorted.map((item) => {
      const name = projectName(item);
      const count = countOf(item);
      const updated = updatedOf(item);
      const details = activity.get(item.id);
      const badges = Object.entries(activityLabels).map(([kind, label]) => {
        const total = details?.[kind] || 0;
        return total ? el('span', { class: `code-project-state ${kind}`, title: label(total),
          'aria-label': label(total) }, String(total)) : null;
      });
      const link = el('a', { href: `#/chat?p=${item.id}`, class: 'code-project project-link',
        'aria-current': item.id === current?.id ? 'page' : null, title: name },
      el('span', { class: 'code-project-heading' },
        el('span', { class: 'code-project-title' }, el('span', { class: 'code-project-name' }, name)),
        ...badges, count ? el('span', { class: 'code-project-count', title: tr("Открыто вкладок: {0}", count) }, String(count)) : null,
        item.inbox ? el('span', { class: 'code-project-pin project-inbox-pin', 'aria-hidden': 'true' }, icon('star')) : null),
      updated ? el('span', { class: 'code-project-summary code-project-modified' }, dialogDateTime(updated)) : null,
      item.role || item.member_count ? el('span', { class: 'code-project-shared' }, tr("Общий доступ")) : null);
      link.addEventListener('click', (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        select(item.id);
      });
      return el('li', { 'data-pid': item.id, 'data-inbox': item.inbox ? 'true' : null }, link);
    }));
  }

  return { node, paint(items, selected, chats, openCounts, stateOf) {
    projects = items;
    current = selected;
    tabs = openCounts;
    activity = new Map();
    for (const chat of chats) {
      const stats = activity.get(chat.project_id) || { updated: 0, running: 0, asking: 0, unread: 0 };
      stats.updated = Math.max(stats.updated, chat.updated);
      const state = stateOf(chat);
      if (state) stats[state]++;
      activity.set(chat.project_id, stats);
    }
    render();
  }, close: () => popup.close() };
}
