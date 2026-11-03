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
 * запоздалый ответ прежнего выбора вид не занимает (всё это — createPicker, feed.js). Раздел без
 * модели (ctx.hint): записи читаются, скачиваются и удаляются, вместо «Загрузить запись» — подсказка.
 *
 * Правка (спека 2026-10-21-studio-transcript-edit-chat-design, раздел 9): «Править» делает тексты
 * реплик полями, «Сохранить» шлёт PATCH только изменённых номеров (ошибка — словами, черновик цел);
 * черновик `editing` переживает перерисовку того же id (опрос, повторный выбор); ответ, пришедший,
 * когда выбрана уже другая запись, вид не занимает. Пока идёт правка, «Копировать», «Скачать» и
 * «В чат» спрятаны (взяли бы сохранённый текст, «В чат» увёл бы от черновика). «Имена» — окно меток,
 * «Сбросить правки» — DELETE …/edits. Вне правки нажатие по реплике (если не выделен текст) или по
 * её времени (кнопка — и с клавиатуры) ставит плеер на её начало, играющая — aria-current по
 * timeupdate. «В чат ▾» → #/chat?transcript=<id>&ask=…; #/transcribe?id=<id> («Открыть» в «В работе»,
 * ссылка «к записи» из чата) выбирает запись после загрузки списка, если её не выбрали сами (нет её —
 * список как обычно), ?id из адреса уходит; принятая запись зовёт jobs.kick(). Текст сервера — только
 * textContent. */
