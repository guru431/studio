/* Раздел «Расшифровка» (#/transcribe) — раскладка Б спеки 2026-10-20 (раздел 6.3).
 *
 * Слева — «+ Загрузить запись» и записи по дням (имя, длительность, состояние), справа — выбранная:
 * длительность, число собеседников, время, «Копировать», «Скачать ▾» (txt, docx, srt), «Удалить»,
 * плеер исходной записи и реплики «Собеседник N · мм:сс». Идущая — состояние словами и «Отменить».
 * На телефоне список выезжает сбоку, как диалоги чата.
 *
 * Загрузка — XMLHttpRequest (ход в процентах): тело — сама запись, имя — заголовком X-Studio-Name
 * (encodeURIComponent; строка запроса попала бы в журнал Apache); 423 запертой студии (XHR идёт
 * мимо api.js) ждёт отпирания и шлёт ту же запись заново. Идущие записи опрашиваются
 * раз в POLL_MS. Список — страницами по PAGE (курсор — created и id последней, feed.js), старше —
 * «Показать ещё»; опрос обновляет первую страницу, подгруженные остаются. Выбор — по номеру:
 * запоздалый ответ прежнего выбора вид не занимает. Текст сервера — только textContent. */
import { api, hooks, me } from './api.js';
import { pageQuery, withFirstPage } from './feed.js';
import { el, openDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const UPLOAD_MAX = 200 * 1024 * 1024;
const ACCEPT = 'audio/*,video/*,.m4a,.mp3,.wav,.ogg,.opus,.webm,.flac,.aac';
const RUNNING = new Set(['working', 'cancelling']);
const FORMATS = [['txt', 'Текст .txt'], ['docx', 'Word .docx'], ['srt', 'Субтитры .srt']];
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h16"/>',
};

let screen = null;

function icon(name) {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
  return box.firstChild;
}

