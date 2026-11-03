/* Вкладки «Файлы» и «Git» проекта Claude Code (#/code/<aid>/<pid>) — спека 2026-10-28-studio-claude-code-design
 * 6.7, протокол канала §4 (files, read, raw, save, git) и §6 (пути).
 *
 * filesView(host, { aid, pid, name, failed, path, git }) → { open(path), leave(go), dirty(), unmount() }:
 * - путь #code-path (корень — имя проекта, части — к своей папке), дерево #code-files — li[data-path] в порядке
 *   моста (папки сначала); папки раскрываются на месте, Git помечает файлы и их родителей; на узком экране, пока открыт
 *   файл, список папки скрыт (назад ведёт путь), на широком — рядом;
 * - текстовый — #code-editor и «Сохранить» (#code-save): PUT …/file с version открытого — файл изменился на
 *   машине (его мог поправить Claude) — 409 conflict и слова в #code-file-status, на машине ничего не
 *   затирается; textarea приводит концы строк к \n — у файла с \r\n они возвращаются при сохранении;
 *   .md — «Текст» / «Вид» (#code-md-view, markdown.js); binary и too_large — слова, файл не открывается;
 * - .pdf — страница картинкой (#code-pdf img из POST …/pdf, число страниц — X-Studio-Pages), «‹» «›» и
 *   «Страница N из M»; прежний blob URL освобождается при смене страницы, файла и уходе;
 * - open(path) — папка файла и сам файл (ссылка «Открыть файл» шага чата); { dir } — только папка после PIN;
 * - текст правлен и не сохранён, а уходят на другой файл, папку (путь, список) или вкладку (leave(go) из
 *   code.js) — окно «Изменения не сохранены»: «Уйти без сохранения» (правка отброшена) или «Остаться»; уход с
 *   уровня проекта и из раздела ловит code.js (dirty, тот же leave), закрытие вкладки — beforeunload;
 * - «Сохранить» получило 409 closed — правка и её version ждут в памяти модуля (edits, ключ aid/pid/путь), а
 *   failed(err, box, path) уводит к PIN; файл, открытый снова (code.js возвращает к нему после PIN), берёт
 *   правку поверх прочитанного с прежним version: изменил его за это время Claude — «Сохранить» скажет
 *   conflict. В хранилище браузера правка не попадает.
 * gitView(host, { git, failed }) → { unmount() }: общий снимок проекта, #code-git — «Ветка», «впереди на N» / «позади на N»
 * (если есть upstream; нулевые не показываются), изменённые файлы (status, path); пусто — «Изменений нет», 409 not_git — «Не
 * репозиторий git».
 * failed(err, box, path) — общие отказы раздела из code.js (closed — к PIN этого компьютера, с path — назад к
 * этому файлу, с { dir } — к папке; offline — слова).
 * Пути — только в теле запросов (поэтому чтения — POST): ни в адрес, ни в хранилище браузера; текст машины —
 * только textContent и markdown.js. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { fileMark } from './code-git.js';
import { renderMarkdown } from './markdown.js';
import { button, el, field, openDialog, plural, words } from './ui.js';

const SAVED = 'Сохранено';
const CONFLICT = 'Файл изменился на компьютере — откройте его заново, чтобы не затереть правки';
const TOO_LARGE = 'Файл больше 2 МиБ — не открывается';
const BINARY = 'Двоичный файл — не открывается';
const NO_CHANGES = 'Изменений нет';
const NOT_GIT = 'Не репозиторий git';
const FOLDER_ICON = '<path d="M3 6.5h6l2 2h10V19H3z"/>';
const FILE_ICON = '<path d="M6 3h8l4 4v14H6zM14 3v4h4"/>';

function icon(paths) {
  const span = el('span', { class: 'code-file-icon', 'aria-hidden': 'true' });
  span.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${paths}</svg>`;    // константы выше
  return span;
}

function parentOf(path) {
  const cut = path.lastIndexOf('/');
  return cut < 0 ? '' : path.slice(0, cut);
}

function nameOf(path) {
  return path.slice(path.lastIndexOf('/') + 1);
}

function join(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

function fileDate(value) {
  return typeof value === 'number' && Number.isFinite(value) ? dateTime(value,
    { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
}

const FILE_COLUMNS = [
  ['name', 'Имя'], ['ext', 'Расш.'], ['size', 'Размер'], ['files', 'Файлы'], ['folders', 'Папки'],
  ['created', 'Создан'], ['modified', 'Изменён'],
];

/** Правки, чьё сохранение застал 409 closed: `<aid>/<pid>/<путь>` → { text, version }; только память страницы. */
const edits = new Map();