import { api, hooks, me } from './api.js';
import { dateTime } from './time.js';
import { createPicker, dayTitle } from './feed.js';
import { kick } from './jobs.js';
import { el, openDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const UPLOAD_MAX = 200 * 1024 * 1024;
const ACCEPT = 'audio/*,video/*,.m4a,.mp3,.wav,.ogg,.opus,.webm,.flac,.aac';
const RUNNING = new Set(['working', 'cancelling']);
const FORMATS = [['txt', 'Текст .txt'], ['docx', 'Word .docx'], ['srt', 'Субтитры .srt']];
const ASKS = [['discuss', 'Обсудить в чате'], ['summary', 'Составить итоги'], ['tasks', 'Выделить решения и задачи']];
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

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Текст расшифровки — как выгрузка .txt. */
export function asText(segments) {
  return segments.map((s) => `${s.speaker ? `${s.speaker} · ` : ''}${clock(s.start)}\n${s.text}\n`).join('\n');
}

function build(view, ctx) {
  let editing = null;          // { id, drafts: Map номер → текст } — черновик правки выбранной записи
  let audio = null;            // плеер выбранной записи: перерисовка того же id его не пересоздаёт
  let picked = false;          // запись выбрали сами (список, загрузка): ?id из адреса её не перебивает
  // records.current — выбранная запись (с segments, если готова); records.done — старше подгруженных нет
  const records = createPicker({ url: '/api/transcripts', size: PAGE, pollMs: POLL_MS,
    running: (i) => RUNNING.has(i.state), paint: paintList,
    show() {
      error.textContent = '';
      if (editing && editing.id !== records.current.id) editing = null;
      paintList();
      paintView();
    },
    fail(err) { error.textContent = words(err); } });
  const { select, refresh } = records;

  const input = el('input', { type: 'file', class: 'sr', accept: ACCEPT });
  const add = el('label', { class: 'btn btn-accent tr-add', id: 'transcribe-add' }, icon('plus'), 'Загрузить запись', input);
  const progress = el('p', { id: 'transcribe-progress', class: 'muted small', 'aria-live': 'polite' });
  const list = el('nav', { class: 'tr-list', 'aria-label': 'Разговоры' });
  // без модели — не загрузить: вместо кнопки подсказка над видом записи (на телефоне список спрятан)
  const aside = el('aside', { class: 'split-aside', id: 'tr-aside' }, ...(ctx.hint ? [] : [add, progress]), list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'icon-btn split-toggle', 'aria-label': 'Разговоры',
    'aria-controls': 'tr-aside', 'aria-expanded': 'false' }, icon('list'));
  const title = el('h1', {}, 'Разговоры');
  const head = el('header', { class: 'head' }, toggle, title);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view' });
  const main = el('section', { class: 'split-main' }, head, ctx.hint ? ctx.hint('hint-bar') : null, error, body);
  const root = el('div', { class: 'split transcribe' }, aside, backdrop, main);
  view.append(root);

  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список */
  function paintList() {
    const { items, current } = records;
    if (!items.length) {
      list.replaceChildren(el('p', { class: 'muted small' }, 'Здесь появятся ваши записи'));
      return;
    }
    const nodes = [];
    let last = '';
    for (const item of items) {
      const label = dayTitle(item.created);
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
        picked = true;
        select(item.id);
      });
      nodes.push(link);
    }
    if (!records.done) {
      const next = el('button', { type: 'button', class: 'btn btn-sm tr-more', id: 'tr-more', disabled: records.paging },
        records.paging ? 'Загружаю…' : 'Показать ещё');
      next.addEventListener('click', records.more);
      nodes.push(next);
    }
    list.replaceChildren(...nodes);
  }

  /* выбранная */
  function download(format) {
    el('a', { href: `/api/transcripts/${records.current.id}/export?format=${format}`, download: '' }).click();
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
        records.forget(item.id);
        paintView();
        await records.load();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  /* правка */
  function adopt(got) {                              // ответ PATCH и DELETE — как GET
    if (!records.current || records.current.id !== got.id) return;   // пока сохраняли, выбрали другую
    records.current = got;
    paintView();
  }

  function confirmReset(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'tr-reset-confirm' }, 'Сбросить');
    const dialog = openDialog(root, { id: 'tr-reset-dialog', title: 'Сбросить правки?', actions: [yes],
      body: [el('p', {}, 'Тексты реплик и имена вернутся к исходному распознаванию.'), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        const got = await api('DELETE', `/api/transcripts/${item.id}/edits`);
        dialog.close();
        editing = null;
        adopt(got);
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function namesDialog(item) {
    const labels = [...new Set(item.segments.map((s) => s.label).filter(Boolean))];
    const inputs = labels.map((label) => el('input', { class: 'input', 'data-label': label, maxlength: '60',
      placeholder: label, 'aria-label': `Имя: ${label}`, value: item.names[label] || '' }));
    const fail = el('p', { class: 'error' });
    const save = el('button', { type: 'button', class: 'btn btn-accent', id: 'tr-names-save' }, 'Сохранить');
    const dialog = openDialog(root, { id: 'tr-names-dialog', title: 'Имена собеседников', actions: [save],
      body: [...labels.map((label, i) => el('label', { class: 'tr-name-row' }, el('span', {}, `${label} →`), inputs[i])), fail] });
    save.addEventListener('click', async () => {
      save.disabled = true;
      const names = Object.fromEntries(inputs.map((input) => [input.dataset.label, input.value.trim() || null]));
      try {
        const got = await api('PATCH', `/api/transcripts/${item.id}`, { names });
        dialog.close();
        adopt(got);
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    });
  }

  async function saveEdits(item, save) {
    const texts = {};
    for (const [i, text] of editing.drafts) if (text !== item.segments[i].text) texts[i] = text;
    if (!Object.keys(texts).length) {
      editing = null;
      paintView();
      return;
    }
    save.disabled = true;
    error.textContent = '';
    try {
      const got = await api('PATCH', `/api/transcripts/${item.id}`, { texts });
      if (editing && editing.id === item.id) editing = null;   // черновик другой записи — не трогать
      adopt(got);
    } catch (err) {
      if (records.current && records.current.id === item.id) error.textContent = words(err);   // черновик остаётся
      save.disabled = false;
    }
  }

  function grow(area) {
    area.style.height = 'auto';
    area.style.height = `${area.scrollHeight}px`;
  }

  function turnNode(s, i) {
    const speaker = s.speaker ? `${s.speaker} · ` : '';
    const original = s.edited ? s.original : s.text;
    if (!editing) {
      // время реплики — кнопка: перемотка и с клавиатуры, не только нажатием по реплике
      const time = el('button', { type: 'button', class: 'tr-seek', 'aria-label': `Слушать с ${clock(s.start)}` },
        clock(s.start));
      time.addEventListener('click', (event) => {
        event.stopPropagation();
        seek(s.start);
      });
      const node = el('div', { class: 'tr-turn', 'data-index': String(i) }, el('p', { class: 'tr-who' }, speaker, time),
        el('p', { class: 'tr-text' }, s.text), s.edited ? el('p', { class: 'tr-mark' }, 'изменено') : null);
      node.addEventListener('click', () => seek(s.start));
      return node;
    }
    const who = el('p', { class: 'tr-who' }, `${speaker}${clock(s.start)}`);
    const area = el('textarea', { class: 'tr-edit-text', rows: '1', 'aria-label': `Реплика ${i + 1}` });
    area.value = editing.drafts.has(i) ? editing.drafts.get(i) : s.text;
    const revert = el('button', { type: 'button', class: 'btn btn-sm tr-revert' }, 'вернуть');
    const mark = el('p', { class: 'tr-mark' }, 'изменено', revert);
    const paintMark = () => { mark.hidden = area.value === original; };
    area.addEventListener('input', () => {
      editing.drafts.set(i, area.value);
      grow(area);
      paintMark();
    });
    revert.addEventListener('click', () => {
      area.value = original;
      area.dispatchEvent(new Event('input'));
    });
    paintMark();
    return el('div', { class: 'tr-turn editing', 'data-index': String(i) }, who, area, mark);
  }

  /* плеер */
  function seek(start) {
    if (String(getSelection() || '')) return;      // выделяют текст — не перематывать
    audio.currentTime = start;
    audio.play().catch(() => {});                  // нет звука или запрет браузера — только перемотка
  }

  function playing() {
    const segments = (records.current && records.current.segments) || [];
    let at = -1;
    if (audio && (audio.currentTime > 0 || !audio.paused)) {
      segments.forEach((s, i) => { if (s.start <= audio.currentTime) at = i; });
    }
    for (const node of body.querySelectorAll('.tr-turn')) {
      if (Number(node.dataset.index) === at) node.setAttribute('aria-current', 'true');
      else node.removeAttribute('aria-current');
    }
  }

  function playerOf(item) {
    if (!audio || audio.dataset.id !== item.id) {
      audio = el('audio', { class: 'tr-audio', controls: true, preload: 'metadata', 'data-id': item.id,
        src: `/api/media/${encodeURIComponent(item.source_media_id)}` });
      audio.addEventListener('timeupdate', playing);
    }
    return audio;
  }

  function chatMenu(item) {
    const menu = el('div', { class: 'tr-formats tr-chat-menu', hidden: true }, ...ASKS.map(([ask, label]) => {
      const b = el('button', { type: 'button', class: 'btn btn-sm', 'data-ask': ask }, label);
      b.addEventListener('click', () => ctx.navigate(`chat?transcript=${encodeURIComponent(item.id)}&ask=${ask}`));
      return b;
    }));
    const open = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-chat', 'aria-haspopup': 'true' }, 'В чат ▾');
    open.addEventListener('click', () => { menu.hidden = !menu.hidden; });
    return el('div', { class: 'tr-download-wrap' }, open, menu);
  }

  function paintView() {
    const item = records.current;
    title.textContent = item ? item.name : 'Расшифровка';
    if (!item) {
      editing = null;
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, ctx.hint ? 'Выберите запись в списке.'
        : 'Загрузите запись разговора — студия расшифрует её по собеседникам.'));
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
      // во время правки «Копировать», «Скачать» и «В чат» взяли бы сохранённый текст, а не черновик,
      // и «В чат» молча увёл бы от него — их нет до «Сохранить» или «Отмена»
      if (!editing) acts.append(copy, el('div', { class: 'tr-download-wrap' }, more, formats));
      if (item.segments && !editing) {
        const edit = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-edit' }, 'Править');
        edit.addEventListener('click', () => {
          editing = { id: item.id, drafts: new Map() };
          paintView();
        });
        acts.append(edit);
      }
      if (item.segments && item.segments.some((s) => s.label)) {
        const names = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-names' }, 'Имена');
        names.addEventListener('click', () => namesDialog(item));
        acts.append(names);
      }
      if (item.edited) {
        const reset = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-reset' }, 'Сбросить правки');
        reset.addEventListener('click', () => confirmReset(item));
        acts.append(reset);
      }
      if (!editing) acts.append(chatMenu(item));
    }
    if (!RUNNING.has(item.state)) {
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'tr-delete' }, 'Удалить');
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    nodes.push(acts);
    nodes.push(playerOf(item));
    if (item.segments) {
      nodes.push(el('div', { class: 'tr-turns' }, ...item.segments.map(turnNode)));
      if (editing) {
        const save = el('button', { type: 'button', class: 'btn btn-accent btn-sm', id: 'tr-save' }, 'Сохранить');
        const cancel = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-edit-cancel' }, 'Отмена');
        save.addEventListener('click', () => saveEdits(item, save));
        cancel.addEventListener('click', () => {
          editing = null;
          paintView();
        });
        nodes.push(el('div', { class: 'tr-edit-bar' }, save, cancel));
      }
    }
    body.replaceChildren(...nodes);
    for (const area of body.querySelectorAll('.tr-edit-text')) grow(area);
    playing();
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
        kick();
        picked = true;
        await records.load();
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
  /** ?id адреса («Открыть» в «В работе», «к записи» из чата) — при сборке и при переходе внутри раздела
   * (update): черновик правки той же записи цел (show его не трогает). */
  function follow(wanted) {
    if (wanted) {
      history.replaceState(history.state, '', '#/transcribe');
      picked = false;
    }
    // пока шёл список, запись могли выбрать сами (загрузка, список другого чтения) — её не перебивать
    records.load().then(() => { if (wanted && !picked) select(wanted, true); });
  }
  paintView();
  follow(ctx.query && ctx.query.get('id'));

  return {
    follow,
    destroy() {
      records.stop();
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
}

/** Переход внутри открытого раздела (#/transcribe?id=…): только ?id — правка и прочее состояние целы. */
export function update(ctx) {
  const wanted = ctx.query && ctx.query.get('id');
  if (screen && wanted) screen.follow(wanted);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
