import { tr } from './i18n.js';
/* «Мои файлы» (#/myfiles, кнопка рельса; my_files.py): личная папка учётки на серверах MCP с контрактом `studio/files`.
 * Вкладка на сервер (GET /api/my-files/servers), папки и путь, загрузка кусками (PUT .../upload: кусок — `chunk` сервера,
 * не больше my_files.CHUNK; отказ связи — повтор куска, 409 `gap` — продолжить с принятого `size`), скачивание и ZIP
 * по адресу с ticket (докачка браузером — Range), удаление — в корзину сервера, «Корзина сервера» с «Вернуть».
 * «Ссылка на сутки» — POST .../links: адрес `/f#<token>` показывается один раз (в базе — только хеш); «Мои ссылки» —
 * действующие ссылки с «Отозвать». Каждый видит только свою папку, администратор — тоже свою. */
import { api } from './api.js';
import { icon } from './icons.js';
import { sectionName } from './sections-meta.js';
import { sizeText } from './settings.js';
import { dateTime } from './time.js';
import { renderTrash } from './trash.js';
import { button, el, field, openDialog, words } from './ui.js';

const RETRIES = 3;                           // попыток одного куска при обрыве связи
const RETRY_STATUS = [0, 502, 503, 504];
const PROTECTED = tr("Сервер MCP — защищённый источник: ссылкой на его файл поделиться нельзя");   // как my_files.PROTECTED
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

