/* Вкладки «Файлы» и «Git» проекта Claude Code (#/code/<aid>/<pid>) — спека 2026-10-28-studio-claude-code-design
 * 6.7, протокол канала §4 (files, read, raw, save, git) и §6 (пути).
 *
 * filesView(host, { aid, pid, name, failed, path }) → { open(path), leave(go), unmount() } (path — открыть сразу, иначе корень):
 * - путь #code-path (корень — имя проекта, части — к своей папке), папка #code-files — li[data-name] в порядке
 *   моста (папки сначала); нажатие на папку — её список, на файл — сам файл; на узком экране, пока открыт
 *   файл, список папки скрыт (назад ведёт путь), на широком — рядом;
 * - текстовый — #code-editor и «Сохранить» (#code-save): PUT …/file с version открытого — файл изменился на
 *   машине (его мог поправить Claude) — 409 conflict и слова в #code-file-status, на машине ничего не
 *   затирается; textarea приводит концы строк к \n — у файла с \r\n они возвращаются при сохранении;
 *   .md — «Текст» / «Вид» (#code-md-view, markdown.js); binary и too_large — слова, файл не открывается;
 * - .pdf — страница картинкой (#code-pdf img из POST …/pdf, число страниц — X-Studio-Pages), «‹» «›» и
 *   «Страница N из M»; прежний blob URL освобождается при смене страницы, файла и уходе;
 * - open(path) — папка файла и сам файл (ссылка «Открыть файл» шага чата);
 * - текст правлен и не сохранён, а уходят на другой файл, папку (путь, список) или вкладку (leave(go) из
 *   code.js) — окно «Изменения не сохранены»: «Уйти без сохранения» (правка отброшена) или «Остаться»;
 * - «Сохранить» получило 409 closed — правка и её version ждут в памяти модуля (edits, ключ aid/pid/путь), а
 *   failed(err, box, path) уводит к PIN; файл, открытый снова (code.js возвращает к нему после PIN), берёт
 *   правку поверх прочитанного с прежним version: изменил его за это время Claude — «Сохранить» скажет
 *   conflict. В хранилище браузера правка не попадает.
 * gitView(host, { aid, pid, failed }) → { unmount() }: #code-git — «Ветка», «впереди на N» / «позади на N»
 * (если есть upstream), изменённые файлы (status, path); пусто — «Изменений нет», 409 not_git — «Не
 * репозиторий git».
 * failed(err, box, path) — общие отказы раздела из code.js (closed — к PIN этого компьютера, с path — назад к
 * этому файлу; offline — слова).
 * Пути — только в теле запросов (поэтому чтения — POST): ни в адрес, ни в хранилище браузера; текст машины —
 * только textContent и markdown.js. */
import { api } from './api.js';
import { renderMarkdown } from './markdown.js';
import { sizeText } from './settings.js';
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

export function filesView(host, { aid, pid, name, failed, path: start = null }) {
  const url = `/api/code/agents/${aid}/projects/${pid}`;
  let alive = true;
  let dir = '';
  let listGen = 0;        // ответ прежней папки новый список не перерисует
  let fileGen = 0;        // так же — прежнего файла
  let blobUrl = null;
  let editing = null;     // открытый текст: { editor, base — текст как прочитан или сохранён, reset() }

  const crumbs = el('nav', { id: 'code-path', class: 'code-crumbs code-path', 'aria-label': 'Путь файла' });
  const list = el('ul', { id: 'code-files', class: 'code-files' });
  const listError = el('p', { id: 'code-files-error', class: 'error', 'aria-live': 'polite' });
  const file = el('section', { id: 'code-file', class: 'code-file', hidden: true });
  const view = el('div', { class: 'code-files-view' }, crumbs, listError, list, file);
  host.replaceChildren(view);

  function refuse(err, box, path) {
    if (alive) failed(err, box, path);
  }

  function dropBlob() {
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = null;
  }

  /** Уйти с открытого текста: правка не сохранена — сначала спросить; «Уйти без сохранения» её отбрасывает. */
  function leave(go) {
    if (!editing || editing.editor.value === editing.base) {
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

  async function listDir(path) {
    const mine = ++listGen;
    dir = path;
    if (file.hidden) showPath(path, false);
    listError.textContent = '';
    list.setAttribute('aria-busy', 'true');
    try {
      const { items } = await api('POST', `${url}/files`, { path });
      if (!alive || mine !== listGen) return;
      list.replaceChildren(...items.map((item) => {
        const row = el('button', { type: 'button', class: `code-file-item${item.dir ? ' dir' : ''}` },
          icon(item.dir ? FOLDER_ICON : FILE_ICON), el('span', { class: 'code-file-name' }, item.name),
          item.dir || typeof item.size !== 'number' ? null : el('span', { class: 'muted small code-file-size' }, sizeText(item.size)));
        row.addEventListener('click', () => leave(() => {
          if (item.dir) {
            closeFile();
            listDir(join(dir, item.name));
          } else openFile(join(dir, item.name));
        }));
        return el('li', { 'data-name': item.name, class: item.dir ? 'dir' : null }, row);
      }));
    } catch (err) {
      if (!alive || mine !== listGen) return;
      list.replaceChildren();
      refuse(err, listError);
    } finally {
      if (mine === listGen) list.removeAttribute('aria-busy');
    }
  }

  function closeFile() {
    fileGen += 1;
    editing = null;
    dropBlob();
    file.hidden = true;
    file.replaceChildren();
    view.classList.remove('has-file');      // узко список папки прячется, пока открыт файл (путь ведёт назад)
  }

  function fileFrame(path) {
    closeFile();
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
        refuse(err, status);
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
    const mark = { editor, base, reset() {
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
        refuse(err, status);
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
    listDir(parentOf(path));
    openFile(path);
  }

  if (start) open(start);
  else listDir('');
  return {
    open,
    leave,
    unmount() {
      alive = false;
      closeFile();
    },
  };
}

export function gitView(host, { aid, pid, failed }) {
  let alive = true;
  const box = el('div', { id: 'code-git', class: 'code-git', 'aria-busy': 'true' });
  const status = el('p', { id: 'code-git-error', class: 'error', 'aria-live': 'polite' });
  host.replaceChildren(box, status);
  api('GET', `/api/code/agents/${aid}/projects/${pid}/git`).then((git) => {
    if (!alive) return;
    const head = el('p', { class: 'code-git-head' }, 'Ветка ',
      el('b', { class: 'code-git-branch' }, typeof git.branch === 'string' && git.branch ? git.branch : '—'));
    if (typeof git.upstream === 'string' && git.upstream) {
      head.append(el('span', { class: 'muted code-git-upstream' }, git.upstream),
        el('span', { class: 'tag code-git-ahead' }, `впереди на ${Number(git.ahead) || 0}`),
        el('span', { class: 'tag code-git-behind' }, `позади на ${Number(git.behind) || 0}`));
    }
    const files = Array.isArray(git.files) ? git.files : [];
    box.replaceChildren(head, files.length
      ? el('ul', { class: 'code-git-files' }, ...files.map((f) => el('li', {},
        el('span', { class: 'tag code-git-status' }, String(f.status)), el('span', { class: 'code-git-path' }, String(f.path)))))
      : el('p', { class: 'muted code-git-empty' }, NO_CHANGES));
  }, (err) => {
    if (!alive) return;
    if (err.code === 'not_git') box.replaceChildren(el('p', { class: 'muted code-git-none' }, NOT_GIT));
    else failed(err, status);
  }).finally(() => box.removeAttribute('aria-busy'));
  return {
    unmount() {
      alive = false;
    },
  };
}
