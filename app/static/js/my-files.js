import { tr } from './i18n.js';
/* «Мои файлы» (#/myfiles, меню аватара; my_files.py): личная папка учётки на серверах MCP с контрактом `studio/files`.
 * Вкладка на сервер (GET /api/my-files/servers), папки и путь, загрузка кусками (PUT .../upload: кусок — `chunk` сервера,
 * не больше my_files.CHUNK; отказ связи — повтор куска, 409 `gap` — продолжить с принятого `size`), скачивание и ZIP
 * по адресу с ticket (докачка браузером — Range), удаление — в корзину сервера, «Корзина сервера» с «Вернуть».
 * «Ссылка на сутки» — POST .../links: адрес `/f#<token>` показывается один раз (в базе — только хеш); «Мои ссылки» —
 * действующие ссылки с «Отозвать». Каждый видит только свою папку, администратор — тоже свою. */
import { api } from './api.js';
import { sectionName } from './sections-meta.js';
import { sizeText } from './settings.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, words } from './ui.js';

const RETRIES = 3;                           // попыток одного куска при обрыве связи
const RETRY_STATUS = [0, 502, 503, 504];
let controller = null;

const moment = (ts) => dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
const join = (path, name) => (path ? `${path}/${name}` : name);

/** Скачивание по адресу службы: ответ — вложением, страница остаётся на месте. */
function fetchFile(url, name) {
  const link = el('a', { href: url, download: name, rel: 'noopener', hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
}

export function mount(view) {
  controller = new AbortController();
  const { signal } = controller;
  const host = view;
  const state = { servers: [], sid: null, path: '', trash: false };
  const note = el('p', { id: 'myfiles-status', class: 'status', role: 'status', 'aria-live': 'polite' });
  const tabs = el('div', { class: 'row myfiles-tabs', id: 'myfiles-tabs', role: 'tablist', 'aria-label': tr("Серверы MCP") });
  const crumbs = el('nav', { class: 'row myfiles-path', id: 'myfiles-path', 'aria-label': tr("Путь") });
  const picker = el('input', { type: 'file', multiple: true, hidden: true, id: 'myfiles-picker' });
  const toolbar = el('div', { class: 'row myfiles-actions' },
    button(tr("Загрузить"), { class: 'btn btn-accent', id: 'myfiles-upload' }, () => picker.click()),
    button(tr("Новая папка"), { class: 'btn', id: 'myfiles-mkdir' }, () => mkdirDialog()),
    button(tr("ZIP папки"), { class: 'btn', id: 'myfiles-zip' }, () => zip(state.path)),
    button(tr("Корзина сервера"), { class: 'btn', id: 'myfiles-trash', 'aria-pressed': 'false' }, () => {
      state.trash = !state.trash;
      load();
    }), picker);
  const list = el('ul', { class: 'agents myfiles-list', id: 'myfiles-list' });
  const fresh = el('div', { class: 'myfiles-fresh', id: 'myfiles-link', hidden: true });
  const links = el('ul', { class: 'agents', id: 'myfiles-links' });
  const browser = el('div', { id: 'myfiles-browser', hidden: true }, tabs, crumbs, toolbar, fresh, list,
    el('h2', {}, tr("Мои ссылки")),
    el('p', { class: 'muted small' }, tr("Ссылка открывает один файл без входа в Студию и действует сутки.")), links);
  view.append(el('header', { class: 'head' }, el('h1', {}, sectionName('myfiles'))),
    el('div', { class: 'page myfiles-page' }, note, browser));

  const say = (text, kind) => {
    note.textContent = text;
    note.classList.toggle('error', kind === 'error');
    note.classList.toggle('ok', kind === 'ok');
  };
  const base = () => `/api/my-files/${state.sid}`;
  const server = () => state.servers.find((s) => s.sid === state.sid);

  async function act(work, done) {
    say('');
    try {
      await work();
      if (done) say(done, 'ok');
    } catch (err) {
      if (!signal.aborted) say(words(err), 'error');
    }
  }

  function paintTabs() {
    tabs.replaceChildren(...state.servers.map((s) => {
      const tab = button(s.name, { class: `btn btn-sm${s.sid === state.sid ? ' btn-accent' : ''}`, role: 'tab',
        'aria-selected': String(s.sid === state.sid), 'data-sid': String(s.sid) }, () => {
        state.sid = s.sid;
        state.path = '';
        state.trash = false;
        load();
      });
      return tab;
    }));
    tabs.hidden = state.servers.length < 2;
  }

  function paintCrumbs() {
    const parts = state.path ? state.path.split('/') : [];
    const items = [button(server()?.name || tr("Мои файлы"), { class: 'btn-link', 'data-path': '' }, () => open(''))];
    parts.forEach((part, i) => {
      const target = parts.slice(0, i + 1).join('/');
      items.push(el('span', { class: 'muted', 'aria-hidden': 'true' }, ' / '),
        button(part, { class: 'btn-link', 'data-path': target }, () => open(target)));
    });
    crumbs.replaceChildren(...items);
  }

  function open(path) {
    state.path = path;
    state.trash = false;
    load();
  }

  function entryRow(entry) {
    const path = join(state.path, entry.name);
    const actions = el('div', { class: 'session-actions' });
    const facts = [entry.dir ? tr("папка") : null, sizeText(entry.size), entry.mtime ? moment(entry.mtime) : null];
    if (entry.dir) {
      actions.append(button(tr("ZIP"), { class: 'btn btn-sm', 'aria-label': tr("ZIP: {0}", entry.name) }, () => zip(path)));
    } else {
      actions.append(
        button(tr("Скачать"), { class: 'btn btn-sm myfiles-download', 'aria-label': tr("Скачать: {0}", entry.name) },
          () => act(async () => fetchFile((await api('POST', `${base()}/ticket`, { path }, { signal })).url, entry.name))),
        button(tr("Ссылка на сутки"), { class: 'btn btn-sm myfiles-share', 'aria-label': tr("Ссылка на сутки: {0}", entry.name) },
          () => act(() => share(path, entry.name))));
    }
    actions.append(button(tr("Удалить"), { class: 'btn btn-sm btn-danger', 'aria-label': tr("Удалить: {0}", entry.name) },
      () => act(async () => {
        await api('POST', `${base()}/delete`, { path }, { signal });
        await load();
      }, tr("В корзине сервера: {0}", entry.name))));
    const title = entry.dir
      ? button(entry.name, { class: 'btn-link myfiles-folder' }, () => open(path))
      : el('b', {}, entry.name);
    return el('li', { class: 'agent session myfiles-item', 'data-name': entry.name, 'data-dir': String(entry.dir) },
      el('div', { class: 'session-info' }, title, el('p', { class: 'muted small' }, facts.filter(Boolean).join(' · '))),
      actions);
  }

  function trashRow(item) {
    return el('li', { class: 'agent session myfiles-trash-item', 'data-id': item.id },
      el('div', { class: 'session-info' }, el('b', {}, item.path),
        el('p', { class: 'muted small' }, [item.deleted ? tr("удалено {0}", moment(item.deleted)) : null,
          sizeText(item.size)].filter(Boolean).join(' · '))),
      el('div', { class: 'session-actions' }, button(tr("Вернуть"), { class: 'btn btn-sm', 'aria-label': tr("Вернуть: {0}", item.path) },
        () => act(async () => {
          const back = await api('POST', `${base()}/restore`, { id: item.id }, { signal });
          await load();
          say(tr("Возвращено: {0}", back.path), 'ok');
        }))));
  }

  async function load() {
    paintTabs();
    paintCrumbs();
    const toggle = document.getElementById('myfiles-trash');
    toggle.setAttribute('aria-pressed', String(state.trash));
    toggle.classList.toggle('btn-accent', state.trash);
    let items;
    try {
      items = state.trash
        ? await api('POST', `${base()}/trash`, {}, { signal })
        : (await api('POST', `${base()}/list`, { path: state.path }, { signal })).entries;
    } catch (err) {
      if (signal.aborted) return;
      say(words(err), 'error');
      list.replaceChildren();
      return;
    }
    if (signal.aborted) return;
    list.replaceChildren(...(items.length ? items.map(state.trash ? trashRow : entryRow)
      : [el('li', { class: 'muted', id: 'myfiles-none' }, state.trash ? tr("Корзина сервера пуста") : tr("Папка пуста"))]));
  }

  async function loadLinks() {
    let items;
    try {
      items = await api('GET', '/api/my-files/links', undefined, { signal });
    } catch (err) {
      if (!signal.aborted) say(words(err), 'error');
      return;
    }
    if (signal.aborted) return;
    links.replaceChildren(...(items.length ? items.map((item) => el('li', { class: 'agent session myfiles-link-item', 'data-id': item.id },
      el('div', { class: 'session-info' }, el('b', {}, item.name),
        el('p', { class: 'muted small' }, [item.server, tr("действует до {0}", moment(item.expires))].join(' · '))),
      el('div', { class: 'session-actions' }, button(tr("Отозвать"), { class: 'btn btn-sm btn-danger', 'aria-label': tr("Отозвать: {0}", item.name) },
        () => act(async () => {
          await api('DELETE', `/api/my-files/links/${encodeURIComponent(item.id)}`, undefined, { signal });
          fresh.hidden = true;
          await loadLinks();
        }, tr("Ссылка отозвана: {0}", item.name))))))
      : [el('li', { class: 'muted', id: 'myfiles-links-none' }, tr("Действующих ссылок нет"))]));
  }

  async function share(path, name) {
    const made = await api('POST', `${base()}/links`, { path }, { signal });
    const input = el('input', { class: 'input', id: 'myfiles-link-url', readonly: true, value: made.url });
    const copy = button(tr("Копировать"), { class: 'btn btn-sm', id: 'myfiles-link-copy' }, async () => {
      input.select();
      try {
        await navigator.clipboard.writeText(made.url);
        say(tr("Ссылка скопирована"), 'ok');
      } catch {
        say(tr("Скопируйте ссылку из поля"), 'error');
      }
    });
    fresh.replaceChildren(el('p', {}, tr("Ссылка на «{0}» действует до {1}. Она видна только сейчас — скопируйте её.", name, moment(made.expires))),
      el('p', { class: 'muted small', id: 'myfiles-link-note' }, tr("Ссылка ведёт к этому файлу: если его заменить или изменить, она перестанет открываться — тогда создайте новую.")),
      el('div', { class: 'row' }, input, copy));
    fresh.hidden = false;
    await loadLinks();
  }

  async function zip(path) {
    await act(async () => {
      say(tr("Собираю ZIP…"));
      const made = await api('POST', `${base()}/zip`, { path }, { signal });
      fetchFile(made.url, `${path.split('/').pop() || 'files'}.zip`);
      say(tr("ZIP готов: {0}", sizeText(made.size)), 'ok');
    });
  }

  async function uploadOne(file) {
    const { chunk, max_upload: limit } = server();
    if (file.size > limit) throw new Error(tr("Файл «{0}» больше предела сервера ({1})", file.name, sizeText(limit)));
    const path = join(state.path, file.name);
    const send = (body, range) => api('PUT', `${base()}/upload`, body, { signal, headers: {
      'content-type': 'application/octet-stream', 'x-studio-name': encodeURIComponent(path), 'content-range': range } });
    if (!file.size) {
      await send(new Blob([]), 'bytes */0');
      return;
    }
    let start = 0;
    while (start < file.size) {
      const end = Math.min(start + chunk, file.size) - 1;
      for (let tries = 1; ; tries += 1) {
        try {
          const reply = await send(file.slice(start, end + 1), `bytes ${start}-${end}/${file.size}`);
          if (!reply.done && reply.size <= start) throw new Error(tr("Сервер MCP не принял кусок"));
          start = reply.done ? file.size : reply.size;
          break;
        } catch (err) {
          if (signal.aborted) throw err;
          if (err.code === 'gap' && Number.isInteger(err.detail?.size)) {
            start = err.detail.size;                // сервер принял меньше — продолжить с его места
            break;
          }
          if (tries >= RETRIES || !(RETRY_STATUS.includes(err.status) || err.name === 'TimeoutError')) throw err;
        }
      }
      say(tr("Загрузка «{0}»: {1}%", file.name, Math.floor((start * 100) / file.size)));
    }
  }

  picker.addEventListener('change', async () => {
    const files = [...picker.files];
    picker.value = '';
    for (const file of files) {
      try {
        await uploadOne(file);
      } catch (err) {
        if (!signal.aborted) say(words(err), 'error');
        await load();
        return;
      }
    }
    if (!files.length || signal.aborted) return;
    say(tr("Загружено: {0}", files.map((f) => f.name).join(', ')), 'ok');
    await load();
  });

  function mkdirDialog() {
    const input = el('input', { class: 'input', id: 'myfiles-folder-name', maxlength: '255', autocomplete: 'off' });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const yes = button(tr("Создать"), { class: 'btn btn-accent solid', id: 'myfiles-folder-create' });
    const dialog = openDialog(host, { id: 'myfiles-folder-dialog', title: tr("Новая папка"), actions: [yes],
      body: [field(tr("Имя папки"), input, 'name'), fail] });
    const submit = async () => {
      const name = input.value.trim();
      if (!name || name.includes('/') || name === '.' || name === '..') {
        fail.textContent = tr("Введите имя папки без «/»");
        return;
      }
      yes.disabled = true;
      try {
        await api('POST', `${base()}/mkdir`, { path: join(state.path, name) }, { signal });
        dialog.close();
        await load();
      } catch (err) {
        if (signal.aborted) return;
        fail.textContent = words(err);
        yes.disabled = false;
      }
    };
    yes.addEventListener('click', submit);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    });
    input.focus();
  }

  (async () => {
    try {
      state.servers = await api('GET', '/api/my-files/servers', undefined, { signal });
    } catch (err) {
      if (!signal.aborted) say(words(err), 'error');
      return;
    }
    if (signal.aborted) return;
    if (!state.servers.length) {
      say(tr("Нет серверов MCP с «Моими файлами». Их подключают в «Настройки → Чат → MCP»."));
      return;
    }
    state.sid = state.servers[0].sid;
    browser.hidden = false;
    await Promise.all([load(), loadLinks()]);
  })();
}

export function unmount() {
  controller?.abort();
  controller = null;
}