function clock(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function day(ts) {
  return new Date(ts * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}

function when(ts) {
  return new Date(ts * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Текст расшифровки — как выгрузка .txt. */
export function asText(segments) {
  return segments.map((s) => `${s.speaker ? `${s.speaker} · ` : ''}${clock(s.start)}\n${s.text}\n`).join('\n');
}

function build(view, ctx) {
  let items = [];
  let current = null;          // выбранная запись (с segments, если готова)
  let alive = true;
  let poll = null;
  let done = false;            // старше подгруженных записей нет
  let paging = false;
  let listing = 0;             // номер чтения первой страницы: запоздавшее прежнее не ляжет
  let removals = 0;            // удаления: страница, начатая до удаления, не вернёт удалённую
  let picking = 0;             // номер выбора: запоздалый ответ прежнего вид не займёт
  let wanted = null;           // id последнего выбора

  const input = el('input', { type: 'file', class: 'sr', accept: ACCEPT });
  const add = el('label', { class: 'btn btn-accent tr-add', id: 'transcribe-add' }, icon('plus'), 'Загрузить запись', input);
  const progress = el('p', { id: 'transcribe-progress', class: 'muted small', 'aria-live': 'polite' });
  const list = el('nav', { class: 'tr-list', 'aria-label': 'Записи' });
  const aside = el('aside', { class: 'split-aside', id: 'tr-aside' }, add, progress, list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'icon-btn split-toggle', 'aria-label': 'Записи',
    'aria-controls': 'tr-aside', 'aria-expanded': 'false' }, icon('list'));
  const title = el('h1', {}, 'Расшифровка');
  const head = el('header', { class: 'head' }, toggle, title);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view' });
  const main = el('section', { class: 'split-main' }, head, error, body);
  const root = el('div', { class: 'split transcribe' }, aside, backdrop, main);
  view.append(root);

  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список */
  function paintList() {
    if (!items.length) {
      list.replaceChildren(el('p', { class: 'muted small' }, 'Здесь появятся ваши записи'));
      return;
    }
    const nodes = [];
    let last = '';
    for (const item of items) {
      const label = day(item.created);
      if (label !== last) {
        nodes.push(el('p', { class: 'tr-group' }, label));
        last = label;
      }
      const state = item.state === 'done' ? clock(item.duration)
        : RUNNING.has(item.state) ? item.detail : item.state === 'cancelled' ? 'отменено' : 'ошибка';
      const link = el('button', { type: 'button', class: 'tr-item', 'data-id': item.id, 'data-state': item.state,
        'aria-current': current && current.id === item.id ? 'true' : null },
      el('span', { class: 'tr-name' }, item.name), el('span', { class: 'tr-state' }, state));
      link.addEventListener('click', () => {
        panel(false);
        select(item.id);
      });
      nodes.push(link);
    }
    if (!done) {
      const next = el('button', { type: 'button', class: 'btn btn-sm tr-more', id: 'tr-more', disabled: paging },
        paging ? 'Загружаю…' : 'Показать ещё');
      next.addEventListener('click', more);
      nodes.push(next);
    }
    list.replaceChildren(...nodes);
  }

  async function loadList() {
    const mine = ++listing;
    try {
      const first = (await api('GET', `/api/transcripts?limit=${PAGE}`)).items;
      if (mine !== listing) return;
      items = withFirstPage(first, items, PAGE);
      if (first.length < PAGE) done = true;
    } catch (err) {
      if (mine !== listing) return;
      error.textContent = words(err);
    }
    paintList();
    schedule();
  }

  async function more() {
    if (paging || done || !items.length) return;
    paging = true;
    const removed = removals;
    paintList();
    try {
      const got = (await api('GET', `/api/transcripts?limit=${PAGE}${pageQuery(items[items.length - 1])}`)).items;
      if (removed === removals) {
        const ids = new Set(items.map((x) => x.id));
        items = [...items, ...got.filter((x) => !ids.has(x.id))];
        done = got.length < PAGE;
      }
    } catch (err) {
      error.textContent = words(err);
    }
    paging = false;
    if (alive) paintList();
  }

  function schedule() {
    clearTimeout(poll);
    if (alive && items.some((i) => RUNNING.has(i.state))) poll = setTimeout(refresh, POLL_MS);
  }

  async function refresh() {
    await loadList();
    if (current && wanted === current.id) {           // выбор другой записи в пути — его не перебивать
      const fresh = items.find((i) => i.id === current.id);
      if (fresh && (fresh.state !== current.state || fresh.detail !== current.detail)) await select(current.id);
    }
  }

  /* выбранная */
  async function select(id) {
    const mine = ++picking;
    wanted = id;
    let got;
    try {
      got = await api('GET', `/api/transcripts/${id}`);
    } catch (err) {
      if (mine === picking) error.textContent = words(err);
      return;
    }
    if (!alive || mine !== picking) return;
    current = got;
    error.textContent = '';
    paintList();
    paintView();
  }

  function download(format) {
    el('a', { href: `/api/transcripts/${current.id}/export?format=${format}`, download: '' }).click();
  }

  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'tr-delete-confirm' }, 'Удалить');
    const dialog = openDialog(root, { id: 'tr-delete-dialog', title: 'Удалить расшифровку?', actions: [yes],
      body: [el('p', {}, 'Удалятся текст и исходная запись.'), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/transcripts/${item.id}`);
        dialog.close();
        removals += 1;
        items = items.filter((x) => x.id !== item.id);
        picking += 1;                                     // выбор, начатый до удаления, вид не займёт
        wanted = null;
        current = null;
        paintView();
        await loadList();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function paintView() {
    const item = current;
    title.textContent = item ? item.name : 'Расшифровка';
    if (!item) {
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, 'Загрузите запись разговора — студия расшифрует её по собеседникам.'));
      return;
    }
    const nodes = [];
    if (RUNNING.has(item.state)) {
      const cancel = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-cancel' }, 'Отменить');
      cancel.addEventListener('click', async () => {
        cancel.disabled = true;
        try {
          await api('POST', `/api/transcripts/${item.id}/cancel`);
        } catch (err) {
          error.textContent = words(err);
        }
        refresh();
      });
      nodes.push(el('div', { class: 'tr-busy' }, el('span', { class: 'ring', 'aria-hidden': 'true' }),
        el('p', {}, item.detail), cancel));
    } else if (item.state !== 'done') {
      nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, item.detail));
    }
    const speakers = item.speakers ? `собеседников: ${item.speakers}` : 'собеседники не разделены';
    nodes.push(el('p', { class: 'tr-meta muted small' },
      [item.duration ? `длительность ${clock(item.duration)}` : '', item.state === 'done' ? speakers : '', when(item.created)]
        .filter(Boolean).join(' · ')));
    const acts = el('div', { class: 'tr-acts' });
    if (item.state === 'done') {
      const copy = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-copy' }, 'Копировать');
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(asText(item.segments));
          copy.textContent = 'Скопировано';
        } catch {
          error.textContent = 'Буфер обмена недоступен — выделите текст вручную';
        }
      });
      const formats = el('div', { class: 'tr-formats', hidden: true }, ...FORMATS.map(([format, label]) => {
        const b = el('button', { type: 'button', class: 'btn btn-sm', 'data-format': format }, label);
        b.addEventListener('click', () => {
          formats.hidden = true;
          download(format);
        });
        return b;
      }));
      const more = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-download', 'aria-haspopup': 'true' }, 'Скачать ▾');
      more.addEventListener('click', () => { formats.hidden = !formats.hidden; });
      acts.append(copy, el('div', { class: 'tr-download-wrap' }, more, formats));
    }
    if (!RUNNING.has(item.state)) {
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'tr-delete' }, 'Удалить');
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    nodes.push(acts);
    nodes.push(el('audio', { class: 'tr-audio', controls: true, preload: 'metadata',
      src: `/api/media/${encodeURIComponent(item.source_media_id)}` }));
    if (item.segments) {
      nodes.push(el('div', { class: 'tr-turns' }, ...item.segments.map((s) => el('div', { class: 'tr-turn' },
        el('p', { class: 'tr-who' }, `${s.speaker ? `${s.speaker} · ` : ''}${clock(s.start)}`),
        el('p', { class: 'tr-text' }, s.text)))));
    }
    body.replaceChildren(...nodes);
  }

  /* загрузка */
  function send(file) {
    if (file.size > UPLOAD_MAX) {
      error.textContent = 'Запись больше 200 МиБ';
      return;
    }
    error.textContent = '';
    input.disabled = true;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/transcripts');
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.setRequestHeader('X-Studio-Name', encodeURIComponent(file.name));
    xhr.setRequestHeader('x-studio-csrf', me.csrf);
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) progress.textContent = `Загружаю запись · ${Math.round(event.loaded * 100 / event.total)}%`;
    });
    xhr.addEventListener('loadend', async () => {
      input.disabled = false;
      progress.textContent = '';
      let answer = null;
      try {
        answer = JSON.parse(xhr.responseText);
      } catch {
        // не JSON — ниже словами по коду
      }
      if (xhr.status === 202 && answer) {
        await loadList();
        await select(answer.id);
        return;
      }
      if (xhr.status === 401) location.href = '/login';
      const detail = answer && answer.detail;
      if (xhr.status === 423 && detail && detail.code === 'screen_locked' && hooks.locked) {
        await hooks.locked();          // студия заперта (lock.js): та же запись — после отпирания
        send(file);
        return;
      }
      error.textContent = words((detail && detail.message) || (xhr.status ? `Ошибка сервера (${xhr.status})` : 'Нет связи с сервером'));
    });
    xhr.send(file);
  }

  input.addEventListener('change', () => {
    const file = input.files[0];
    input.value = '';
    if (file) send(file);
  });
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  paintView();
  loadList();

  return {
    destroy() {
      alive = false;
      clearTimeout(poll);
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
