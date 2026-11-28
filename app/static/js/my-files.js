import { locale, tr } from './i18n.js';
/* «Мои файлы» (#/myfiles): проекты, результаты разделов и личные папки серверов MCP с контрактом `studio/files`.
 * Папки MCP: загрузка кусками (PUT .../upload: кусок — `chunk` сервера,
 * не больше my_files.CHUNK; отказ связи — повтор куска, 409 `gap` — продолжить с принятого `size`), скачивание и ZIP
 * по адресу с ticket (докачка браузером — Range), удаление — в корзину сервера, «Корзина сервера» с «Вернуть».
 * «Ссылка на сутки» — POST .../links: адрес `/f#<token>` показывается один раз (в базе — только хеш); «Мои ссылки» —
 * действующие ссылки с «Отозвать». Каждый видит только свою папку, администратор — тоже свою. */
import { api, unknownOutcome } from './api.js';
import { dropFiles } from './chat-drop.js';
import { chooseProject, projectEditor, projectName, PROJECTS_API } from './chat-projects.js';
import { icon } from './icons.js';
import { sectionName } from './sections-meta.js';
import { sizeText } from './settings.js';
import { dateOnly, dateTime } from './time.js';
import { renderTrash } from './trash.js';
import { button, el, field, openDialog, words } from './ui.js';

const RETRIES = 3;                           // попыток одного куска при обрыве связи
const SELECTION_MAX = 100;
const PROTECTED = tr("Сервер MCP — защищённый источник: ссылкой на его файл поделиться нельзя");   // как my_files.PROTECTED
let controller = null;