export function filesView(host, { aid, pid, name, failed, path: start = null, git, operations = false }) {
  const url = `/api/code/agents/${aid}/projects/${pid}`;
  let alive = true;
  let dir = '';
  let listGen = 0;        // старый переход по пути не раскрывает папки после нового
  let fileGen = 0;        // так же — прежнего файла
  let blobUrl = null;
  let editing = null;     // открытый текст: { editor, base — текст как прочитан или сохранён, reset() }
  let selected = null;
  let gitFiles = [];
  let gitPaths = null;
  let treeGen = 0;
  const nodes = new Map();
  const directories = new Map();
  const statistics = new Map();
  let statsGen = 0;
  let operating = false;

  const crumbs = el('nav', { id: 'code-path', class: 'code-crumbs code-path', 'aria-label': 'Путь файла' });
  const calculate = button('Просчитать все подпапки', { id: 'code-files-calculate', class: 'btn btn-sm',
    title: 'Размеры и количество файлов и папок во всём проекте, включая вложенные папки' });
  const statsStatus = el('p', { id: 'code-files-stats', class: 'muted small', 'aria-live': 'polite' });
  const list = el('ul', { id: 'code-files', class: 'code-files', role: 'tree', 'aria-label': 'Файлы проекта' });
  const table = el('div', { class: 'code-file-table', tabindex: '0', role: 'region', 'aria-label': 'Сведения о файлах' },
    el('div', { class: 'code-file-columns', 'aria-hidden': 'true' },
      ...FILE_COLUMNS.map(([key, label]) => el('span', { class: `code-file-${key}-column` }, label))), list);
  directories.set('', { group: list, loaded: false, pending: null });
  const listError = el('p', { id: 'code-files-error', class: 'error', 'aria-live': 'polite' });
  const file = el('section', { id: 'code-file', class: 'code-file', hidden: true });
  const create = el('div', { class: 'code-files-create', hidden: !operations },
    button('Новый файл', { id: 'code-new-file', class: 'btn btn-sm' }, () => operationDialog('create_file', dir)),
    button('Новая папка', { id: 'code-new-folder', class: 'btn btn-sm' }, () => operationDialog('create_dir', dir)));
  const view = el('div', { class: 'code-files-view' }, el('div', { class: 'code-files-toolbar' }, crumbs, create, calculate),
    statsStatus, listError, table, file);
  host.replaceChildren(view);

  const contains = (parent, path) => path != null && (path === parent || path.startsWith(`${parent}/`));

  async function refreshTree(operation, path, destination) {
    const remap = (value) => {
      if (!contains(path, value) || operation.startsWith('create_')) return value;
      return operation === 'move' ? destination + value.slice(path.length) : parentOf(path);
    };
    const expanded = [...nodes.values()].filter((node) => node.li.getAttribute('aria-expanded') === 'true')
      .filter((node) => operation !== 'delete' || !contains(path, node.path)).map((node) => remap(node.path));
    const currentDir = remap(dir);
    const movedFile = selected && contains(path, selected) && operation === 'move' ? remap(selected) : null;
    if (operation === 'delete' && contains(path, selected)) closeFile();
    treeGen += 1;
    listGen += 1;
    nodes.clear();
    directories.clear();
    list.replaceChildren();
    directories.set('', { group: list, loaded: false, pending: null });
    clearStats();
    dir = currentDir;
    if (movedFile) openFile(movedFile);
    else showPath(selected || dir, !!selected);
    if (!(await loadDir('')) || !alive) return;
    for (const target of [...expanded, currentDir]) {
      const parts = target.split('/').filter(Boolean);
      for (let i = 1; i <= parts.length; i += 1) {
        const node = nodes.get(parts.slice(0, i).join('/'));
        if (!node?.item.dir) break;
        expand(node, true);
        if (!(await loadDir(node.path)) || !alive) return;
      }
    }
    dir = currentDir;
    showPath(selected || dir, !!selected);
    git.refresh(true);
  }

  function operationDialog(action, path, isDir = false) {
    if (!alive || operating) return;
    const creating = action.startsWith('create_');
    const moving = action === 'rename' || action === 'move';
    const deleting = action === 'delete';
    const title = { create_file: 'Новый файл', create_dir: 'Новая папка', rename: 'Переименовать',
      move: 'Переместить', delete: 'Удалить' }[action];
    const input = el('input', { id: 'code-file-operation-path', class: 'input', maxlength: '1024',
      autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
    input.value = action === 'rename' ? nameOf(path) : moving ? path : '';
    const error = el('p', { id: 'code-file-operation-error', class: 'error', 'aria-live': 'polite' });
    const submit = button(title, { id: 'code-file-operation-submit', class: `btn ${deleting ? 'btn-danger' : 'btn-accent'}` });
    const body = [el('p', { class: 'code-operation-source' }, creating ? `Папка: ${path || name || 'Проект'}` : path)];
    if (deleting) body.push(el('p', {}, isDir ? 'Папка и всё её содержимое будут удалены без возможности отмены.'
      : 'Файл будет удалён без возможности отмены.'));
    else body.push(field(moving && action !== 'rename' ? 'Новый путь от корня проекта, включая имя' : 'Имя', input),
      el('p', { class: 'muted small' }, action === 'rename' ? 'Введите имя без пути.'
        : 'Разделитель папок — /. Родительская папка должна существовать.'));
    body.push(error);
    const dialog = openDialog(host, { id: 'code-file-operation', title, body, actions: [submit] });
    if (!deleting) { input.focus(); if (action === 'rename') input.select(); }
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); submit.click(); }
    });
    submit.addEventListener('click', async () => {
      if (operating || !alive) return;
      const value = input.value;
      if (!deleting && (!value || action === 'rename' && value.includes('/'))) {
        error.textContent = 'Введите корректное имя или путь';
        return;
      }
      if (!creating && contains(path, selected) && dirty()) {
        error.textContent = 'Сначала сохраните изменения открытого файла';
        return;
      }
      const operation = action === 'rename' ? 'move' : action;
      const source = creating ? join(path, value) : path;
      const destination = action === 'rename' ? join(parentOf(path), value) : moving ? value : null;
      operating = true;
      submit.disabled = true;
      dialog.querySelector('.dialog-close').disabled = true;
      const preventCancel = (event) => event.preventDefault();
      dialog.addEventListener('cancel', preventCancel);
      error.textContent = '';
      try {
        await api('POST', `${url}/files/operate`, { operation, path: source, destination });
        dialog.close();
        if (alive) await refreshTree(operation, source, destination);
      } catch (err) {
        if (!alive) return;
        if (err.code === 'closed') { dialog.close(); refuse(err, listError, null); }
        else {
          error.textContent = words(err);
          // При частичном удалении обновить сведения, оставив открытый текст на месте.
          if (deleting) { clearStats(); for (const target of directories.keys()) loadDir(target, true); }
        }
      } finally {
        operating = false;
        submit.disabled = false;
        dialog.querySelector('.dialog-close').disabled = false;
        dialog.removeEventListener('cancel', preventCancel);
      }
    });
  }

  function actionsMenu(path, isDir) {
    if (!operations || operating) return;
    const actions = [['rename', 'Переименовать'], ['move', 'Переместить'], ['delete', 'Удалить']];
    if (isDir) actions.unshift(['create_file', 'Новый файл'], ['create_dir', 'Новая папка']);
    const choices = el('div', { class: 'code-file-actions' });
    const dialog = openDialog(host, { id: 'code-file-actions', title: nameOf(path), body: [choices], cancelText: 'Закрыть' });
    for (const [action, label] of actions) choices.append(button(label, { class: `btn${action === 'delete' ? ' btn-danger' : ''}`,
      'data-action': action }, () => { dialog.close(); operationDialog(action, path, isDir); }));
  }

  function clearStats() {
    statsGen += 1;
    statistics.clear();
    statsStatus.textContent = '';
    for (const node of nodes.values()) decorate(node);
  }

  calculate.addEventListener('click', async () => {
    if (calculate.disabled) return;
    const mine = ++statsGen;
    calculate.disabled = true;
    calculate.textContent = 'Считаю…';
    statsStatus.textContent = 'Подсчёт файлов и папок во всём проекте…';
    try {
      const result = await api('POST', `${url}/files/stats`, { path: '' });
      if (!alive || mine !== statsGen) return;
      statistics.clear();
      for (const item of result.items) statistics.set(item.path, item);
      for (const node of nodes.values()) decorate(node);
      const total = statistics.get('');
      const prefix = result.complete ? 'Всего:' : 'Подсчёт неполный — не менее';
      statsStatus.textContent = `${prefix} ${total.size.toLocaleString('ru-RU')} Б · ${total.files.toLocaleString('ru-RU')} `
        + `${plural(total.files, ['файл', 'файла', 'файлов'])} · ${total.folders.toLocaleString('ru-RU')} `
        + `${plural(total.folders, ['папка', 'папки', 'папок'])}. С учётом вложенных папок.`
        + ` Подсчитано: ${fileDate(result.checked_at)}.`
        + (result.skipped_links ? ` Ссылок пропущено: ${result.skipped_links}.` : '')
        + (!result.complete ? ' Достигнут предел обхода или часть записей недоступна.' : '');
    } catch (err) {
      if (alive && mine === statsGen) {
        statsStatus.textContent = words(err);
        if (err.code === 'closed') refuse(err, statsStatus, null);
      }
    } finally {
      if (alive) {
        calculate.disabled = false;
        calculate.textContent = 'Просчитать все подпапки';
      }
    }
  });

  function refuse(err, box, path) {
    if (!alive) return;
    if (err.code === 'closed') {
      clearStats();
      if (dirty()) editing.keep();
      if (path == null && selected) path = selected;
      treeGen += 1;
      listGen += 1;
      nodes.clear();
      directories.clear();
      list.replaceChildren();
      directories.set('', { group: list, loaded: false, pending: null });
    }
    failed(err, box, path);
  }

  function dropBlob() {
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = null;
  }

  /** Открытый текст правлен и не сохранён. */
  function dirty() {
    return !!editing && editing.editor.value !== editing.base;
  }

  /** Закрытие вкладки и перезагрузка с несохранённой правкой — вопрос браузера. */
  function unload(event) {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = '';
  }

  /** Уйти с открытого текста: правка не сохранена — сначала спросить; «Уйти без сохранения» её отбрасывает. */
  function leave(go) {
    if (!dirty()) {
      go();
      return;
    }
    const yes = button('Уйти без сохранения', { class: 'btn btn-danger', id: 'code-unsaved-leave' });
    const dialog = openDialog(host, { id: 'code-unsaved', title: 'Изменения не сохранены', actions: [yes],
      cancelText: 'Остаться' });
    yes.addEventListener('click', () => {
      dialog.close();
      if (!alive) return;
      if (editing) editing.reset();
      go();
    });
  }

  function showPath(path, isFile) {
    const parts = path ? path.split('/') : [];
    const items = [[name || 'Проект', '']];
    parts.forEach((part, i) => items.push([part, parts.slice(0, i + 1).join('/')]));
    crumbs.replaceChildren();
    items.forEach(([text, target], i) => {
      if (i) crumbs.append(el('span', { class: 'muted', 'aria-hidden': 'true' }, '›'));
      const last = i === items.length - 1;
      if (last && (isFile || target === dir)) crumbs.append(el('span', { class: 'code-crumb-here' }, text));
      else {
        const go = el('button', { type: 'button', class: 'code-path-link' }, text);
        go.addEventListener('click', () => leave(() => {
          closeFile();
          listDir(target);
        }));
        crumbs.append(go);
      }
    });
  }

  function decorate(node) {
    const { path, item, row, badge, li } = node;
    const changes = gitFiles.filter((f) => f.path === path || (item.dir && f.path.startsWith(`${path}/`)));
    const mark = changes.length ? fileMark(changes.find((f) => fileMark(f.status).kind === 'conflict')?.status || changes[0].status) : null;
    row.dataset.git = mark?.kind || '';
    badge.textContent = mark ? item.dir ? '•' : mark.label : '';
    badge.title = mark ? item.dir ? `Изменений в папке: ${changes.length}` : mark.title : '';
    const ext = item.dir || item.name.lastIndexOf('.') <= 0 ? '' : item.name.slice(item.name.lastIndexOf('.') + 1);
    const recursive = item.dir && statistics.get(path);
    const counts = recursive || node.counts;
    const lowerBound = recursive ? !recursive.complete : counts?.truncated;
    const count = (key) => counts ? `${lowerBound ? '≥' : ''}${counts[key].toLocaleString('ru-RU')}` : '—';
    const values = { ext: ext || '—', size: item.dir ? recursive ? count('size') : 'Папка'
      : Number.isFinite(item.size) ? item.size.toLocaleString('ru-RU') : '—',
      files: item.dir ? count('files') : '—', folders: item.dir ? count('folders') : '—',
      created: fileDate(item.created), modified: fileDate(item.modified) };
    for (const [key, cell] of Object.entries(node.cells)) {
      cell.textContent = values[key];
      const label = FILE_COLUMNS.find(([column]) => column === key)[1];
      cell.title = `${label}: ${values[key]}${key === 'size' && (!item.dir || recursive) ? ' Б' : ''}`;
      if (key === 'files' || key === 'folders' || key === 'size' && recursive) cell.title += recursive
        ? ' — включая все вложенные папки' : ' — непосредственно в папке, без вложенных папок; после раскрытия';
    }
    row.title = [path, ...Object.values(node.cells).map((cell) => cell.title), badge.title].filter(Boolean).join('\n');
    li.setAttribute('aria-label', `${item.name}\n${row.title}`);
    li.setAttribute('aria-selected', String(path === selected));
  }

  const unsubscribeGit = git.subscribe(({ data }) => {
    if (!data) {
      gitPaths = null;
    }
    gitFiles = (Array.isArray(data?.files) ? data.files : []).filter((f) => typeof f.path === 'string');
    for (const node of nodes.values()) decorate(node);
    const paths = JSON.stringify(gitFiles.map((f) => f.path).sort());
    if (data && gitPaths !== null && gitPaths !== paths) {
      clearStats();
      for (const [path] of directories) {
        if (!path || nodes.get(path)?.li.getAttribute('aria-expanded') === 'true') loadDir(path, true);
      }
    }
    if (data) gitPaths = paths;
  });

  function expand(node, open) {
    node.li.setAttribute('aria-expanded', String(open));
    const directory = directories.get(node.path);
    directory.group.hidden = !open;
    if (!open) return;
    if (file.hidden) {
      dir = node.path;
      showPath(dir, false);
    }
    loadDir(node.path);
  }

  function treeRow(item, parent) {
    const path = join(parent, item.name);
    const chevron = el('span', { class: 'code-file-chevron', 'aria-hidden': 'true' }, item.dir ? '›' : '');
    const badge = el('span', { class: 'code-file-git', 'aria-hidden': 'true' });
    const cells = Object.fromEntries(FILE_COLUMNS.slice(1).map(([key]) => [key,
      el('span', { class: `code-file-meta code-file-${key}`, 'data-column': key })]));
    const row = el('button', { type: 'button', class: `code-file-item${item.dir ? ' dir' : ''}`,
      style: `--tree-depth: ${path.split('/').length - 1}` }, el('span', { class: 'code-file-label' }, chevron,
      icon(item.dir ? FOLDER_ICON : FILE_ICON), el('span', { class: 'code-file-name' }, item.name), badge), ...Object.values(cells));
    const li = el('li', { 'data-name': item.name, 'data-path': path, role: 'treeitem',
      'aria-label': item.name, class: item.dir ? 'dir' : null }, row);
    const node = { path, item, row, badge, li, cells };
    nodes.set(path, node);
    if (operations) {
      const menu = button('⋯', { class: 'code-file-menu', 'aria-label': `Действия: ${item.name}`,
        title: 'Действия с файлом или папкой', 'aria-haspopup': 'dialog' }, () => actionsMenu(path, node.item.dir));
      li.classList.add('has-actions');
      li.append(menu);
      row.addEventListener('contextmenu', (event) => { event.preventDefault(); actionsMenu(path, node.item.dir); });
      row.addEventListener('keydown', (event) => {
        if (event.key === 'F10' && event.shiftKey) { event.preventDefault(); actionsMenu(path, node.item.dir); }
      });
    }
    if (item.dir) {
      const group = el('ul', { role: 'group', hidden: true });
      li.append(group);
      li.setAttribute('aria-expanded', 'false');
      directories.set(path, { group, loaded: false, pending: null });
    }
    row.addEventListener('click', () => {
      if (item.dir) expand(node, li.getAttribute('aria-expanded') !== 'true');
      else leave(() => openFile(path));
    });
    row.addEventListener('keydown', async (event) => {
      const keys = ['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End'];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      const visible = [...list.querySelectorAll('.code-file-item')].filter((b) => b.getClientRects().length);
      const index = visible.indexOf(row);
      if (event.key === 'ArrowDown') visible[index + 1]?.focus();
      else if (event.key === 'ArrowUp') visible[index - 1]?.focus();
      else if (event.key === 'Home') visible[0]?.focus();
      else if (event.key === 'End') visible.at(-1)?.focus();
      else if (event.key === 'ArrowRight' && item.dir) {
        if (li.getAttribute('aria-expanded') !== 'true') expand(node, true);
        else {
          await loadDir(path);
          if (alive) directories.get(path)?.group.querySelector('.code-file-item')?.focus();
        }
      } else if (event.key === 'ArrowLeft') {
        if (item.dir && li.getAttribute('aria-expanded') === 'true') expand(node, false);
        else nodes.get(parent)?.row.focus();
      }
    });
    decorate(node);
    return li;
  }

  async function loadDir(path, fresh = false) {
    const directory = directories.get(path);
    if (!directory || (directory.loaded && !fresh)) return true;
    if (directory.pending) return directory.pending;
    const { group } = directory;
    const tree = treeGen;
    group.setAttribute('aria-busy', 'true');
    listError.textContent = '';
    directory.pending = (async () => {
      try {
        const { items, truncated } = await api('POST', `${url}/files`, { path });
        if (!alive || tree !== treeGen || directories.get(path) !== directory) return false;
        const active = group.contains(document.activeElement) ? document.activeElement : null;
        const children = items.map((item) => {
          const old = nodes.get(join(path, item.name));
          if (old && old.item.dir !== item.dir) dropNode(old.path);
          if (old && old.item.dir === item.dir) {
            old.item = item;
            decorate(old);
            return old.li;
          }
          return treeRow(item, path);
        });
        const kept = new Set(items.map((item) => join(path, item.name)));
        for (const child of group.children) {
          const removed = child.dataset.path;
          if (!removed || kept.has(removed)) continue;
          dropNode(removed);
        }
        group.replaceChildren(...children);
        const parent = nodes.get(path);
        if (parent) {
          parent.counts = { files: items.filter((item) => !item.dir).length,
            folders: items.filter((item) => item.dir).length, truncated: !!truncated };
          decorate(parent);
        }
        if (active?.isConnected) active.focus({ preventScroll: true });
        if (truncated) group.append(el('li', { class: 'muted code-tree-note' }, 'Показаны первые 5000 записей'));
        if (!items.length) group.append(el('li', { class: 'muted code-tree-note' }, 'Папка пуста'));
        directory.loaded = true;
        return true;
      } catch (err) {
        if (alive && tree === treeGen && directories.get(path) === directory) refuse(err, listError, file.hidden ? { dir: path } : null);
        return false;
      } finally {
        group.removeAttribute('aria-busy');
        directory.pending = null;
      }
    })();
    return directory.pending;
  }

  function dropNode(path) {
    for (const key of nodes.keys()) {
      if (key === path || key.startsWith(`${path}/`)) {
        nodes.delete(key);
        directories.delete(key);
      }
    }
  }

  async function listDir(path) {
    const mine = ++listGen;
    if (!(await loadDir('')) || !alive || mine !== listGen) return;
    const parts = path ? path.split('/') : [];
    for (let i = 0; i < parts.length; i += 1) {
      const target = parts.slice(0, i + 1).join('/');
      const node = nodes.get(target);
      if (!node?.item.dir) return;
      expand(node, true);
      if (!(await loadDir(target)) || !alive || mine !== listGen) return;
    }
    dir = path;
    for (const [target, node] of nodes) {
      if (node.item.dir && target !== path && !path.startsWith(`${target}/`)) {
        node.li.setAttribute('aria-expanded', 'false');
        directories.get(target).group.hidden = true;
      }
    }
    if (file.hidden) showPath(path, false);
  }

  function closeFile() {
    fileGen += 1;
    editing = null;
    selected = null;
    for (const node of nodes.values()) node.li.setAttribute('aria-selected', 'false');
    dropBlob();
    file.hidden = true;
    file.replaceChildren();
    view.classList.remove('has-file');      // узко список папки прячется, пока открыт файл (путь ведёт назад)
  }

  function fileFrame(path) {
    closeFile();
    selected = path;
    for (const node of nodes.values()) node.li.setAttribute('aria-selected', String(node.path === path));
    showPath(path, true);
    const status = el('p', { id: 'code-file-status', class: 'code-file-status', 'aria-live': 'polite' });
    const bar = el('div', { class: 'code-file-bar' });
    if (operations) bar.append(button('⋯', { class: 'btn btn-sm', id: 'code-open-file-actions',
      'aria-label': 'Действия с открытым файлом' }, () => actionsMenu(path, false)));
    file.replaceChildren(bar, status);
    file.hidden = false;
    view.classList.add('has-file');
    return { mine: fileGen, bar, status };
  }

  function say(status, text, error) {
    status.textContent = text;
    status.classList.toggle('error', !!error);
  }

  async function openText(path) {
    const { mine, bar, status } = fileFrame(path);
    let got;
    try {
      got = await api('POST', `${url}/read`, { path });
    } catch (err) {
      if (!alive || mine !== fileGen) return;
      if (err.code === 'binary') say(status, BINARY, true);
      else if (err.code === 'too_large') say(status, TOO_LARGE, true);
      else {
        say(status, '', true);
        refuse(err, status, path);
      }
      return;
    }
    if (!alive || mine !== fileGen) return;
    let version = got.version;
    const crlf = got.text.includes('\r\n');          // textarea отдаст \n — при сохранении вернуть \r\n
    const editor = el('textarea', { id: 'code-editor', class: 'input code-editor', spellcheck: 'false',
      autocapitalize: 'off', autocomplete: 'off', wrap: 'off', 'aria-label': nameOf(path) });
    editor.value = got.text;
    const base = editor.value;
    const key = `${aid}/${pid}/${path}`;
    const kept = edits.get(key);                        // правка, чьё сохранение застал closed
    if (kept) {
      edits.delete(key);
      editor.value = kept.text;
      version = kept.version;                           // правка Claude за это время — conflict, а не затирание
    }
    let shown = null;                                   // разметка .md («Вид»)
    const mark = { editor, base, keep() { edits.set(key, { text: editor.value, version }); }, reset() {
      edits.delete(key);
      editor.value = mark.base;
      if (shown && !shown.hidden) shown.replaceChildren(renderMarkdown(editor.value));
    } };
    editing = mark;
    const save = el('button', { type: 'button', id: 'code-save', class: 'btn btn-accent btn-sm' }, 'Сохранить');
    editor.addEventListener('input', () => say(status, '', false));
    save.addEventListener('click', async () => {
      if (save.disabled) return;
      save.disabled = true;
      say(status, '', false);
      const sent = editor.value;
      const text = crlf ? sent.replace(/\n/g, '\r\n') : sent;
      try {
        ({ version } = await api('PUT', `${url}/file`, { path, text, version }));
        mark.base = sent;
        clearStats();
        if (alive && mine === fileGen) say(status, SAVED, false);
        if (alive) {
          loadDir(parentOf(path), true);
          git.refresh(true);
        }
      } catch (err) {
        if (!alive || mine !== fileGen) return;
        if (err.code === 'conflict') say(status, CONFLICT, true);
        else if (err.code === 'closed') {
          edits.set(key, { text: editor.value, version });
          refuse(err, status, path);
        } else {
          say(status, '', true);
          refuse(err, status);
        }
      } finally {
        save.disabled = false;
      }
    });
    file.append(editor);
    if (/\.md$/i.test(path)) {
      shown = el('div', { id: 'code-md', class: 'md code-md', hidden: true });
      const modes = el('div', { id: 'code-md-view', class: 'code-md-view', role: 'group', 'aria-label': 'Показ' });
      for (const [mode, text] of [['text', 'Текст'], ['view', 'Вид']]) {
        const b = el('button', { type: 'button', class: 'chip', 'data-mode': mode, 'aria-pressed': String(mode === 'text') }, text);
        b.addEventListener('click', () => {
          for (const other of modes.children) other.setAttribute('aria-pressed', String(other === b));
          editor.hidden = mode === 'view';
          shown.hidden = mode !== 'view';
          shown.replaceChildren(...(mode === 'view' ? [renderMarkdown(editor.value)] : []));
        });
        modes.append(b);
      }
      bar.append(modes);
      file.append(shown);
    }
    bar.append(save);
  }

  function openPdf(path) {
    const { mine, bar, status } = fileFrame(path);
    const img = el('img', { alt: nameOf(path) });
    const prev = el('button', { type: 'button', id: 'code-pdf-prev', class: 'btn btn-sm', 'aria-label': 'Предыдущая страница', disabled: true }, '‹');
    const next = el('button', { type: 'button', id: 'code-pdf-next', class: 'btn btn-sm', 'aria-label': 'Следующая страница', disabled: true }, '›');
    const label = el('span', { id: 'code-pdf-page', class: 'code-pdf-page' });
    bar.append(el('div', { class: 'code-pdf-nav' }, prev, label, next));
    file.append(el('div', { id: 'code-pdf', class: 'code-pdf' }, img));
    let page = 1;
    let pages = 0;

    async function show(n) {
      prev.disabled = true;
      next.disabled = true;
      say(status, '', false);
      try {
        const response = await api('POST', `${url}/pdf`, { path, page: n });
        const total = Number(response.headers.get('X-Studio-Pages'));
        const blob = await response.blob();
        if (!alive || mine !== fileGen) return;
        dropBlob();
        blobUrl = URL.createObjectURL(blob);
        img.src = blobUrl;
        page = n;
        pages = Number.isInteger(total) && total > 0 ? total : n;
        label.textContent = `Страница ${page} из ${pages}`;
      } catch (err) {
        if (!alive || mine !== fileGen) return;
        say(status, '', true);
        refuse(err, status, path);
      }
      if (alive && mine === fileGen) {
        prev.disabled = !pages || page <= 1;
        next.disabled = !pages || page >= pages;
      }
    }

    prev.addEventListener('click', () => show(page - 1));
    next.addEventListener('click', () => show(page + 1));
    show(1);
  }

  function openFile(path) {
    dir = parentOf(path);
    if (/\.pdf$/i.test(path)) openPdf(path);
    else openText(path);
  }

  function open(path) {
    closeFile();
    if (typeof path === 'object') listDir(path.dir);
    else {
      listDir(parentOf(path));
      openFile(path);
    }
  }

  window.addEventListener('beforeunload', unload);
  if (start) open(start);
  else listDir('');
  return {
    open,
    leave,
    dirty,
    unmount() {
      alive = false;
      unsubscribeGit();
      window.removeEventListener('beforeunload', unload);
      closeFile();
    },
  };
}

