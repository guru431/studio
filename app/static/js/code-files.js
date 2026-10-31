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
import { fileMark, GIT_ACTIONS } from './code-git.js';
import { renderMarkdown } from './markdown.js';
import { button, el, openDialog, words } from './ui.js';

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

/** Правки, чьё сохранение застал 409 closed: `<aid>/<pid>/<путь>` → { text, version }; только память страницы. */
const edits = new Map();

export function filesView(host, { aid, pid, name, failed, path: start = null, git }) {
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

  const crumbs = el('nav', { id: 'code-path', class: 'code-crumbs code-path', 'aria-label': 'Путь файла' });
  const list = el('ul', { id: 'code-files', class: 'code-files', role: 'tree', 'aria-label': 'Файлы проекта' });
  directories.set('', { group: list, loaded: false, pending: null });
  const listError = el('p', { id: 'code-files-error', class: 'error', 'aria-live': 'polite' });
  const file = el('section', { id: 'code-file', class: 'code-file', hidden: true });
  const view = el('div', { class: 'code-files-view' }, crumbs, listError, list, file);
  host.replaceChildren(view);

  function refuse(err, box, path) {
    if (!alive) return;
    if (err.code === 'closed') {
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
    row.title = `${path}${badge.title ? ` — ${badge.title}` : ''}`;
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
    const row = el('button', { type: 'button', class: `code-file-item${item.dir ? ' dir' : ''}`,
      style: `--tree-depth: ${path.split('/').length - 1}` }, chevron,
    icon(item.dir ? FOLDER_ICON : FILE_ICON), el('span', { class: 'code-file-name' }, item.name), badge);
    const li = el('li', { 'data-name': item.name, 'data-path': path, role: 'treeitem',
      'aria-label': item.name, class: item.dir ? 'dir' : null }, row);
    const node = { path, item, row, badge, li };
    nodes.set(path, node);
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
          return old && old.item.dir === item.dir ? old.li : treeRow(item, path);
        });
        const kept = new Set(items.map((item) => join(path, item.name)));
        for (const child of group.children) {
          const removed = child.dataset.path;
          if (!removed || kept.has(removed)) continue;
          dropNode(removed);
        }
        group.replaceChildren(...children);
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
        if (alive && mine === fileGen) say(status, SAVED, false);
        if (alive) git.refresh(true);
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

export function gitView(host, { git, failed, controls: initial = {}, action }) {
  let allowed = initial;
  let hasGit = false;
  const buttons = GIT_ACTIONS.map(({ id, label, prompt }) => {
    const compact = id === 'push-all' ? 'Все Git' : id === 'deploy' ? 'Деплой' : label;
    const button = el('button', { type: 'button', id: `code-git-${id}`, class: 'btn btn-sm',
      title: `${label} · выполнить в выбранном чате`, 'aria-label': label, disabled: true },
    el('span', { class: 'code-git-label-full' }, label), el('span', { class: 'code-git-label-compact' }, compact));
    button.addEventListener('click', () => { if (hasGit && allowed.enabled) action(prompt); });
    return button;
  });
  const actions = el('div', { class: 'code-git-actions', role: 'group', 'aria-label': 'Действия Git в выбранном чате' }, ...buttons);
  const hint = el('p', { class: 'muted small code-git-hint' });
  function controls(next) {
    allowed = next;
    for (const button of buttons) button.disabled = !hasGit || !allowed.enabled;
    hint.textContent = allowed.message || '';
    hint.hidden = !hint.textContent;
  }
  const box = el('div', { id: 'code-git', class: 'code-git', 'aria-busy': 'true' });
  const status = el('p', { id: 'code-git-error', class: 'error', 'aria-live': 'polite' });
  host.replaceChildren(actions, hint, box, status);
  const unsubscribe = git.subscribe(({ data: git, error, loading }) => {
    hasGit = !!git;
    controls(allowed);
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
    controls,
    unmount() {
      unsubscribe();
    },
  };
}