export function mount(view, ctx) {
  controller = new AbortController();
  const { signal } = controller;
  const host = view;
  const trash = ctx.query.has('trash');
  const studio = { sid: 0, name: tr("Записи Studio"), local: true };
  const state = { servers: trash ? [studio] : [], sid: trash ? 0 : null, path: '' };
  const note = el('p', { id: 'myfiles-status', class: 'status', role: 'status', 'aria-live': 'polite' });
  const tabs = el('div', { class: 'row myfiles-tabs', id: 'myfiles-tabs', role: 'tablist', 'aria-label': tr("Хранилища") });
  const crumbs = el('nav', { class: 'row myfiles-path', id: 'myfiles-path', 'aria-label': tr("Путь") });
  const picker = el('input', { type: 'file', multiple: true, hidden: true, id: 'myfiles-picker' });
  const toolbar = el('div', { class: 'row myfiles-actions' },
    button(tr("Загрузить"), { class: 'btn btn-accent', id: 'myfiles-upload' }, () => picker.click()),
    button(tr("Новая папка"), { class: 'btn', id: 'myfiles-mkdir' }, () => mkdirDialog()),
    button(tr("ZIP папки"), { class: 'btn', id: 'myfiles-zip' }, () => zip(state.path)), picker);
  const list = el('ul', { class: 'agents myfiles-list', id: 'myfiles-list' });
  const more = button(tr("Ещё"), { class: 'btn btn-sm', hidden: true });
  let loadVersion = 0;
  let next = null;
  const fresh = el('div', { class: 'myfiles-fresh', id: 'myfiles-link', hidden: true });
  const links = el('ul', { class: 'agents', id: 'myfiles-links' });
  const studioTrash = el('div', { id: 'myfiles-studio-trash', hidden: true });
  let stopTrash = null;
  signal.addEventListener('abort', () => stopTrash?.(), { once: true });
  const linksBox = el('section', { hidden: trash },
    el('h2', {}, tr("Мои ссылки")),
    el('p', { class: 'muted small' }, tr("Ссылка открывает один файл без входа в Студию и действует сутки.")), links);
  const browser = el('div', { id: 'myfiles-browser', hidden: !trash }, crumbs, toolbar, fresh, studioTrash, list, more, linksBox);
  const trashLink = el('a', { class: 'btn btn-sm', id: trash ? 'myfiles-back' : 'myfiles-trash',
    href: trash ? '#/myfiles' : '#/myfiles?trash=1' }, icon(trash ? 'prev' : 'trash'),
  trash ? tr("Вернуться к файлам") : tr("Корзина"));
  view.append(el('header', { class: 'head' }, el('h1', {}, sectionName(trash ? 'trash' : 'myfiles'))),
    el('div', { class: 'page myfiles-page' }, el('div', { class: 'myfiles-switches' }, tabs, trashLink), note, browser));

  const thumbs = new WeakMap();
  const visible = new IntersectionObserver((entries) => {
    for (const { target, isIntersecting } of entries) {
      if (!isIntersecting) continue;
      visible.unobserve(target);
      const show = thumbs.get(target);
      thumbs.delete(target);
      show?.();
    }
  });
  signal.addEventListener('abort', () => visible.disconnect(), { once: true });

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
        say('');
        load();
        tabs.querySelector(`[data-sid="${s.sid}"]`)?.focus();
      });
      return tab;
    }));
    tabs.hidden = !state.servers.length;
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
    load();
  }

  async function preview(ticket, name) {
    const type = ticket.kind.split('/')[0];
    let content;
    if (type === 'text') {
      const shown = await api('GET', ticket.url, undefined, { signal });
      content = el('pre', {}, shown.text);
    } else if (type === 'audio' || type === 'video') {
      content = el(type, { src: ticket.url, controls: true, playsinline: true, preload: 'metadata' });
    } else {
      content = el('img', { src: ticket.url, alt: name });
    }
    if (signal.aborted) return;
    const body = el('div', { class: 'mcp-result myfiles-preview' }, content);
    if (type === 'pdf') body.prepend(el('p', { class: 'muted small' }, tr("Первая страница PDF")));
    content.addEventListener('error', () => body.replaceChildren(el('p', { class: 'error' }, tr("Не удалось открыть файл"))));
    openDialog(host, { id: 'myfiles-preview', title: name, body: [body], cancelText: tr("Закрыть") });
  }

  async function paintThumbnail(tile, kind, getPreview, local) {
    if (signal.aborted || !tile.isConnected) return;
    try {
      const ticket = await getPreview(false);
      if (signal.aborted || !tile.isConnected) return;
      const video = kind === 'video';
      const visual = video
        ? el('video', { preload: 'metadata', playsinline: true, muted: true, 'aria-hidden': 'true' })
        : el('img', { alt: '', decoding: 'async' });
      visual.addEventListener('error', () => visual.remove(), { once: true });
      visual.addEventListener(video ? 'loadeddata' : 'load', () => tile.classList.add('has-preview'), { once: true });
      if (video) {
        visual.muted = true;
        visual.addEventListener('loadedmetadata', () => {
          if (Number.isFinite(visual.duration)) visual.currentTime = Math.min(0.1, visual.duration / 2);
        }, { once: true });
      }
      tile.append(visual);
      visual.src = local && kind === 'image' ? `${ticket.url}?thumb=1` : ticket.url;
    } catch {
      // Превью в списке необязательно. Значок остаётся, полное открытие покажет причину отказа.
    }
  }

  function fileInfo(entry, facts, getPreview, openFolder = null) {
    const kind = (entry.preview?.kind || entry.preview_kind || '').split('/')[0];
    const glyph = entry.dir ? 'folder' : { image: 'photo', video: 'video', audio: 'speech', pdf: 'doc' }[kind] || 'file';
    const tile = el('span', { class: 'myfiles-thumb', 'aria-hidden': 'true' }, icon(glyph));
    if (getPreview && ['image', 'video', 'pdf'].includes(kind)) {
      thumbs.set(tile, () => paintThumbnail(tile, kind, getPreview, !!entry.preview));
      visible.observe(tile);
    }
    const openFile = getPreview ? () => act(async () => preview(await getPreview(), entry.name)) : null;
    const info = openFolder || openFile
      ? button('', { class: `myfiles-file-info btn-link${openFolder ? ' myfiles-folder' : ''}`,
        title: openFile ? tr("Предпросмотр") : null }, openFolder || openFile)
      : el('div', { class: 'myfiles-file-info' });
    info.append(tile, el('span', {}, el('span', { class: 'myfiles-name' }, entry.name),
      el('span', { class: 'muted small myfiles-meta' }, facts.filter(Boolean).join(' · '))));
    return el('div', { class: 'session-info' }, info);
  }

  function studioRow(entry) {
    const facts = [sectionName(entry.section), sizeText(entry.size), moment(entry.mtime)];
    return el('li', { class: 'agent session myfiles-item', 'data-name': entry.name, 'data-dir': 'false' },
      fileInfo(entry, facts, entry.preview ? () => entry.preview : null),
      el('div', { class: 'session-actions' },
        button(tr("Скачать"), { class: 'btn btn-sm myfiles-download', 'aria-label': tr("Скачать: {0}", entry.name) },
          () => fetchFile(entry.download, entry.name))));
  }

  function entryRow(entry) {
    const path = join(state.path, entry.name);
    const endpoint = `${base()}/ticket`;
    const actions = el('div', { class: 'session-actions' });
    const facts = [entry.dir ? tr("папка") : null, sizeText(entry.size), entry.mtime ? moment(entry.mtime) : null];
    if (entry.dir) {
      actions.append(button(tr("ZIP"), { class: 'btn btn-sm', 'aria-label': tr("ZIP: {0}", entry.name) }, () => zip(path)));
    } else {
      actions.append(
        button(tr("Скачать"), { class: 'btn btn-sm myfiles-download', 'aria-label': tr("Скачать: {0}", entry.name) },
          () => act(async () => fetchFile((await api('POST', `${base()}/ticket`, { path }, { signal })).url, entry.name))),
        // защищённый источник: ссылки на его файл нет (сервер ответил бы 409 `protected`), скачать можно
        button(tr("Ссылка на сутки"), { class: 'btn btn-sm myfiles-share', 'aria-label': tr("Ссылка на сутки: {0}", entry.name),
          disabled: server()?.protected || null, title: server()?.protected ? PROTECTED : null },
          () => act(() => share(path, entry.name))));
    }
    actions.append(button(tr("Удалить"), { class: 'btn btn-sm btn-danger', 'aria-label': tr("Удалить: {0}", entry.name) },
      () => act(async () => {
        await api('POST', `${base()}/delete`, { path }, { signal });
        await load();
      }, tr("В корзине сервера: {0}", entry.name))));
    const getPreview = entry.preview_kind ? (unlock = true) =>
      api('POST', endpoint, { path, preview: true }, { signal, unlock }) : null;
    return el('li', { class: 'agent session myfiles-item', 'data-name': entry.name, 'data-dir': String(entry.dir) },
      fileInfo(entry, facts, getPreview, entry.dir ? () => open(path) : null),
      actions);
  }

  function trashRow(item) {
    return el('li', { class: 'agent session myfiles-trash-item', 'data-id': item.id },
      el('div', { class: 'session-info' }, el('b', {}, item.path),
        el('p', { class: 'muted small' }, [item.deleted ? tr("удалено {0}", moment(item.deleted)) : null,
          sizeText(item.size)].filter(Boolean).join(' · '))),
      el('div', { class: 'session-actions' }, button(tr("Вернуть"), { class: 'btn btn-sm', 'aria-label': tr("Вернуть: {0}", item.path) },
        () => act(async () => {
          const sid = state.sid;
          const back = await api('POST', `${base()}/restore`, { id: item.id }, { signal });
          if (sid !== state.sid) return;
          await load();
          if (sid === state.sid) say(tr("Возвращено: {0}", back.path), 'ok');
        }))));
  }

  async function load(after = null) {
    const version = ++loadVersion;
    const local = Boolean(server()?.local);
    if (!after) {
      visible.disconnect();
      list.replaceChildren();
      more.hidden = true;
    }
    stopTrash?.();
    stopTrash = null;
    toolbar.hidden = local || trash;
    crumbs.hidden = trash;
    studioTrash.hidden = !(trash && local);
    list.hidden = trash && local;
    paintTabs();
    paintCrumbs();
    if (!studioTrash.hidden) {
      stopTrash = renderTrash(studioTrash);
      return;
    }
    let result;
    try {
      result = trash
        ? { entries: await api('POST', `${base()}/trash`, {}, { signal }) }
        : await api('POST', `${base()}/list${after ? `?after=${encodeURIComponent(after)}` : ''}`, { path: state.path }, { signal });
    } catch (err) {
      if (!signal.aborted && version === loadVersion) say(words(err), 'error');
      return;
    }
    if (signal.aborted || version !== loadVersion) return;
    const rows = result.entries.map(local ? studioRow : trash ? trashRow : entryRow);
    if (after) list.append(...rows);
    else list.replaceChildren(...(rows.length ? rows
      : [el('li', { class: 'muted', id: 'myfiles-none' }, trash ? tr("Корзина сервера пуста") : tr("Папка пуста"))]));
    next = result.next;
    more.hidden = !next;
  }

  more.addEventListener('click', async () => {
    more.disabled = true;
    try {
      if (next) await load(next);
    } finally {
      more.disabled = false;
    }
  });

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
    if (trash) load();
    try {
      const available = await api('GET', '/api/my-files/servers', undefined, { signal, unlock: !trash });
      if (signal.aborted) return;
      state.servers = trash ? [studio, ...available.filter(server => !server.local)] : available;
    } catch (err) {
      if (!signal.aborted && (!trash || err.status !== 423)) say(words(err), 'error');
      return;
    }
    if (trash) {
      paintTabs();
      return;
    }
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