export function gitView(host, { git, failed }) {
  const box = el('div', { id: 'code-git', class: 'code-git', 'aria-busy': 'true' });
  const status = el('p', { id: 'code-git-error', class: 'error', 'aria-live': 'polite' });
  host.replaceChildren(box, status);
  const unsubscribe = git.subscribe(({ data: git, error, loading }) => {
    if (loading) box.setAttribute('aria-busy', 'true');
    else box.removeAttribute('aria-busy');
    status.textContent = '';
    if (!git) {
      if (loading) return;
      if (error?.code === 'not_git') box.replaceChildren(el('p', { class: 'muted code-git-none' }, NOT_GIT));
      else if (error) {
        box.replaceChildren();
        failed(error, status);
      }
      return;
    }
    const head = el('p', { class: 'code-git-head' }, 'Ветка ',
      el('b', { class: 'code-git-branch' }, typeof git.branch === 'string' && git.branch ? git.branch : '—'));
    if (typeof git.upstream === 'string' && git.upstream) {
      const ahead = Number(git.ahead) || 0;
      const behind = Number(git.behind) || 0;
      head.append(el('span', { class: 'muted code-git-upstream' }, git.upstream));
      if (ahead) head.append(el('span', { class: 'tag code-git-ahead' }, `впереди на ${ahead}`));
      if (behind) head.append(el('span', { class: 'tag code-git-behind' }, `позади на ${behind}`));
    }
    const files = Array.isArray(git.files) ? git.files : [];
    box.replaceChildren(head, files.length
      ? el('ul', { class: 'code-git-files' }, ...files.map((f) => el('li', {},
        el('span', { class: 'tag code-git-status' }, String(f.status)), el('span', { class: 'code-git-path' }, String(f.path)))))
      : el('p', { class: 'muted code-git-empty' }, NO_CHANGES));
  });
  return {
    unmount() {
      unsubscribe();
    },
  };
}