const moment = (ts) => dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
const fileDate = (ts) => Number.isFinite(ts)
  ? dateTime(ts, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
const join = (path, name) => (path ? `${path}/${name}` : name);

function folderFacts({ size, files, folders }) {
  return [tr("Размер: {0}", sizeText(size)),
    Number.isSafeInteger(files) ? tr("Файлов: {0}", files.toLocaleString(locale())) : null,
    Number.isSafeInteger(folders) ? tr("Подпапок: {0}", folders.toLocaleString(locale())) : null];
}

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
  const projectsSection = { key: 'projects', name: tr("Проекты"), glyph: 'project' };
  const mediaSection = (section) => ({ sid: 0, key: section, local: true, section, name: sectionName(section), glyph: section });
  const mainSections = [projectsSection, mediaSection('photo'), mediaSection('video')];
  const linksSection = { key: 'links', name: tr("Мои ссылки"), glyph: 'share' };
  const state = { servers: trash ? [studio] : [...mainSections, linksSection],
    sid: trash ? 0 : ctx.query.has('links') ? 'links' : 'projects', project: null, path: '', folders: [], projects: [] };
  const note = el('p', { id: 'myfiles-status', class: 'status', role: 'status', 'aria-live': 'polite' });
  const tabs = el(trash ? 'div' : 'nav', { class: trash ? 'row myfiles-tabs' : 'myfiles-nav', id: 'myfiles-tabs',
    role: trash ? 'tablist' : null, 'aria-label': tr("Хранилища") });
  const crumbs = el('nav', { class: 'row myfiles-path', id: 'myfiles-path', 'aria-label': tr("Путь") });
  const picker = el('input', { type: 'file', multiple: true, hidden: true, id: 'myfiles-picker' });
  const dirPicker = el('input', { type: 'file', webkitdirectory: true, multiple: true, hidden: true, id: 'myfiles-directory' });
  const uploadFiles = button(tr("Загрузить"), { class: 'btn btn-accent', id: 'myfiles-upload' }, () => picker.click());
  const uploadFolder = button(tr("Импортировать папку"), { class: 'btn', id: 'myfiles-import' }, () => dirPicker.click());
  const toolbar = el('div', { class: 'row myfiles-actions' }, uploadFiles, uploadFolder,
    button(tr("Новая папка"), { class: 'btn', id: 'myfiles-mkdir' }, () => mkdirDialog()),
    button(tr("ZIP папки"), { class: 'btn', id: 'myfiles-zip' }, () => zip(state.path)), picker, dirPicker);
  const projectPicker = el('input', { type: 'file', multiple: true, hidden: true, id: 'project-files-picker' });
  const projectDirPicker = el('input', { type: 'file', webkitdirectory: true, multiple: true, hidden: true,
    id: 'project-files-directory' });
  const projectUpload = button(tr("Загрузить"), { class: 'btn btn-accent' }, () => projectPicker.click());
  const projectImport = button(tr("Импортировать папку"), { class: 'btn', id: 'project-files-import' }, () => projectDirPicker.click());
  const projectMkdir = button(tr("Новая папка"), { class: 'btn' }, () => projectFolderDialog());
  const projectToolbar = el('div', { class: 'row myfiles-actions', role: 'group', 'aria-label': tr("Действия"), hidden: true },
    projectUpload, projectImport, projectMkdir,
    button(tr("Скачать ZIP"), { class: 'btn' }, () => {
      const parent = folder();
      fetchFile(`${projectBase()}/zip${parent ? `?parent=${parent}` : ''}`, '');
    }), projectPicker, projectDirPicker);
  const projectSearch = el('input', { type: 'search', class: 'input myfiles-project-search', placeholder: tr("Поиск проектов"),
    'aria-label': tr("Поиск проектов"), hidden: true });
  const searchProjects = iconButton('search', tr("Поиск проектов"), () => {
    projectSearch.hidden = !projectSearch.hidden;
    searchProjects.setAttribute('aria-expanded', String(!projectSearch.hidden));
    if (!projectSearch.hidden) projectSearch.focus();
    else {
      projectSearch.value = '';
      paintProjects();
    }
  });
  searchProjects.setAttribute('aria-expanded', 'false');
  let alphabetical = false;
  const sortProjects = iconButton('sort', tr("Сортировка проектов"), () => {
    alphabetical = !alphabetical;
    sortProjects.setAttribute('aria-pressed', String(alphabetical));
    paintProjects();
  });
  sortProjects.setAttribute('aria-pressed', 'false');
  const createProject = button(tr("Новый проект"), { class: 'btn btn-sm myfiles-new-project', disabled: true }, () => {
    projectEditor(host, null, (item) => {
      if (signal.aborted) return;
      const made = { ...item, role: 'owner' };
      state.projects.push(made);
      openProject(made);
    });
  });
  const projectGrid = el('ul', { class: 'myfiles-project-grid', id: 'myfiles-projects' });
  const projectGallery = el('section', { id: 'myfiles-project-gallery', hidden: trash },
    el('div', { class: 'myfiles-project-head' }, el('h2', {}, tr("Проекты")),
      el('div', { class: 'row' }, searchProjects, sortProjects, createProject)), projectSearch, projectGrid);
  const list = el('ul', { class: 'agents myfiles-list', id: 'myfiles-list' });
  const choices = new Map();
  let selecting = false;
  let working = false;
  const selectCount = el('span', { class: 'small', 'aria-live': 'polite', id: 'myfiles-selected' });
  const selectToggle = button(tr("Выбрать"), { class: 'btn btn-sm', id: 'myfiles-select', 'aria-pressed': 'false' }, () => {
    selecting = !selecting;
    if (!selecting) selectAll(false);
    else paintSelection();
  });
  const chooseAll = button(tr("Выбрать все показанные"), { class: 'btn btn-sm' }, () => selectAll(true));
  const clearSelection = button(tr("Снять выделение"), { class: 'btn btn-sm' }, () => selectAll(false));
  const downloadSelection = button(tr("Скачать ZIP"), { class: 'btn btn-sm', id: 'myfiles-selection-zip' }, () => {
    const items = selected();
    if (state.project) {
      const query = new URLSearchParams(items.map((item) => ['item', item.id]));
      fetchFile(`${projectBase()}/zip?${query}`, '');
    } else {
      batch(async () => {
        const { id } = await api('POST', '/api/media/archives', { media_ids: items.map((item) => item.id) }, { signal });
        fetchFile(`/api/media/archives/${encodeURIComponent(id)}`, '');
        return [];
      });
    }
  });
  const addSelection = button(tr("Добавить в проект"), { class: 'btn btn-sm' }, () => {
    const items = selected();
    chooseProject(host, tr("Добавить в проект"), editableProjects(), (pid) => batch(() => each(items, (entry) =>
      api('POST', `${PROJECTS_API}/${pid}/files/from-section`, { media_id: entry.id, section: entry.section }, { signal }))));
  });
  const deleteSelection = button(tr("Удалить"), { class: 'btn btn-sm btn-danger', id: 'myfiles-selection-delete' }, () => {
    const items = selected();
    const project = Boolean(state.project);
    const url = project ? `${projectBase()}/delete-selection` : `${base()}/delete`;
    const confirm = button(tr("Удалить"), { class: 'btn btn-danger', id: 'myfiles-confirm-selection' });
    const dialog = openDialog(host, { id: 'myfiles-delete-selection', title: tr("Удалить выбранное?"),
      body: [el('p', {}, tr("Выбрано: {0}", items.length)), el('p', {}, project
        ? tr("Папки будут удалены из проекта вместе с содержимым.") : tr("Файлы и папки будут перемещены в корзину сервера."))],
      actions: [confirm] });
    confirm.addEventListener('click', () => {
      dialog.close();
      batch(async () => {
        if (!project) return each(items, (item) => api('POST', url, { path: item.path }, { signal }));
        await api('POST', url, { ids: items.map((item) => item.id) }, { signal });
        return [];
      }, true);
    });
  });
  const batchTools = el('div', { class: 'row', hidden: true }, selectCount, chooseAll, clearSelection,
    downloadSelection, addSelection, deleteSelection);
  const selectionBar = el('div', { class: 'row myfiles-selection', hidden: true }, selectToggle, batchTools);
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
  const browser = el('div', { id: 'myfiles-browser', hidden: !trash }, crumbs, toolbar, projectToolbar, selectionBar,
    fresh, studioTrash, list, more, linksBox);
  const trashLink = el('a', { class: 'btn btn-sm', id: trash ? 'myfiles-back' : 'myfiles-trash',
    href: trash ? '#/myfiles' : '#/myfiles?trash=1' }, icon(trash ? 'prev' : 'trash'),
  trash ? tr("Вернуться к файлам") : tr("Корзина"));
  view.append(el('header', { class: 'head' }, el('h1', {}, sectionName(trash ? 'trash' : 'myfiles'))),
    el('div', { class: `page myfiles-page${trash ? '' : ' myfiles-layout'}` },
      el('div', { class: 'myfiles-switches' }, tabs, trashLink), el('div', { class: 'myfiles-pane' }, note, projectGallery, browser)));

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
  const keyOf = (s) => s.key ?? s.sid;
  const server = () => state.servers.find((s) => keyOf(s) === state.sid);
  const base = () => `/api/my-files/${server()?.sid}`;
  const projectBase = () => `${PROJECTS_API}/${state.project.id}/files`;
  const folder = () => state.folders.at(-1)?.id || null;
  const writable = () => state.project?.role !== 'viewer' && folder() !== 'chats';
  const editableProjects = () => state.projects.filter((project) => project.role !== 'viewer');
  const place = () => JSON.stringify([state.sid, state.project?.id, folder(), state.path]);

  function selected() {
    return [...choices].filter(([input]) => input.checked).map(([, item]) => item);
  }

  function selectAll(checked) {
    let count = selected().length;
    for (const input of choices.keys()) {
      if (!checked) input.checked = false;
      else if (!input.checked && count < SELECTION_MAX) {
        input.checked = true;
        count += 1;
      }
    }
    paintSelection();
  }

  function paintSelection() {
    const count = selected().length;
    selectionBar.hidden = !choices.size;
    selectToggle.setAttribute('aria-pressed', String(selecting));
    selectToggle.disabled = working;
    batchTools.hidden = !selecting;
    selectCount.textContent = tr("Выбрано: {0}", count);
    chooseAll.disabled = working || count >= Math.min(choices.size, SELECTION_MAX);
    chooseAll.title = choices.size > SELECTION_MAX ? tr("Не больше {0} за раз", SELECTION_MAX) : '';
    clearSelection.disabled = working || !count;
    downloadSelection.hidden = !state.project && !server()?.local;
    addSelection.hidden = !server()?.local || !editableProjects().length;
    deleteSelection.hidden = state.project ? !writable() : Boolean(server()?.local);
    for (const action of [downloadSelection, addSelection, deleteSelection]) action.disabled = working || !count;
    for (const input of choices.keys()) {
      input.hidden = !selecting;
      input.disabled = working || (!input.checked && count >= SELECTION_MAX);
      input.closest('li').toggleAttribute('data-selected', input.checked);
    }
  }

  function selectable(row, item) {
    const input = el('input', { type: 'checkbox', class: 'myfiles-checkbox', hidden: true,
      'aria-label': tr("Выбрать: {0}", item.name) });
    input.addEventListener('change', paintSelection);
    row.prepend(input);
    choices.set(input, item);
    return row;
  }

  async function each(items, operation) {
    const failed = [];
    for (const item of items) {
      if (signal.aborted) break;
      try {
        await operation(item);
      } catch (err) {
        failed.push(`${item.name} — ${words(err)}`);
      }
    }
    return failed;
  }

  async function batch(operation, refresh = false) {
    if (working) return;
    const target = place();
    working = true;
    paintSelection();
    say('');
    try {
      const failures = await operation();
      if (signal.aborted || target !== place()) return;
      if (refresh) await load();
      if (target === place()) say(failures.length ? failures.join('\n') : tr("Готово"), failures.length ? 'error' : 'ok');
    } catch (err) {
      if (!signal.aborted && target === place()) say(words(err), 'error');
    } finally {
      working = false;
      paintSelection();
    }
  }

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
      const key = keyOf(s);
      const active = key === state.sid;
      const tab = button(s.name, { class: `btn btn-sm${trash ? (active ? ' btn-accent' : '') : ' myfiles-nav-button'}`,
        role: trash ? 'tab' : null, 'aria-selected': trash ? String(active) : null,
        'aria-current': !trash && active ? 'page' : null, 'data-sid': String(key), 'data-storage': s.sid > 0 ? 'mcp' : null }, () => {
        selectSection(key);
        [...tabs.children].find((item) => item.dataset.sid === String(key))?.focus();
      });
      if (!trash) tab.prepend(icon(s.glyph || 'folder'));
      return tab;
    }));
  }

  function selectSection(key) {
    state.sid = key;
    state.project = null;
    state.path = '';
    state.folders = [];
    say('');
    load();
  }

  function openProject(project) {
    state.sid = 'projects';
    state.project = project;
    state.folders = [];
    say('');
    load();
  }

  function iconButton(glyph, label, action) {
    const control = button('', { class: 'icon-btn', 'aria-label': label, title: label }, action);
    control.append(icon(glyph));
    return control;
  }

  function paintProjects() {
    const lang = locale();
    const query = projectSearch.value.trim().toLocaleLowerCase(lang);
    const byName = (left, right) => projectName(left).localeCompare(projectName(right), lang, { numeric: true });
    const items = state.projects.filter((item) => projectName(item).toLocaleLowerCase(lang).includes(query))
      .sort((left, right) => Number(right.inbox) - Number(left.inbox)
        || (alphabetical ? 0 : right.created - left.created) || byName(left, right));
    projectGrid.replaceChildren(...items.map((item) => {
      const name = projectName(item);
      const card = button('', { class: 'myfiles-project-card', 'data-project': item.id, title: name }, () => openProject(item));
      card.append(el('span', { class: 'myfiles-project-icon' }, icon('project')),
        el('span', { class: 'myfiles-project-caption' }, el('span', { class: 'myfiles-project-name' }, name),
          !item.inbox && item.created ? el('span', { class: 'muted small' }, dateOnly(item.created, { day: 'numeric', month: 'short' })) : null));
      return el('li', {}, card);
    }));
    if (!items.length && !createProject.disabled) projectGrid.append(el('li', { class: 'muted' }, tr("Проекты не найдены")));
  }
  projectSearch.addEventListener('input', paintProjects);

  function paintCrumbs() {
    if (state.project) {
      const names = [projectName(state.project), ...state.folders.map((entry) => entry.name)];
      const root = el('li', {}, button(tr("Проекты"), { class: 'btn-link' }, () => selectSection('projects')));
      const locations = names.map((name, index) => {
        const item = index === names.length - 1
          ? el('span', { class: 'small', 'aria-current': 'location' }, name)
          : button(name, { class: 'btn-link' }, () => {
            state.folders = state.folders.slice(0, index);
            load();
          });
        return el('li', {}, el('span', { class: 'muted', 'aria-hidden': 'true' }, '›'), item);
      });
      crumbs.replaceChildren(el('ol', { class: 'row myfiles-project-path' }, root, ...locations));
      return;
    }
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
    const dates = entry.dir && entry.from_chat ? null : el('span', { class: 'muted small myfiles-dates' },
      el('span', {}, entry.dir ? tr("Создано: {0}", fileDate(entry.created)) : tr("Загружено: {0}", fileDate(entry.created))),
      el('span', {}, tr("Изменено: {0}", fileDate(entry.mtime))));
    info.append(tile, el('span', {}, el('span', { class: 'myfiles-name' }, entry.name),
      el('span', { class: 'muted small myfiles-meta' }, facts.filter(Boolean).join(' · ')), dates));
    return el('div', { class: 'session-info' }, info);
  }

  function studioRow(entry) {
    const name = entry.title || entry.name;
    const transcript = entry.section === 'transcribe' && entry.title;
    const facts = [transcript ? tr("Диалог") : sectionName(entry.section), transcript ? 'JSON' : null, sizeText(entry.size)];
    return el('li', { class: 'agent session myfiles-item', 'data-name': entry.name, 'data-dir': 'false' },
      fileInfo({ ...entry, name }, facts, entry.preview ? () => entry.preview : null),
      el('div', { class: 'session-actions' },
        editableProjects().length && entry.section !== 'chat' && !entry.protected ? button(tr("Добавить в проект"), { class: 'btn btn-sm' }, () =>
          chooseProject(host, tr("Добавить в проект"), editableProjects(), async (pid) => {
            await api('POST', `${PROJECTS_API}/${pid}/files/from-section`, { media_id: entry.id, section: entry.section }, { signal });
            say(tr("Файл добавлен в проект"), 'ok');
          })) : null,
        button(tr("Скачать"), { class: 'btn btn-sm myfiles-download', 'aria-label': tr("Скачать: {0}", name) },
          () => fetchFile(entry.download, entry.name))));
  }

  function projectRow(entry) {
    const name = entry.dir && entry.from_chat ? tr("Файлы диалогов") : entry.name;
    const actions = el('div', { class: 'session-actions' });
    if (!entry.dir) actions.append(button(tr("Скачать"), { class: 'btn btn-sm' }, () => fetchFile(entry.download, name)));
    if (!entry.from_chat && writable()) actions.append(button(tr("Удалить"), { class: 'btn btn-sm btn-danger' }, () => {
      const remove = button(tr("Удалить"), { class: 'btn btn-danger' });
      const dialog = openDialog(host, { id: 'project-file-delete', title: tr("Удалить из проекта?"),
        body: [el('p', {}, name)], actions: [remove] });
      const url = `${projectBase()}/${entry.id}`;
      remove.addEventListener('click', () => {
        dialog.close();
        act(async () => {
          await api('DELETE', url, undefined, { signal });
          await load();
        });
      });
    }));
    return el('li', { class: 'agent session myfiles-item', 'data-name': name, 'data-dir': String(entry.dir) },
      fileInfo({ ...entry, name }, entry.dir ? folderFacts(entry) : [sizeText(entry.size)], entry.preview ? () => entry.preview : null,
        entry.dir ? () => {
          state.folders.push({ id: entry.id, name });
          say('');
          load();
        } : null), actions);
  }

  function projectFolderDialog() {
    const input = el('input', { class: 'input', maxlength: '180', required: true });
    const error = el('p', { class: 'error' });
    const save = button(tr("Создать"), { class: 'btn btn-accent' });
    const dialog = openDialog(host, { id: 'project-file-folder', title: tr("Новая папка"),
      body: [field(tr("Название"), input), error], actions: [save] });
    const url = `${projectBase()}/folders`;
    const parent = folder();
    save.addEventListener('click', async () => {
      if (!input.reportValidity()) return;
      save.disabled = true;
      try {
        await api('POST', url, { name: input.value, parent }, { signal });
        dialog.close();
        await load();
      } catch (err) {
        error.textContent = words(err);
        save.disabled = false;
      }
    });
  }

  function entryRow(entry) {
    const path = join(state.path, entry.name);
    const endpoint = `${base()}/ticket`;
    const actions = el('div', { class: 'session-actions' });
    const facts = entry.dir ? folderFacts(entry) : [sizeText(entry.size)];
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
    const project = state.project;
    const gallery = state.sid === 'projects' && !project;
    const linkList = state.sid === 'links';
    const remote = !local && !project && !gallery && !linkList;
    if (!after) {
      visible.disconnect();
      list.replaceChildren();
      more.hidden = true;
      choices.clear();
      selecting = false;
      paintSelection();
    }
    stopTrash?.();
    stopTrash = null;
    browser.hidden = gallery;
    projectGallery.hidden = !gallery;
    toolbar.hidden = !remote || trash;
    projectToolbar.hidden = !project;
    projectUpload.hidden = projectImport.hidden = projectMkdir.hidden = !writable();
    crumbs.hidden = trash || linkList;
    studioTrash.hidden = !(trash && local);
    list.hidden = linkList || (trash && local);
    linksBox.hidden = !(linkList || (remote && !trash));
    if (!remote) fresh.hidden = true;
    paintTabs();
    if (gallery) {
      paintProjects();
      return;
    }
    if (linkList) return;
    paintCrumbs();
    if (!studioTrash.hidden) {
      stopTrash = renderTrash(studioTrash);
      return;
    }
    let result;
    try {
      result = trash
        ? { entries: await api('POST', `${base()}/trash`, {}, { signal }) }
        : project ? await api('GET', `${projectBase()}?${new URLSearchParams({
          ...(folder() ? { parent: folder() } : {}), ...(after ? { after } : {}) })}`, undefined, { signal })
        : await api('POST', `${base()}/list?${new URLSearchParams({
          ...(after ? { after } : {}), ...(server()?.section ? { section: server().section } : {}) })}`, { path: state.path }, { signal });
    } catch (err) {
      if (!signal.aborted && version === loadVersion) say(words(err), 'error');
      return;
    }
    if (signal.aborted || version !== loadVersion) return;
    const rows = result.entries.map((entry) => {
      const row = project ? projectRow(entry) : local ? studioRow(entry) : trash ? trashRow(entry) : entryRow(entry);
      return trash || entry.from_chat ? row : selectable(row, remote ? { ...entry, path: join(state.path, entry.name) } : entry);
    });
    if (after) list.append(...rows);
    else list.replaceChildren(...(rows.length ? rows
      : [el('li', { class: 'muted', id: 'myfiles-none' }, trash ? tr("Корзина сервера пуста") : tr("Папка пуста"))]));
    next = result.next;
    more.hidden = !next;
    paintSelection();
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

  async function uploadOne(file, path, url, { chunk, max_upload: limit }, target) {
    if (file.size > limit) throw new Error(tr("Файл «{0}» больше предела сервера ({1})", file.name, sizeText(limit)));
    const send = (body, range) => api('PUT', `${url}/upload`, body, { signal, headers: {
      'content-type': 'application/octet-stream', 'x-studio-name': encodeURIComponent(path), 'content-range': range } });
    if (!file.size) {
      await send(new Blob([]), 'bytes */0');
      return;
    }
    let start = 0;
    let corrections = 0;
    while (start < file.size) {
      const end = Math.min(start + chunk, file.size) - 1;
      for (let tries = 1; ; tries += 1) {
        try {
          const reply = await send(file.slice(start, end + 1), `bytes ${start}-${end}/${file.size}`);
          if (!Number.isSafeInteger(reply.size) || reply.size < end + 1 || reply.size > file.size
              || reply.done !== (reply.size === file.size)) throw new Error(tr("Сервер MCP не принял кусок"));
          start = reply.done ? file.size : reply.size;
          corrections = 0;
          break;
        } catch (err) {
          if (signal.aborted) throw err;
          const offset = err.detail?.size;
          if (err.code === 'gap' && Number.isSafeInteger(offset) && offset >= 0 && offset < file.size
              && offset !== start && corrections < RETRIES) {
            start = offset;
            corrections += 1;
            break;
          }
          if (tries >= RETRIES || !unknownOutcome(err)) throw err;
        }
      }
      if (target === place()) say(tr("Загрузка «{0}»: {1}%", file.name, Math.floor((start * 100) / file.size)));
    }
  }

  let uploading = false;
  const uploadButtons = [uploadFiles, uploadFolder, projectUpload, projectImport];
  const canUpload = () => !trash && !uploading && (state.project ? writable() : server()?.sid > 0);
  document.addEventListener('studio-before-auto-refresh', (event) => {
    if (uploading) event.preventDefault();
  }, { signal });
  async function receive(files, project, directory) {
    if (!files.length || !canUpload()) return;
    const target = place();
    const source = server();
    const url = project ? projectBase() : base();
    const root = project ? folder() : state.path;
    const folders = new Map();
    const items = files.map((file) => ({ file, name: directory ? file.webkitRelativePath || file.name : file.name }));
    uploading = true;
    uploadButtons.forEach((control) => { control.disabled = true; });
    say('');
    try {
      const failures = await each(items, async ({ file, name }) => {
        const parts = name.split('/');
        if (directory && parts.length < 2) throw new Error(tr("Браузер не передал путь к файлу в папке"));
        const filename = parts.pop();
        let parent = root;
        let prefix = '';
        for (const part of parts) {
          prefix = join(prefix, part);
          if (!folders.has(prefix)) {
            const path = project ? null : join(parent, part);
            folders.set(prefix, project
              ? api('POST', `${url}/folders`, { name: part, parent, reuse: true }, { signal }).then((made) => made.id)
              : api('POST', `${url}/mkdir`, { path }, { signal }).then(() => path));
          }
          parent = await folders.get(prefix);
        }
        if (target === place()) say(tr("Загружаю {0}…", file.name));
        if (project) {
          await api('POST', `${url}/upload`, file, { signal, headers: { 'x-studio-name': encodeURIComponent(filename),
            ...(Number.isFinite(file.lastModified) ? { 'x-studio-mtime': String(file.lastModified) } : {}),
            ...(parent ? { 'x-studio-folder': parent } : {}) } });
        } else {
          await uploadOne(file, join(parent, filename), url, source, target);
        }
      });
      if (signal.aborted || target !== place()) return;
      await load();
      if (target === place()) say([tr("Загружено файлов: {0}", files.length - failures.length), ...failures].join('\n'),
        failures.length ? 'error' : 'ok');
    } finally {
      uploading = false;
      uploadButtons.forEach((control) => { control.disabled = false; });
    }
  }
  for (const [input, project, directory] of [[picker, false, false], [dirPicker, false, true],
    [projectPicker, true, false], [projectDirPicker, true, true]]) input.addEventListener('change', () => {
    const files = [...input.files];
    input.value = '';
    receive(files, project, directory);
  });
  dropFiles(browser, (files) => receive(files, Boolean(state.project), false), canUpload);

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
      const [available, projects] = await Promise.all([
        api('GET', '/api/my-files/servers?sections=true', undefined, { signal, unlock: !trash }),
        trash ? null : api('GET', PROJECTS_API, undefined, { signal }),
      ]);
      if (signal.aborted) return;
      if (trash) {
        state.servers = [studio, ...available.filter(server => !server.local)];
      } else {
        state.projects = projects.items;
        createProject.disabled = false;
        const extra = available.flatMap((s) => s.local
          ? (s.sections || []).filter((section) => !['photo', 'video'].includes(section)).map(mediaSection) : [s]);
        state.servers = [...mainSections, ...extra, linksSection];
        if (state.sid === 'projects') state.project = state.projects.find((item) => item.id === ctx.query.get('p')) || null;
      }
    } catch (err) {
      if (!signal.aborted && (!trash || err.status !== 423)) say(words(err), 'error');
      return;
    }
    if (trash) {
      paintTabs();
      return;
    }
    await Promise.all([load(), loadLinks()]);
  })();
}

export function unmount() {
  controller?.abort();
  controller = null;
}
