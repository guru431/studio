/* Раздел «Перевод» (#/translate) — спека 2026-10-28-studio-translator-design, 6.1–6.4.
 *
 * Шапка — «Перевод» и выбор модели #tl-model («подключение · модель» из отмеченных раздела, GET /api/sections;
 * нелокальная по `local_models` — « · облако»): выбор живёт в памяти экрана, по умолчанию — умолчание раздела,
 * и уходит в каждый запрос (`connection_id`, `model`). Ниже — языки (#tl-source с «Определить язык» первым,
 * ⇄ #tl-swap, #tl-target), два окна (широко — рядом, ≤ 760 px — друг под другом) и история.
 *
 * Левое окно — поле fieldBox (#tl-input, «очистить», микрофон) или плашка #tl-chip: выбранный файл или снимок
 * (имя, крестик #tl-chip-x — назад к полю) либо исходник открытой записи-файла (имя — ссылка на файл
 * /api/media/<id>, под ней текст). Под окном — «Файл» (#tl-file-input), «Фото» (#tl-photo-input), счётчик
 * #tl-count и «Перевести» #tl-go. Правое окно #tl-output — перевод (только чтение), над ним состояние
 * #tl-status, «Остановить» #tl-stop и «Продолжить» #tl-continue, под ним «Копировать» #tl-copy и «Скачать ▾»
 * #tl-export (.txt, .docx).
 *
 * «Перевести»: выбран файл — XMLHttpRequest с FormData на /api/translate/files, языки и модель — заголовками
 * X-Studio-* (имя файла — в части формы, не в адресе), 423 запертой студии ждёт отпирания и шлёт тот же файл
 * заново; открыта запись-файл или снимок — POST /<id>/repeat (новая запись из сохранённого исходника); иначе —
 * текст поля POST /api/translate. Принятая запись открывается, зовётся jobs.kick(). Открытая идущая запись
 * опрашивается раз в POLL_MS: GET /<id>?from=k, k — число готовых кусков подряд (сервер шлёт только куски
 * после них), справа — готовые куски с разделителями и `live` идущего. Опрос той же записи перерисовывает только
 * перемены: перевод дописывается в конец, плашка, строки истории и «Показать ещё» — те же узлы, пока их данные те
 * же; aria-live — только у состояния, не у перевода. «Перевести» закрыта, пока принятая запись не открыта.
 *
 * Языки при входе — пара последней записи истории, без истории — «Определить язык» → умолчание сервера.
 * Открытая запись ставит свои: исходник — выбранный, справа — куда переведено на деле (`result_lang`);
 * определённый язык виден в «Определить язык · английский». ⇄ при «Определить язык» берёт определённый
 * язык показанной записи (нет — кнопка закрыта); у готовой текстовой записи окна меняются текстом (запись
 * отпущена), следующий ⇄ меняет тексты обратно вместе с языками. × и ⇄ сильнее перечитывания в пути (deselect).
 *
 * История — createPicker (feed.js) под окнами: по дням, «EN → RU», время, вид, начала исходника и перевода,
 * состояние идущих; нажатие — запись в окнах, крестик — удаление с подтверждением. #/translate?id=<id>
 * («В работе», уведомление, «Поиск») открывает запись после загрузки истории, если её не выбрали сами; ?id из
 * адреса уходит. Раздел без модели (ctx.hint): история читается, скачивается и удаляется, вместо «Перевести»
 * — подсказка. Текст сервера — только textContent.
 *
 * Запись-снимок (translate-photo.js) — над правым окном переключатель «Поверх снимка» (по умолчанию: снимок с
 * переводом блоков вместо #tl-output, слева — только плашка) / «Текстом» (слева распознанный текст, справа
 * перевод). Режим живёт в памяти экрана; оба вида — из одной записи, без нового распознавания. */
import { api, hooks, me } from './api.js';
import { createPicker, dayTitle } from './feed.js';
import { fieldBox } from './field.js';
import { kick } from './jobs.js';
import { photoView } from './translate-photo.js';
import { el, openDialog, words } from './ui.js';

