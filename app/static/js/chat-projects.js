import { api } from './api.js';
import { icon } from './icons.js';
import { tr } from './i18n.js';
import { button, el, field, openDialog, words } from './ui.js';

export const PROJECTS_API = '/api/chat-projects';
export const projectName = (item) => item.inbox ? tr("Без проекта") : item.name;

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

export function projectsPanel({ select, create, edit, memory, chats }) {
  const rows = el('div', { class: 'code-projects project-list', role: 'group', 'aria-label': tr("Проекты") });
  const tools = el('div', { class: 'row project-actions' });
  const description = el('p', { class: 'muted small project-description', hidden: true });
  const detail = el('div', { class: 'project-detail' }, description, tools, chats);
  const node = el('div', { class: 'project-panel' },
    button(tr("Новый проект"), { class: 'btn project-create' }, create), rows, detail);
  return { node, paint(items, current) {
    if (!current) node.append(detail);
    rows.replaceChildren(...items.flatMap((item) => {
      const active = item.id === current?.id;
      const link = el('a', { href: `#/chat?p=${item.id}`, class: 'code-project project-link',
        'data-inbox': item.inbox ? 'true' : null,
        'aria-current': active ? 'page' : null }, icon(item.inbox ? 'star' : 'folder'), el('span', {}, projectName(item)));
      link.addEventListener('click', (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        select(item.id);
      });
      return active ? [link, detail] : [link];
    }));
    description.hidden = !current?.description;
    description.textContent = current?.description || '';
    tools.replaceChildren(...(current ? [
      el('a', { class: 'btn btn-sm', href: `#/myfiles?p=${current.id}` }, tr("Файлы проекта")),
      button(tr("Память"), { class: 'btn btn-sm' }, () => memory(current)),
      button(tr("Настройки"), { class: 'btn btn-sm' }, () => edit(current)),
    ] : []));
  } };
}