const POLL_MS = 1000;                    // опрос открытой идущей записи
const LIST_POLL_MS = 3000;               // опрос истории, пока в ней что-то идёт
const PAGE = 30;
const TEXT_MAX = 300000;                 // translate_text.TEXT_MAX
const UPLOAD_MAX = 20 * 1024 * 1024;     // documents.DOC_MAX
const AUTO = 'auto';
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const FILE_ACCEPT = '.pdf,.docx,.txt,.md';
const PHOTO_ACCEPT = 'image/png,image/jpeg,image/webp';
const FORMATS = [['txt', 'Текст .txt'], ['docx', 'Word .docx']];
const FILE_KINDS = { pdf: 'PDF', docx: 'Word', txt: 'TXT', md: 'MD' };
const ICONS = {
  text: '<path d="M5 6h14M5 12h14M5 18h9"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  photo: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  swap: '<path d="M5 8h14l-3-3M19 16H5l3 3"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
};

let screen = null;

function icon(name) {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
  return box.firstChild;
}

/** 300000 → «300 000». */
function grouped(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

function capital(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function timeOf(ts) {
  return new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function kindLabel(item) {
  if (item.kind === 'text') return 'текст';
  if (item.kind === 'photo') return 'фото';
  const ext = String(item.name || '').split('.').pop().toLowerCase();
  return FILE_KINDS[ext] || 'файл';
}

/** «EN → RU»: исходник — определённый или выбранный («auto» без определённого — «?»), справа — куда переведено. */
function pair(item) {
  const from = item.detected_lang || item.source_lang;
  return `${from === AUTO ? '?' : from.toUpperCase()} → ${(item.result_lang || item.target_lang).toUpperCase()}`;
}

/** Готовые куски подряд с начала (по ним — `?from=`). */
function doneHead(parts) {
  let k = 0;
  for (const p of parts) {
    if (p.n !== k + 1 || p.state !== 'done') break;
    k += 1;
  }
  return k;
}

/** Перевод записи: готовые куски подряд с разделителями, у идущей — ещё перевод идущего куска. */
function translation(rec) {
  const out = [];
  for (const p of rec.parts) {
    if (p.state !== 'done') break;
    out.push(p.text || '', p.sep || '');
  }
  return rec.live ? out.join('') + rec.live : out.slice(0, -1).join('');
}

function build(view, ctx) {
  let alive = true;
  let languages = [];          // [{ code, name }] сервера
  let links = [];              // подключения раздела с отмеченными моделями
  let pending = null;          // выбранный и ещё не отправленный файл или снимок: { file, photo }
  let rec = null;              // открытая запись целиком (GET …/<id>): куски — массивом по порядку
  let loose = '';              // текст справа без записи (⇄ поменял окна местами)
  let touched = false;         // языки выбрал человек: пара из истории их не перебьёт
  let picked = false;          // запись выбрали сами: ?id из адреса её не перебьёт
  let sending = false;
  let timer = null;
  let photo = null;            // вид открытой записи-снимка: { id, node, setMode, update, destroy }
  let photoMode = 'overlay';   // 'overlay' | 'text'
  let listed = false;          // первая страница истории пришла (до неё — не «Здесь появятся ваши переводы»)
  let pollWords = '';          // слова неудачного опроса записи: удачный опрос их уберёт
  let chipKey = '';            // что показывает плашка: та же — не пересобирается
  let statusKey = '';          // что показывает #tl-status: то же — не перерисовывается (aria-live — только перемены)
  let rows = new Map();        // id → { sig, node } строк истории: строка с теми же данными — тот же узел
  const dayNodes = new Map();  // подпись дня → узел

  const docs = createPicker({ url: '/api/translate', size: PAGE, pollMs: LIST_POLL_MS, polled: true,
    running: (i) => RUNNING.has(i.state),
    paint() {
      listed = true;
      paintHistory();
    },
    show() { open(docs.current); },
    fail(err) { error.textContent = words(err); } });

  /* шапка и языки */
  const model = el('select', { id: 'tl-model', class: 'chip tl-model', 'aria-label': 'Модель перевода', hidden: true });
  const head = el('header', { class: 'head tl-head' }, el('h1', {}, 'Перевод'), model);
  const source = el('select', { id: 'tl-source', class: 'input tl-lang', 'aria-label': 'Язык исходника' });
  const target = el('select', { id: 'tl-target', class: 'input tl-lang', 'aria-label': 'Язык перевода' });
  const swap = el('button', { type: 'button', id: 'tl-swap', class: 'icon-btn', 'aria-label': 'Поменять языки местами',
    title: 'Поменять языки местами', disabled: true }, icon('swap'));
  const langRow = el('div', { class: 'tl-langs' }, source, swap, target);

  /* левое окно */
  const input = el('textarea', { id: 'tl-input', class: 'tl-input', rows: '8', 'aria-label': 'Текст для перевода',
    placeholder: 'Введите или вставьте текст' });
  const textBox = fieldBox(input);
  const chip = el('div', { id: 'tl-chip', class: 'att-chip tl-chip', hidden: true });
  const sourceText = el('div', { class: 'tl-source-text', hidden: true });
  const fileInput = el('input', { type: 'file', id: 'tl-file-input', class: 'sr', accept: FILE_ACCEPT });
  const photoInput = el('input', { type: 'file', id: 'tl-photo-input', class: 'sr', accept: PHOTO_ACCEPT });
  const fileBtn = el('label', { class: 'btn btn-sm tl-pick' }, icon('file'), 'Файл', fileInput);
  const photoBtn = el('label', { class: 'btn btn-sm tl-pick' }, icon('photo'), 'Фото', photoInput);
  const count = el('span', { id: 'tl-count', class: 'muted small tl-count' });
  const goBtn = el('button', { type: 'button', id: 'tl-go', class: 'btn btn-accent composer-go' }, 'Перевести');
  const bar = el('div', { class: 'composer-bar' },
    el('div', { class: 'composer-opts' }, ...(ctx.hint ? [count] : [fileBtn, photoBtn, count])), ctx.hint ? null : goBtn);
  const left = el('div', { class: 'tl-pane tl-left' }, textBox.box, textBox.error, chip, sourceText, bar);

  /* правое окно */
  const status = el('p', { id: 'tl-status', class: 'tl-status small', 'aria-live': 'polite' });
  const stop = el('button', { type: 'button', id: 'tl-stop', class: 'btn btn-sm', hidden: true }, 'Остановить');
  const cont = el('button', { type: 'button', id: 'tl-continue', class: 'btn btn-sm', hidden: true }, 'Продолжить');
  const outText = document.createTextNode('');   // перевод одним текстовым узлом: растёт — дописывается
  const output = el('div', { id: 'tl-output', class: 'tl-output', 'data-placeholder': 'Перевод' }, outText);
  const copy = el('button', { type: 'button', id: 'tl-copy', class: 'btn btn-sm' }, 'Копировать');
  const formats = el('div', { class: 'tr-formats tl-formats', hidden: true }, ...FORMATS.map(([key, label]) => {
    const b = el('button', { type: 'button', class: 'btn btn-sm', 'data-format': key }, label);
    b.addEventListener('click', () => {
      formats.hidden = true;
      download(key);
    });
    return b;
  }));
  const exportBtn = el('button', { type: 'button', id: 'tl-export', class: 'btn btn-sm', 'aria-haspopup': 'true' }, 'Скачать ▾');
  const acts = el('div', { class: 'tr-acts tl-acts', hidden: true }, copy, el('div', { class: 'tr-download-wrap' }, exportBtn, formats));
  const right = el('div', { class: 'tl-pane tl-right' }, el('div', { class: 'tl-state' }, status, stop, cont), output, acts);

  const error = el('p', { class: 'error tl-error', 'aria-live': 'polite' });
  const list = el('div', { id: 'tl-history', class: 'tl-history' });
  const empty = el('p', { class: 'muted small' }, 'Здесь появятся ваши переводы');
  const more = el('button', { type: 'button', class: 'btn btn-sm tr-more', id: 'tl-more' }, 'Показать ещё');
  more.addEventListener('click', () => docs.more());
  const body = el('div', { class: 'tl-view' }, ctx.hint ? ctx.hint('hint-bar') : null, langRow,
    el('div', { class: 'tl-panes' }, left, right), error,
    el('section', { class: 'tl-past', 'aria-label': 'История переводов' }, el('h2', {}, 'История'), list));
  view.append(head, body);

  /* модель и языки */
  function chosen() {
    if (!model.value) return {};
    const [id, ...rest] = model.value.split(':');
    return { connection_id: Number(id), model: rest.join(':') };
  }

  async function loadModels() {
    try {
      const { sections } = await api('GET', '/api/sections');
      const section = sections.find((s) => s.id === 'translate');
      links = section ? section.links : [];
      model.replaceChildren(...links.flatMap(({ connection: c, models, local_models: local }) => models.map((m) =>
        el('option', { value: `${c.id}:${m}` }, `${c.name} · ${m}${(local || []).includes(m) ? '' : ' · облако'}`))));
      if (section && section.default) model.value = `${section.default.connection_id}:${section.default.model}`;
      model.hidden = !model.options.length;
    } catch (err) {
      error.textContent = words(err);
    }
  }

  function langName(code) {
    const found = languages.find((l) => l.code === code);
    return found ? found.name : code;
  }

  async function loadLanguages() {
    let answer;
    try {
      answer = await api('GET', '/api/translate/languages');
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    languages = answer.items;
    const options = () => languages.map((l) => el('option', { value: l.code }, capital(l.name)));
    source.replaceChildren(el('option', { value: AUTO }, 'Определить язык'), ...options());
    target.replaceChildren(...options());
    source.value = AUTO;
    target.value = answer.default_target;
  }

  function setLangs(from, to) {
    if ([...source.options].some((o) => o.value === from)) source.value = from;
    if ([...target.options].some((o) => o.value === to)) target.value = to;
  }

  /** «Определить язык · английский» — определённый язык показанной записи. */
  function paintAuto() {
    const auto = source.options[0];
    if (!auto) return;
    const found = rec && rec.source_lang === AUTO && rec.detected_lang;
    const label = found ? `Определить язык · ${langName(rec.detected_lang)}` : 'Определить язык';
    if (auto.textContent !== label) auto.textContent = label;   // опрос раскрытый список не трогает
    swap.disabled = !languages.length || (source.value === AUTO && !(rec && rec.detected_lang));
  }

  /* окна */
  function paintCount() {
    const n = input.value.length;
    count.textContent = `${grouped(n)} / ${grouped(TEXT_MAX)}`;
    count.classList.toggle('error', n > TEXT_MAX);
  }

  /** Левое окно: поле, плашка выбранного файла или исходник открытой записи-файла. */
  function paintLeft() {
    const shown = !pending && rec && rec.kind !== 'text' ? rec : null;
    textBox.box.hidden = !!(pending || shown);
    count.hidden = !!(pending || shown);
    sourceText.hidden = !shown || (shown.kind === 'photo' && photoMode === 'overlay');
    chip.hidden = !(pending || shown);
    const key = pending ? `pending:${pending.photo}:${pending.file.name}`
      : shown ? `record:${shown.id}:${shown.kind}:${shown.media_id}:${shown.name}` : '';
    if (key !== chipKey) {               // опрос той же записи плашку не пересобирает: нажатие и фокус — на месте
      chipKey = key;
      const x = el('button', { type: 'button', id: 'tl-chip-x', class: 'icon-btn', 'aria-label': 'Убрать' }, icon('x'));
      x.addEventListener('click', clearLeft);
      if (pending) {
        chip.replaceChildren(icon(pending.photo ? 'photo' : 'file'), el('span', { class: 'att-name' }, pending.file.name), x);
      } else if (shown) {
        const name = shown.media_id
          ? el('a', { class: 'att-name', href: `/api/media/${encodeURIComponent(shown.media_id)}`, target: '_blank',
            rel: 'noopener' }, shown.name)
          : el('span', { class: 'att-name' }, shown.name);
        chip.replaceChildren(icon(shown.kind === 'photo' ? 'photo' : 'file'), name, x);
      }
    }
    if (shown) {
      const text = shown.source || (RUNNING.has(shown.state) ? 'исходник ещё готовится…' : '');
      if (sourceText.textContent !== text) sourceText.textContent = text;
    }
    paintGo();
  }

  function paintGo() {
    goBtn.disabled = sending || (!pending && !(rec && rec.kind !== 'text') && input.value.length > TEXT_MAX);
  }

  /** Вид записи-снимка над правым окном: новый — на новую запись, та же — новые переводы блоков на слой. */
  function paintPhoto() {
    const want = rec && rec.kind === 'photo' && rec.media_id ? rec : null;
    if (photo && (!want || photo.id !== want.id)) {
      photo.destroy();
      photo.node.remove();
      photo = null;
    }
    if (want && photo) {
      photo.update(want, want.parts);
    } else if (want) {
      photo = { id: want.id, ...photoView(want, want.parts, { mode: photoMode, onMode(mode) {
        photoMode = mode;
        paintLeft();
        paintRight();
      } }) };
      output.before(photo.node);
    }
    output.hidden = !!photo && photoMode === 'overlay';
  }

  /** Состояние над правым окном (kind — '', 'error', 'muted'); то же — без перерисовки: aria-live читает перемены. */
  function setStatus(kind, text, ring = false) {
    const next = `${kind}|${ring}|${text}`;
    if (next === statusKey) return;
    statusKey = next;
    status.className = `tl-status small${kind ? ` ${kind}` : ''}`;
    status.replaceChildren(...(ring ? [el('span', { class: 'ring', 'aria-hidden': 'true' })] : []), ...(text ? [text] : []));
  }

  /** Правое окно: перевод, состояние, кнопки. Перевод растёт — дописывается в конец (выделение на месте). */
  function paintRight() {
    paintPhoto();
    const text = rec ? translation(rec) : loose;
    const was = outText.data;
    if (text !== was) {
      if (was && text.startsWith(was)) outText.appendData(text.slice(was.length));
      else outText.data = text;
      copy.textContent = 'Копировать';
    }
    const running = !!rec && RUNNING.has(rec.state);
    if (!rec) setStatus('', '');
    else if (running) setStatus('', rec.detail || '', true);
    else if (rec.state === 'error') setStatus('error', rec.detail || 'ошибка');
    else if (rec.state === 'cancelled' || !text) setStatus('muted', rec.detail || '');
    else setStatus('', '');
    stop.hidden = !running || rec.state === 'cancelling';
    cont.hidden = !(rec && rec.can_continue) || !!ctx.hint;
    acts.hidden = running || !text;
    exportBtn.hidden = !rec;
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (alive && rec && RUNNING.has(rec.state)) timer = setTimeout(poll, POLL_MS);
  }

  /** Запись в окнах: новая — её языки и исходник слева; та же (опрос, перечитанная) — только перемены. */
  function open(record) {
    const same = !!rec && rec.id === record.id;
    const before = rec;
    rec = record;
    docs.current = record;
    loose = '';
    if (!same) {
      pending = null;
      setLangs(record.source_lang, record.result_lang || record.target_lang);
      if (record.kind === 'text') {
        input.value = record.source || '';
        paintCount();
      }
      error.textContent = '';
      pollWords = '';
      copy.textContent = 'Копировать';
    } else {
      if (pollWords && error.textContent === pollWords) error.textContent = '';   // свежая копия — связь есть
      pollWords = '';
      if (!before.result_lang && record.result_lang) {
        setLangs(source.value, record.result_lang);        // перевод ушёл не на выбранный язык — правый выбор скажет
      }
    }
    paintLeft();
    paintAuto();
    paintRight();
    paintHistory();
    schedulePoll();
    if (same && RUNNING.has(before.state) && !RUNNING.has(record.state)) docs.load();
  }

  async function poll() {
    if (!rec) return;
    const id = rec.id;
    const k = doneHead(rec.parts);
    let got;
    try {
      got = await api('GET', `/api/translate/${encodeURIComponent(id)}?from=${k}`);
    } catch (err) {
      if (alive && rec && rec.id === id) {
        pollWords = words(err);
        error.textContent = pollWords;
        if (err.code !== 'gone') schedulePoll();
      }
      return;
    }
    if (!alive || !rec || rec.id !== id) return;
    const kept = rec.parts.filter((p) => p.n <= k);
    const had = new Map(rec.parts.map((p) => [p.n, p.source]));   // исходник ждущих куски опроса не несут
    const fresh = got.parts.map((p) => (p.source === undefined ? { ...p, source: had.get(p.n) } : p));
    open({ ...got, source: got.source !== undefined ? got.source : rec.source, parts: [...kept, ...fresh] });
  }

  function clearLeft() {
    if (pending) {
      pending = null;
    } else {
      rec = null;
      docs.deselect();                   // перечитывание в пути запись не вернёт
      loose = '';
      input.value = '';
      paintCount();
      paintRight();
      paintAuto();
      paintHistory();
      clearTimeout(timer);
    }
    paintLeft();
    input.focus();
  }

  function download(format) {
    if (!rec) return;
    el('a', { href: `/api/translate/${encodeURIComponent(rec.id)}/export?format=${format}`, download: '' }).click();
  }

  /* перевод */
  async function started(answer) {
    kick();
    picked = true;
    pending = null;
    await docs.load();
    await docs.select(answer.id);
  }

  function upload(file, langs, pick) {
    sending = true;
    paintGo();
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/translate/files');
    try {
      xhr.setRequestHeader('x-studio-csrf', me.csrf);
      xhr.setRequestHeader('x-studio-source-lang', langs.source_lang);
      xhr.setRequestHeader('x-studio-target-lang', langs.target_lang);
      if (pick.connection_id) xhr.setRequestHeader('x-studio-connection', String(pick.connection_id));
      if (pick.model) xhr.setRequestHeader('x-studio-model', pick.model);
    } catch {
      sending = false;
      paintGo();
      error.textContent = 'Имя модели нельзя передать заголовком — выберите другую';
      return;
    }
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) setStatus('', `Загружаю · ${Math.round(event.loaded * 100 / event.total)}%`);
    });
    xhr.addEventListener('loadend', async () => {
      let answer = null;
      try {
        answer = JSON.parse(xhr.responseText);
      } catch {
        // не JSON — ниже словами по коду
      }
      if (xhr.status === 202 && answer) {
        try {
          await started(answer);           // «Перевести» закрыта, пока новая запись не открыта
        } finally {
          sending = false;
          paintGo();
        }
        return;
      }
      sending = false;
      paintRight();
      paintGo();
      if (xhr.status === 401) location.href = '/login';
      const detail = answer && answer.detail;
      if (xhr.status === 423 && detail && detail.code === 'screen_locked' && hooks.locked) {
        await hooks.locked();          // студия заперта (lock.js): тот же файл — после отпирания
        upload(file, langs, pick);
        return;
      }
      error.textContent = words((detail && detail.message) || (xhr.status ? `Ошибка сервера (${xhr.status})` : 'Нет связи с сервером'));
    });
    xhr.send(form);
  }

  async function send(method, path, payload) {
    sending = true;
    paintGo();
    try {
      await started(await api(method, path, payload));
    } catch (err) {
      error.textContent = words(err);
    } finally {
      sending = false;
      paintGo();
    }
  }

  function go() {
    if (sending) return;
    error.textContent = '';
    const langs = { source_lang: source.value, target_lang: target.value };
    const pick = chosen();
    if (pending) {
      upload(pending.file, langs, pick);
    } else if (rec && rec.kind !== 'text') {
      send('POST', `/api/translate/${encodeURIComponent(rec.id)}/repeat`, { ...langs, ...pick });
    } else if (!input.value.trim()) {
      error.textContent = 'Введите текст';
      input.focus();
    } else {
      send('POST', '/api/translate', { text: input.value, ...langs, ...pick });
    }
  }

  function choose(inputNode, photo) {
    const file = inputNode.files[0];
    inputNode.value = '';
    if (!file) return;
    if (file.size > UPLOAD_MAX) {
      error.textContent = 'Файл больше 20 МиБ';
      return;
    }
    error.textContent = '';
    pending = { file, photo };
    paintLeft();
  }

  /* история */
  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'tl-delete-confirm' }, 'Удалить');
    const dialog = openDialog(body, { id: 'tl-delete-dialog', title: 'Удалить перевод?', actions: [yes],
      body: [el('p', {}, 'Удалятся исходник, перевод и исходный файл.'),
        item.scan_id ? el('p', {}, item.scan_shared
          ? 'Распознанный документ останется в «Сканах»: его использует повтор этого перевода.'
          : 'Распознанный документ в «Сканах» удалится вместе с переводом.') : null, fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/translate/${encodeURIComponent(item.id)}`);
        dialog.close();
        const shown = rec && rec.id === item.id;
        docs.forget(item.id);
        if (shown) {
          rec = null;
          clearTimeout(timer);
          paintLeft();
          paintRight();
          paintAuto();
        }
        await docs.load();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  /** Строка под парой языков: время, вид, состояние словами (у идущей — «жду карту · м:сс», каждую секунду новое). */
  function metaText(item) {
    const running = RUNNING.has(item.state);
    const state = running ? item.detail : item.state === 'error' ? 'ошибка' : item.state === 'cancelled' ? 'остановлено' : '';
    return [timeOf(item.created), kindLabel(item), state].filter(Boolean).join(' · ');
  }

  function row(item) {
    const running = RUNNING.has(item.state);
    const from = item.kind === 'file' ? item.name : item.source_head || item.name;
    const meta = el('span', { class: `tl-meta${item.state === 'error' ? ' error' : ''}` }, metaText(item));
    const opener = el('button', { type: 'button', class: 'tl-open' },
      el('span', { class: 'tl-row-top' }, icon(item.kind === 'text' ? 'text' : item.kind === 'photo' ? 'photo' : 'file'),
        el('b', { class: 'tl-pair' }, pair(item)), meta),
      el('span', { class: 'tl-from' }, from || ''),
      item.text_head ? el('span', { class: 'tl-to muted' }, item.text_head) : null);
    opener.addEventListener('click', () => {
      picked = true;
      langsReady.then(() => docs.select(item.id));
    });
    const node = el('div', { class: 'tl-row', 'data-id': item.id, 'data-state': item.state }, opener);   // aria-current — paintHistory
    if (!running) {
      const del = el('button', { type: 'button', class: 'icon-btn tl-delete', 'aria-label': 'Удалить перевод',
        title: 'Удалить' }, icon('x'));
      del.addEventListener('click', () => confirmDelete(item));
      node.append(del);
    }
    return { node, meta };
  }

  /** История: строки с прежними данными и подписи дней — те же узлы, в списке меняется только то, что
   * сменилось (опрос раз в секунду не сбивает нажатие и фокус); слова состояния идущей — на месте, без новой
   * строки; у показанной записи — aria-current. */
  function paintHistory() {
    const { items } = docs;
    const nodes = [];
    const kept = new Map();
    let last = '';
    for (const item of items) {
      const label = dayTitle(item.created);
      if (label !== last) {
        if (!dayNodes.has(label)) dayNodes.set(label, el('p', { class: 'tr-group tl-day' }, label));
        nodes.push(dayNodes.get(label));
        last = label;
      }
      const sig = JSON.stringify({ ...item, detail: undefined });   // слова состояния — ниже, на месте
      const old = rows.get(item.id);
      const r = old && old.sig === sig ? old : { sig, ...row(item) };
      const meta = metaText(item);
      if (r.meta.textContent !== meta) r.meta.textContent = meta;
      kept.set(item.id, r);
      nodes.push(r.node);
    }
    rows = kept;
    for (const [id, { node }] of rows) {
      if (rec && rec.id === id) node.setAttribute('aria-current', 'true');
      else node.removeAttribute('aria-current');
    }
    if (!items.length && listed) nodes.push(empty);
    if (items.length && !docs.done) {
      const label = docs.paging ? 'Загружаю…' : 'Показать ещё';
      more.disabled = docs.paging;
      if (more.textContent !== label) more.textContent = label;
      nodes.push(more);
    }
    const keep = new Set(nodes);
    for (const child of [...list.children]) if (!keep.has(child)) child.remove();
    let at = list.firstChild;
    for (const node of nodes) {
      if (node === at) at = at.nextSibling;
      else list.insertBefore(node, at);
    }
  }

  /* события */
  input.addEventListener('input', () => {
    paintCount();
    paintGo();
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (!ctx.hint) go();
    }
  });
  goBtn.addEventListener('click', go);
  fileInput.addEventListener('change', () => choose(fileInput, false));
  photoInput.addEventListener('change', () => choose(photoInput, true));
  for (const select of [source, target]) {
    select.addEventListener('change', () => {
      touched = true;
      paintAuto();
    });
  }
  swap.addEventListener('click', () => {
    const from = source.value === AUTO ? rec && rec.detected_lang : source.value;
    if (!from) return;
    const to = target.value;
    touched = true;
    setLangs(to, from);
    if (rec && rec.kind === 'text' && rec.state === 'done') {   // окна — текстом: перевод влево, исходник вправо
      const done = translation(rec);
      loose = rec.source || '';
      rec = null;
      docs.deselect();                   // перечитывание в пути запись не вернёт
      clearTimeout(timer);
      input.value = done;
      paintCount();
      paintRight();
      paintHistory();
    } else if (!rec && loose) {          // окна уже поменяны текстом — обратно вместе с языками
      [input.value, loose] = [loose, input.value];
      paintCount();
      paintRight();
    }
    paintAuto();
    paintGo();
  });
  stop.addEventListener('click', async () => {
    if (!rec) return;
    stop.disabled = true;
    try {
      await api('POST', `/api/translate/${encodeURIComponent(rec.id)}/cancel`);
    } catch (err) {
      error.textContent = words(err);
    }
    stop.disabled = false;
    poll();
  });
  cont.addEventListener('click', async () => {
    if (!rec) return;
    const id = rec.id;
    cont.disabled = true;
    try {
      await api('POST', `/api/translate/${encodeURIComponent(id)}/continue`);
      kick();
      await docs.select(id);
      docs.load();
    } catch (err) {
      setStatus('error', words(err));
    }
    cont.disabled = false;
  });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(output.textContent);
      copy.textContent = 'Скопировано';
    } catch {
      error.textContent = 'Буфер обмена недоступен — выделите текст вручную';
    }
  });
  exportBtn.addEventListener('click', () => { formats.hidden = !formats.hidden; });

  paintCount();
  paintLeft();
  paintRight();
  paintHistory();

  const langsReady = loadLanguages();   // запись открывается после языков: она ставит свои

  /** ?id адреса («В работе», уведомление, «Поиск») — при сборке и при переходе внутри раздела (update). */
  function follow(wanted) {
    if (wanted) {
      history.replaceState(null, '', '#/translate');
      picked = false;
    }
    return Promise.all([docs.load(), langsReady]).then(() => { if (wanted && !picked) docs.select(wanted, true); });
  }

  const wanted = ctx.query && ctx.query.get('id');
  Promise.all([loadModels(), follow(wanted)]).then(() => {
    if (!alive) return;
    const last = docs.items[0];
    if (!touched && !rec && !wanted && last) setLangs(last.source_lang, last.target_lang);   // пара прошлого раза
    paintAuto();
  });

  return {
    follow,
    destroy() {
      alive = false;
      clearTimeout(timer);
      docs.stop();
      if (photo) photo.destroy();
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
}

/** Переход внутри открытого раздела (#/translate?id=…): только ?id. */
export function update(ctx) {
  const wanted = ctx.query && ctx.query.get('id');
  if (screen && wanted) screen.follow(wanted);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
