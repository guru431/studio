import { language, message as serverMessage, tr } from './i18n.js';
/* Раздел «Расшифровка» (#/transcribe) — раскладка Б спеки 2026-10-20 (раздел 6.3).
 *
 * Слева — «+ Загрузить запись» и записи по дням (имя, длительность, состояние), справа — выбранная:
 * длительность, число собеседников, время, «Копировать», «Скачать ▾» (txt, docx, srt), «Удалить»,
 * плеер исходной записи и реплики «Собеседник N · мм:сс». Идущая — состояние словами и «Отменить».
 * На телефоне список выезжает сбоку, как диалоги чата.
 *
 * Загрузка — XMLHttpRequest (ход в процентах над видом записи, `.tr-sending`, и «Отменить загрузку» — xhr.abort()):
 * тело — сама запись, имя — заголовком X-Studio-Name
 * (encodeURIComponent; строка запроса попала бы в журнал Apache); 423 запертой студии (XHR идёт
 * мимо api.js) ждёт отпирания и шлёт ту же запись заново. Состояние загрузки — модуля (transfer): уход из
 * раздела её не обрывает, новый экран показывает ход и отмену. Несколько файлов — по одному (sendAll), пока
 * очередь принимает; «Переименовать» — PATCH `name` (расширение сохраняет сервер). Идущие записи опрашиваются
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
 * «Сбросить правки» — DELETE …/edits. Вне правки нажатие по реплике (если не выделен текст), а в правке и вне
 * её — по её времени (кнопка — и с клавиатуры) ставит плеер на её начало, играющая — aria-current по
 * timeupdate. Плеер играет исходник как есть: не играет (событие error — формат браузера, файла нет) —
 * слова `.tr-unplayable` под ним, нажатие по реплике их повторяет. «В чат ▾» → #/chat?transcript=<id>&ask=…; #/transcribe?id=<id> («Открыть» в «В работе»,
 * ссылка «к записи» из чата) выбирает запись после загрузки списка, если её не выбрали сами (нет её —
 * список как обычно), ?id из адреса уходит; принятая запись зовёт jobs.kick(). Текст сервера — только
 * textContent.
 *
 * Язык распознавания (спека 2026-11-21-studio-source-capabilities-design, п. 26): выбор «Авто / Русский / English»
 * в ряду над видом (picker.js, `#tr-language`) — заголовком X-Studio-Language загрузки («Авто» — без него) и полем
 * окна «Расшифровать ещё раз»; у инструмента MCP язык — его поле. Проверка до загрузки (п. 36): длительность записи —
 * `loadedmetadata` по blob URL, пределы — GET /api/transcripts/limits (у инструмента MCP — его `studio/limits`);
 * длиннее записи целиком — «не подходит» словами и не отправляется, длиннее одного запроса — «будет расшифрована
 * N частями» (`#transcribe-check`). Длительность не прочиталась или пределов нет — решает сервер.
 *
 * «Записать» (п. 38, recorder.js): запись встречи с микрофона рядом с «Загрузить запись» — время, «Пауза» /
 * «Продолжить», «Стоп и расшифровать» (Blob записи уходит тем же путём, что файл, с той же проверкой пределов; её
 * длительность известна сама), «Отменить» (окно: запись стирается). Запись — модуля recorder.js: уход из раздела её не
 * прерывает, новый экран подхватывает её по событию `studio-meeting`. За MEETING_MARGIN до TOTAL_MAX запись встаёт на
 * паузу; проверка до загрузки — с допуском сервера `total_slack`. Записи, оставшиеся в IndexedDB (перезагрузка,
 * закрытая вкладка, отказ загрузки), — «Восстановить запись (мм:сс)», «Скачать запись» (файл с устройства) и
 * «Удалить»; куски стираются, только когда сервер запись принял. На iPhone — строка, что запись встаёт, когда экран
 * гаснет. */
import { api, hooks, me, toLogin } from './api.js';
import { holdRefresh } from './refresh.js';
import { icon } from './icons.js';
import { actionMenu } from './menu.js';
import { NAMES } from './sections-meta.js';
import { dateTime } from './time.js';
import { busyBlock, createPicker, keep, pickerList } from './feed.js';
import { kick } from './jobs.js';
import { sectionMcp } from './mcp-sections.js';
import { modelLabel } from './settings.js';
import { picker } from './picker.js';
import { SPEECH_LANGUAGES, recordingName } from './mic.js';
import { SCREEN_STOPS, discard, meeting, pendingRecordings, restore, startMeeting } from './recorder.js';
import { el, field, openDialog, renameDialog, words } from './ui.js';

const POLL_MS = 3000;
const PAGE = 60;
const UPLOAD_MAX = 200 * 1024 * 1024;
const ACCEPT = 'audio/*,video/*,.m4a,.mp3,.wav,.ogg,.opus,.webm,.flac,.aac';
const RUNNING = new Set(['queued', 'working', 'cancelling']);
const CANCELLED = tr("Загрузка отменена.");
const FORMATS = [['txt', tr("Текст .txt")], ['docx', 'Word .docx'], ['srt', tr("Субтитры .srt")]];
const METADATA_MS = 5000;
const ASKS = [['discuss', tr("Обсудить в чате")], ['summary', tr("Составить итоги")], ['tasks', tr("Выделить решения и задачи")]];

let screen = null;
let shownEntry = null;     // метка записи истории раздела ({ entry: n } — роутер app.js): сюда возвращает hold()
const HOME = '#/transcribe';
/* Загрузка записей — состояние модуля, а не экрана: уход из раздела XHR не обрывает, новый экран раздела
 * подхватывает ход, «Отменить загрузку» и итог. busy — идёт пачка, text — ход словами, abort — отменить идущий
 * файл (null — отменять нечего), note — отказ, случившийся без экрана раздела: его покажет следующий. */
const transfer = { busy: false, checking: false, text: '', abort: null, note: '' };
const TOTAL_MAX = 4 * 3600;     // как transcripts.TOTAL_MAX: длиннее запись целиком не примут
/* запись встречи встаёт на паузу за MEETING_MARGIN с до TOTAL_MAX: время приходит раз в 250 мс, а длительность,
 * измеренная сервером, чуть расходится со временем страницы (сверх предела — допуск transcripts.TOTAL_SLACK) */
const MEETING_MARGIN = 10;
/* Запись встречи (recorder.js) — модуля: note — слова о ней (пауза на пределе), sending — id записей, что уходят
 * сейчас («Восстановить» их не предлагает). */
const recording = { note: '', sending: new Set() };

/** Время записи встречи: у TOTAL_MAX — пауза и слова (длиннее не примут, записанное цело). */
function meetingTick(seconds, paused) {
  if (paused || seconds < TOTAL_MAX - MEETING_MARGIN) return;
  recording.note = tr("Запись достигла {0} — остановите её и расшифруйте", span(TOTAL_MAX));
  meeting()?.pause();
}

/** Имя записи встречи по её началу: «Запись 2026-11-21 14-05» (без «:» — имя файла выгрузки). */
function meetingName(started) {
  const d = new Date(started);
  const two = (x) => String(x).padStart(2, '0');
  return tr("Запись {0}", `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}-${two(d.getMinutes())}`);
}
// перезагрузка стёрла бы несохранённую правку или оборвала пачку загрузок (между файлами holdRefresh не держит) —
// автоматическую (refresh.js) они запрещают
document.addEventListener('studio-before-auto-refresh', (event) => {
  if (screen?.dirty() || transfer.busy || transfer.checking) event.preventDefault();
});

function clock(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Предел словами: целые часы — «4 ч», иначе минуты. */
function span(sec) {
  return sec % 3600 === 0 ? tr("{0} ч", sec / 3600) : tr("{0} мин", Math.round(sec / 60));
}

/** Длительность файла по его метаданным в браузере, с; не прочиталась (формат, webm без длительности) — null. */
function durationOf(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const probe = el('audio', { preload: 'metadata' });
    let timer = null;
    const done = (value) => {
      clearTimeout(timer);
      probe.removeAttribute('src');
      URL.revokeObjectURL(url);
      resolve(Number.isFinite(value) && value > 0 ? value : null);
    };
    probe.addEventListener('loadedmetadata', () => done(probe.duration), { once: true });
    probe.addEventListener('error', () => done(null), { once: true });
    timer = setTimeout(() => done(null), METADATA_MS);
    probe.src = url;
  });
}

const languageOptions = (value) => SPEECH_LANGUAGES.map(([id, label]) => el('option', { value: id, selected: id === value }, label));

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Собеседник реплики: имя пользователя — как есть, метка сервера «Собеседник N» — на языке интерфейса. */
function speakerName(s) {
  return s.speaker && s.speaker === s.label ? serverMessage(s.label) : s.speaker;
}

const turnText = s => s.system ? serverMessage(s.text) : s.text;
const speakerLabels = segments => [...new Set(segments.map(s => s.original_label || s.label).filter(Boolean))];

/** Реплики итога инструмента MCP (`turns`, mcp_sections.turns) видом расшифровки: «Собеседник N · мм:сс» и текст;
 * итог без времени (текст с метками) — без него. Плеера нет: запись осталась у инструмента. */
function mcpTurns(turns) {
  const timed = turns.some((s) => s.start || s.end);
  return el('div', { class: 'tr-turns', id: 'section-mcp-turns' }, ...turns.map((s) => el('div', { class: 'tr-turn' },
    el('p', { class: 'tr-who' }, [s.speaker ? serverMessage(s.speaker) : '', timed ? clock(s.start) : ''].filter(Boolean).join(' · ')),
    el('p', { class: 'tr-text' }, s.text))));
}

/** Текст расшифровки — как выгрузка .txt. */
export function asText(segments) {
  return segments.map((s) => `${s.speaker ? `${speakerName(s)} · ` : ''}${clock(s.start)}\n${turnText(s)}\n`).join('\n');
}

function build(view, ctx) {
  let editing = null;         // черновики текста и назначений собеседников выбранной записи
  let audio = null;            // плеер выбранной записи: перерисовка того же id его не пересоздаёт
  let unplayable = null;       // слова под плеером: браузер запись не играет (событие error)
  let busy = null;             // блок «идёт» выбранной записи (feed.busyBlock): опрос меняет в нём только текст
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

  const input = el('input', { type: 'file', class: 'sr', accept: ACCEPT, multiple: true });
  const add = el('label', { class: 'btn btn-accent tr-add', id: 'transcribe-add', hidden: !!ctx.hint }, icon('plus'), tr("Загрузить запись"), input);
  // ход загрузки и её отмена — над видом записи, а не в списке: на телефоне список спрятан
  const progress = el('p', { id: 'transcribe-progress', class: 'muted small', 'aria-live': 'polite' });
  const stop = el('button', { type: 'button', class: 'btn btn-sm', id: 'transcribe-cancel' }, tr("Отменить загрузку"));
  stop.addEventListener('click', () => transfer.abort?.());
  const sending = el('div', { class: 'tr-sending', hidden: true }, progress, stop);
  const list = el('nav', { class: 'tr-list', 'aria-label': tr("Записи") });
  // без модели — не загрузить: вместо кнопки подсказка над видом записи (на телефоне список спрятан)
  const aside = el('aside', { class: 'split-aside', id: 'tr-aside' }, ...(ctx.hint ? [] : [add]), list);
  const backdrop = el('div', { class: 'split-backdrop', hidden: true });
  const toggle = el('button', { type: 'button', class: 'btn btn-sm split-toggle', 'aria-label': tr("Записи"),
    'aria-controls': 'tr-aside', 'aria-expanded': 'false' }, icon('list'), tr("Записи"));
  const upload = el('button', { type: 'button', class: 'ctl-primary', id: 'transcribe-upload', hidden: !!ctx.hint }, icon('plus'), tr("Загрузить запись"));
  upload.addEventListener('click', () => input.click());
  const record = el('button', { type: 'button', class: 'ctl-chip', id: 'transcribe-record', hidden: !!ctx.hint }, icon('mic'), tr("Записать"));
  let starting = false;        // запись встречи ждёт разрешения на микрофон
  const mcp = sectionMcp('transcribe', { native: !ctx.hint, fields: [], fileRequired: 'artifact_id', onChange: paintMcp, onBusy: paintBusy,
    turns: mcpTurns });
  const toolbar = el('div', { class: 'split-toolbar' }, toggle, mcp.control, ...(ctx.hint ? [] : [upload, record]));
  const title = el('h1', {}, NAMES.transcribe);
  const head = el('header', { class: 'head' }, title);
  const error = el('p', { class: 'error tr-error', 'aria-live': 'polite' });
  const checked = el('p', { class: 'muted small', id: 'transcribe-check', 'aria-live': 'polite' });
  const body = el('div', { class: 'tr-view' });
  // запись встречи: время, «Пауза», «Стоп и расшифровать», «Отменить»; оставшиеся записи — «Восстановить»
  const recTime = el('span', { class: 'rec-time', id: 'rec-time', role: 'timer' });
  const recPause = el('button', { type: 'button', class: 'btn btn-sm', id: 'rec-pause' });
  const recStop = el('button', { type: 'button', class: 'btn btn-sm btn-accent', id: 'rec-stop' }, icon('stop'), tr("Стоп и расшифровать"));
  const recCancel = el('button', { type: 'button', class: 'btn btn-sm', id: 'rec-cancel' }, tr("Отменить"));
  const recNote = el('p', { class: 'muted small', id: 'rec-note', 'aria-live': 'polite' });
  const recPanel = el('div', { class: 'tr-rec', id: 'rec-panel', hidden: true }, recTime, recPause, recStop, recCancel, recNote);
  const restoreBar = el('div', { class: 'tr-rec', id: 'rec-restore-bar', hidden: true });
  const nativeHint = ctx.hint?.('hint-bar');
  const main = el('section', { class: 'split-main' }, head, toolbar, nativeHint, error, checked, recPanel, restoreBar, sending,
    mcp.output, body);
  const root = el('div', { class: 'split transcribe' }, aside, backdrop, main);
  view.append(root);
  // язык загрузки — «как у Агентов» (picker.js) после выбора инструмента MCP
  const langSelect = el('select', { id: 'tr-language' }, ...languageOptions('auto'));
  const languagePick = picker(root, tr("Язык"), langSelect);
  languagePick.wrap.hidden = !!ctx.hint;
  mcp.control.after(languagePick.wrap);
  languagePick.wrap.after(mcp.fields);           // поля инструмента MCP по его схеме: «Протокол», «Говорящих», «Язык»

  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список */
  const paintRows = pickerList(list, { cls: '', moreId: 'tr-more', empty: tr("Здесь появятся ваши записи"),
    name: (item) => item.name,
    state: (item) => (item.state === 'done' ? clock(item.duration)
      : RUNNING.has(item.state) ? serverMessage(item.detail) : item.state === 'cancelled' ? tr("отменено") : tr("ошибка")),
    pick(id) {
      leave(() => {
        panel(false);
        picked = true;
        select(id);
      }, id);
    } });

  function paintList() {
    paintRows(records);
  }

  /* выбранная */
  function download(format) {
    el('a', { href: `/api/transcripts/${records.current.id}/export?format=${format}&lang=${language()}`, download: '' }).click();
  }

  function confirmDelete(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'tr-delete-confirm' }, tr("Удалить"));
    const dialog = openDialog(root, { id: 'tr-delete-dialog', title: tr("Удалить расшифровку?"), actions: [yes],
      body: [el('p', {}, tr("Расшифровка уйдёт в корзину на 14 дней — оттуда её можно вернуть.")), fail] });
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

  async function retryDialog(item) {
    if (root.querySelector('#tr-retry-dialog')) return;
    const model = el('select', { class: 'input', id: 'tr-retry-model', disabled: true });
    const lang = el('select', { class: 'input', id: 'tr-retry-language' }, ...languageOptions(item.language || 'auto'));
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const go = el('button', { type: 'button', class: 'btn btn-accent', id: 'tr-retry-confirm', disabled: true }, tr("Расшифровать запись"));
    const dialog = openDialog(root, { id: 'tr-retry-dialog', title: tr("Расшифровать ещё раз"), actions: [go],
      body: [field(tr("Модель"), model, 'model'), field(tr("Язык"), lang, 'language'), fail] });
    let choices;
    try {
      const { sections } = await api('GET', '/api/sections');
      if (!dialog.open || !dialog.isConnected) return;
      const section = sections.find(s => s.id === 'transcribe');
      choices = section.links.filter(link => link.connection.proxied).flatMap(({ connection, models }) =>
        models.map(id => ({ connection_id: connection.id, model: id, label: `${modelLabel(id)} · ${connection.name}` })));
      if (!choices.length) {
        fail.textContent = tr("Выберите модель в настройках раздела «Расшифровка».");
        return;
      }
      model.append(...choices.map((choice, index) => el('option', { value: String(index) }, choice.label)));
      const selected = choices.findIndex(choice => choice.connection_id === section.default?.connection_id
        && choice.model === section.default?.model);
      model.selectedIndex = Math.max(0, selected);
      model.disabled = go.disabled = false;
    } catch (err) {
      fail.textContent = words(err);
      return;
    }
    go.addEventListener('click', async () => {
      const { connection_id, model: chosen } = choices[model.selectedIndex];
      go.disabled = model.disabled = true;
      fail.textContent = '';
      try {
        const answer = await api('POST', `/api/transcripts/${item.id}/retry`,
          { connection_id, model: chosen, language: lang.value === 'auto' ? null : lang.value });
        kick();
        if (!dialog.open || !dialog.isConnected) return;
        dialog.close();
        picked = true;
        await records.load();
        await select(answer.id);
      } catch (err) {
        fail.textContent = words(err);
        go.disabled = model.disabled = false;
      }
    });
  }

  /* правка */
  function adopt(got) {                              // ответ PATCH и DELETE — как GET
    if (!records.current || records.current.id !== got.id) return;   // пока сохраняли, выбрали другую
    records.current = got;
    paintView();
  }

  function rename(item) {
    renameDialog(root, { id: 'tr-rename-dialog', name: item.name, async save(name) {
      const got = await api('PATCH', `/api/transcripts/${item.id}`, { name });
      for (const x of records.items) if (x.id === got.id) x.name = got.name;
      paintList();
      adopt(got);
    } });
  }

  function confirmReset(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'tr-reset-confirm' }, tr("Сбросить"));
    const dialog = openDialog(root, { id: 'tr-reset-dialog', title: tr("Сбросить правки?"), actions: [yes],
      body: [el('p', {}, tr("Тексты реплик и имена вернутся к исходному распознаванию.")), fail] });
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

  function confirmDropSource(item) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'tr-source-confirm' },
      tr("Удалить исходник"));
    const dialog = openDialog(root, { id: 'tr-source-dialog', title: tr("Удалить исходник?"), actions: [yes],
      body: [el('p', {}, tr("Расшифровка и MP3-копия останутся. Вернуть исходный файл будет нельзя.")), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/transcripts/${item.id}/source`);
        const got = await api('GET', `/api/transcripts/${item.id}`);
        dialog.close();
        adopt(got);
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function namesDialog(item) {
    const labels = speakerLabels(item.segments);
    const inputs = labels.map((label) => el('input', { class: 'input', 'data-label': label, maxlength: '60',
      placeholder: serverMessage(label), 'aria-label': tr("Имя: {0}", serverMessage(label)), value: item.names[label] || '' }));
    const fail = el('p', { class: 'error' });
    const save = el('button', { type: 'button', class: 'btn btn-accent', id: 'tr-names-save' }, tr("Сохранить"));
    const dialog = openDialog(root, { id: 'tr-names-dialog', title: tr("Имена собеседников"), actions: [save],
      body: [...labels.map((label, i) => el('label', { class: 'tr-name-row' }, el('span', {}, `${serverMessage(label)} →`), inputs[i])), fail] });
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

  /** Правка выбранной записи изменила хоть одну реплику и не сохранена. */
  function dirty() {
    const item = records.current;
    // идущее «Сохранить» — уже не потеря: ответ ляжет, только если запись та же (adopt)
    return !!editing && !editing.saving && !!item && editing.id === item.id && !!item.segments
      && ([...editing.drafts].some(([i, text]) => text !== item.segments[i].text)
        || [...editing.speakers].some(([i, label]) => label !== item.segments[i].label));
  }

  /** Уйти от несохранённой правки (другая запись `to`, другой адрес): «Сохранить» (ушла — уйти), «Не сохранять»
   * (правка отброшена) или «Остаться». Нет правки или та же запись — сразу. */
  function leave(go, to = null) {
    if (!dirty() || (to !== null && to === records.current.id)) {
      go();
      return;
    }
    const item = records.current;
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const keep = el('button', { type: 'button', class: 'btn btn-accent', id: 'tr-unsaved-save' }, tr("Сохранить"));
    const drop = el('button', { type: 'button', class: 'btn btn-danger', id: 'tr-unsaved-drop' }, tr("Не сохранять"));
    const dialog = openDialog(root, { id: 'tr-unsaved', title: tr("Изменения не сохранены"), body: [fail],
      actions: [drop, keep], cancelText: tr("Остаться") });
    keep.addEventListener('click', async () => {
      drop.disabled = true;
      const saved = await saveEdits(item, keep);
      drop.disabled = false;
      if (!saved) {
        fail.textContent = error.textContent;
        return;
      }
      dialog.close();
      go();
    });
    drop.addEventListener('click', () => {
      dialog.close();
      editing = null;
      paintView();
      go();
    });
  }

  /** true — правка сохранена (или нечего сохранять); отказ — словами, черновик цел. */
  async function saveEdits(item, save) {
    const texts = {};
    const speakers = {};
    for (const [i, text] of editing.drafts) if (text !== item.segments[i].text) texts[i] = text;
    for (const [i, label] of editing.speakers) if (label !== item.segments[i].label) speakers[i] = label;
    if (!Object.keys(texts).length && !Object.keys(speakers).length) {
      editing = null;
      paintView();
      return true;
    }
    save.disabled = true;
    error.textContent = '';
    const own = editing;
    own.saving = true;
    try {
      const got = await api('PATCH', `/api/transcripts/${item.id}`, { texts, speakers });
      if (editing && editing.id === item.id) editing = null;   // черновик другой записи — не трогать
      adopt(got);
      return true;
    } catch (err) {
      own.saving = false;
      if (records.current && records.current.id === item.id) error.textContent = words(err);   // черновик остаётся
      save.disabled = false;
      return false;
    }
  }

  function grow(area) {
    area.style.height = 'auto';
    area.style.height = `${area.scrollHeight}px`;
  }

  function turnNode(s, i, labels, names) {
    const speaker = s.speaker ? `${speakerName(s)} · ` : '';
    const original = s.edited ? s.original : s.text;
    // время реплики — кнопка: перемотка и с клавиатуры, не только нажатием по реплике; в правке — тоже
    const time = el('button', { type: 'button', class: 'tr-seek', 'aria-label': tr("Слушать с {0}", clock(s.start)) },
      clock(s.start));
    time.addEventListener('click', (event) => {
      event.stopPropagation();
      seek(s.start, true);
    });
    const changed = s.edited || !!s.original_label;
    const who = el('p', { class: 'tr-who' }, speaker, time);
    if (!editing) {
      const node = el('div', { class: 'tr-turn', 'data-index': String(i) }, who,
        el('p', { class: 'tr-text' }, turnText(s)), changed ? el('p', { class: 'tr-mark' }, tr("изменено")) : null);
      node.addEventListener('click', () => seek(s.start));
      return node;
    }
    const area = el('textarea', { class: 'tr-edit-text', rows: '1', 'aria-label': tr("Реплика {0}", i + 1) });
    area.value = editing.drafts.has(i) ? editing.drafts.get(i) : s.text;
    const revert = el('button', { type: 'button', class: 'btn btn-sm tr-revert' }, tr("вернуть"));
    const mark = el('p', { class: 'tr-mark' }, tr("изменено"), revert);
    const select = s.label && labels.length > 1 ? el('select', { class: 'input tr-turn-speaker',
      'aria-label': tr("Собеседник реплики {0}", i + 1) },
    ...labels.map(label => el('option', { value: label }, names[label] || serverMessage(label)))) : null;
    const originalLabel = s.original_label || s.label;
    if (select) {
      select.value = editing.speakers.get(i) || s.label;
      who.replaceChildren(select, time);
      select.addEventListener('change', () => {
        editing.speakers.set(i, select.value);
        paintMark();
      });
    }
    const paintMark = () => { mark.hidden = area.value === original && (!select || select.value === originalLabel); };
    area.addEventListener('input', () => {
      editing.drafts.set(i, area.value);
      grow(area);
      paintMark();
    });
    revert.addEventListener('click', () => {
      area.value = original;
      area.dispatchEvent(new Event('input'));
      if (select) {
        select.value = originalLabel;
        select.dispatchEvent(new Event('change'));
      }
    });
    paintMark();
    return el('div', { class: 'tr-turn editing', 'data-index': String(i) }, who, area, mark);
  }

  /* плеер */
  /** Плеер на начало реплики; `button` — нажата кнопка времени (выделение текста не мешает). */
  function seek(start, button = false) {
    if (!button && String(getSelection() || '')) return;      // выделяют текст — не перематывать
    if (audio.error) {                             // запись не играет (формат браузера, файла нет) — сказать снова
      unplayable.hidden = false;
      return;
    }
    audio.currentTime = start;
    audio.play().catch(() => {});                  // запрет браузера — только перемотка
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

  /** Готовая MP3-копия или исходник старой записи. При недоступности копии — один переход на исходник. */
  function playerOf(item) {
    const preferred = item.listen_media_id || item.source_media_id;
    if (!audio || audio.dataset.id !== item.id || audio.dataset.preferred !== preferred) {
      audio = el('audio', { class: 'tr-audio', controls: true, preload: 'metadata', 'data-id': item.id,
        'data-preferred': preferred, src: `/api/media/${encodeURIComponent(preferred)}` });
      audio.addEventListener('timeupdate', playing);
      unplayable = el('p', { class: 'muted small tr-unplayable', 'aria-live': 'polite', hidden: true },
        tr("Браузер не воспроизводит эту запись — слушать её и перематывать по репликам здесь нельзя. Текст расшифровки доступен."));
      let triedOriginal = false;
      audio.addEventListener('error', (event) => {
        if (event.currentTarget !== audio) return;
        const original = records.current?.source_media_id;
        if (!triedOriginal && original && original !== preferred) {
          triedOriginal = true;
          audio.src = `/api/media/${encodeURIComponent(original)}`;
        } else unplayable.hidden = false;
      });
    }
    return [audio, unplayable];
  }

  function chatMenu(item) {
    return actionMenu(tr("В чат ▾"), { id: 'tr-chat', cls: 'tr-chat-menu', entries: ASKS.map(([ask, label]) => [label,
      { 'data-ask': ask }, () => ctx.navigate(`chat?transcript=${encodeURIComponent(item.id)}&ask=${ask}`)]) });
  }

  function paintView() {
    const item = records.current;
    title.textContent = item ? item.name : tr("Расшифровка");
    if (!item) {
      editing = null;
      busy = null;
      body.replaceChildren(el('p', { class: 'muted tr-empty' }, ctx.hint ? tr("Выберите запись в списке.")
        : tr("Загрузите запись разговора — студия расшифрует её по собеседникам.")));
      return;
    }
    const nodes = [];
    busy = RUNNING.has(item.state) ? busyBlock(busy, item, { id: 'tr-cancel', url: '/api/transcripts',
      fail: (err) => { error.textContent = words(err); }, after: refresh }) : null;
    if (busy) {
      nodes.push(busy.box);
    } else if (item.state !== 'done') {
      nodes.push(el('p', { class: item.state === 'error' ? 'error' : 'muted' }, serverMessage(item.detail)));
    }
    const speakers = item.speakers ? tr("собеседников: {0}", item.speakers) : tr("собеседники не разделены");
    nodes.push(el('p', { class: 'tr-meta muted small' },
      [item.duration ? tr("длительность {0}", clock(item.duration)) : '', item.state === 'done' ? speakers : '', when(item.created)]
        .filter(Boolean).join(' · ')));
    const acts = el('div', { class: 'tr-acts' });
    if (item.state === 'done') {
      const copy = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-copy' }, tr("Копировать"));
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(asText(item.segments));
          copy.textContent = tr("Скопировано");
        } catch {
          error.textContent = tr("Буфер обмена недоступен — выделите текст вручную");
        }
      });
      const formats = actionMenu(tr("Скачать ▾"), { id: 'tr-download', entries: FORMATS.map(([format, label]) => [label,
        { 'data-format': format }, () => download(format)]) });
      // во время правки «Копировать», «Скачать» и «В чат» взяли бы сохранённый текст, а не черновик,
      // и «В чат» молча увёл бы от него — их нет до «Сохранить» или «Отмена»
      if (!editing) acts.append(copy, formats);
      if (item.segments && !editing) {
        const edit = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-edit' }, tr("Править"));
        edit.addEventListener('click', () => {
          editing = { id: item.id, drafts: new Map(), speakers: new Map() };
          paintView();
        });
        acts.append(edit);
      }
      if (item.segments && item.segments.some((s) => s.label)) {
        const names = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-names' }, tr("Имена"));
        names.addEventListener('click', () => namesDialog(item));
        acts.append(names);
      }
      if (item.edited) {
        const reset = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-reset' }, tr("Сбросить правки"));
        reset.addEventListener('click', () => confirmReset(item));
        acts.append(reset);
      }
      if (!editing) acts.append(chatMenu(item));
      if (!editing && item.listen_media_id && item.listen_media_id !== item.source_media_id) {
        const drop = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'tr-source-delete' },
          tr("Удалить исходник"));
        drop.addEventListener('click', () => confirmDropSource(item));
        acts.append(drop);
      }
    }
    if (!editing) {
      const ren = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-rename' }, tr("Переименовать"));
      ren.addEventListener('click', () => rename(item));
      acts.append(ren);
    }
    if (!RUNNING.has(item.state)) {
      if (!editing && item.source_media_id) {
        const repeat = item.state === 'error' || item.state === 'cancelled';
        const retry = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-retry' },
          repeat ? tr("Повторить") : tr("Расшифровать ещё раз"));
        retry.addEventListener('click', async () => {
          if (!repeat) {
            await retryDialog(item);
            return;
          }
          retry.disabled = true;
          error.textContent = '';
          try {
            const got = await api('POST', `/api/transcripts/${item.id}/retry`,
              { connection_id: item.connection_id, model: item.model, language: item.language });
            kick();
            adopt(got);
            await records.load();
          } catch (err) {
            error.textContent = words(err);
          } finally {
            retry.disabled = false;
          }
        });
        acts.append(retry);
      }
      const del = el('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'tr-delete' }, tr("Удалить"));
      del.addEventListener('click', () => confirmDelete(item));
      acts.append(del);
    }
    nodes.push(acts);
    nodes.push(...playerOf(item));
    if (item.segments) {
      const labels = speakerLabels(item.segments);
      nodes.push(el('div', { class: 'tr-turns' }, ...item.segments.map((s, i) => turnNode(s, i, labels, item.names))));
      if (editing) {
        const save = el('button', { type: 'button', class: 'btn btn-accent btn-sm', id: 'tr-save' }, tr("Сохранить"));
        const cancel = el('button', { type: 'button', class: 'btn btn-sm', id: 'tr-edit-cancel' }, tr("Отмена"));
        save.addEventListener('click', () => saveEdits(item, save));
        cancel.addEventListener('click', () => {
          editing = null;
          paintView();
        });
        nodes.push(el('div', { class: 'tr-edit-bar' }, save, cancel));
      }
    }
    keep(body, nodes);
    for (const area of body.querySelectorAll('.tr-edit-text')) grow(area);
    playing();
  }

  /* загрузка */
  function paintMcp() {
    add.hidden = !mcp.control.hidden || !!ctx.hint;
    upload.hidden = !!ctx.hint && !mcp.tool;
    record.hidden = upload.hidden;
    if (nativeHint && mcp.tool) {
      nativeHint.hidden = true;
      if (!upload.parentNode) {
        aside.prepend(add);
        toolbar.append(upload, record);
      }
    }
    input.multiple = !mcp.tool;                    // инструмент MCP берёт один файл
    languagePick.wrap.hidden = !!ctx.hint || !!mcp.tool;
    languagePick.sync();
  }

  function paintBusy() {
    input.disabled = upload.disabled = transfer.busy || mcp.busy || transfer.checking;
    record.disabled = !!meeting() || starting;
  }

  /** Проверка до загрузки: записи, которые можно отправить; «не подходит» — в `error`, «частями» — в `checked`.
   * `seconds` — длительность, известная заранее (запись встречи: webm браузера её не называет). */
  async function fit(files, seconds = null) {
    transfer.checking = true;
    paintBusy();
    try {
      const own = mcp.tool ? mcp.maxSeconds : null;
      const limits = own ? { max_seconds: own, total_max: own }
        : mcp.tool ? null : await api('GET', '/api/transcripts/limits').catch(() => null);
      if (!limits) return files;
      const ok = [], refused = [], parts = [];
      for (const file of files) {
        const sec = seconds ?? await durationOf(file);
        if (sec !== null && sec > limits.total_max + (limits.total_slack || 0)) {   // допуск — как на сервере
          refused.push(tr("«{0}» не подходит: длиннее {1}", file.name, span(limits.total_max)));
          continue;
        }
        ok.push(file);
        const count = sec === null ? 1 : Math.ceil(sec / limits.max_seconds);
        if (count > 1) parts.push(tr("«{0}» будет расшифрована {1} частями", file.name, count));
      }
      error.textContent = refused.join(' ');
      checked.textContent = parts.join(' ');
      return ok;
    } finally {
      transfer.checking = false;
      paintBusy();
      screen?.paintUpload();
    }
  }

  /** Ход загрузки модуля (transfer) — в вид: строка хода, «Отменить загрузку», занятая кнопка; отказ без экрана. */
  function paintUpload() {
    sending.hidden = !transfer.busy;
    progress.textContent = transfer.text;
    stop.hidden = !transfer.abort;
    paintBusy();
    if (transfer.note) {
      error.textContent = transfer.note;
      transfer.note = '';
    }
  }

  /** Проверка и загрузка подходящих (файлы и запись встречи); true — приняты все. */
  async function deliver(files, seconds = null) {
    if (transfer.checking || transfer.busy || mcp.busy) return false;
    error.textContent = checked.textContent = '';
    const tool = mcp.tool, chosen = langSelect.value;
    const ok = await fit(files, seconds);
    if (!ok.length || tool !== mcp.tool) return false;    // пока проверяли, сменили инструмент — выбрать заново
    const sent = tool ? await mcp.run({}, { files: { artifact_id: ok[0] } }) : await sendAll(ok, chosen === 'auto' ? '' : chosen);
    return sent && ok.length === files.length;
  }

  input.addEventListener('change', () => {
    const files = [...input.files];
    input.value = '';
    if (files.length && !input.disabled) deliver(files);
  });

  /* запись встречи */
  function paintMeeting() {
    const rec = meeting();
    recPanel.hidden = !rec;
    paintBusy();
    if (!rec) return;
    recPanel.dataset.state = rec.paused ? 'paused' : 'recording';
    recTime.textContent = `${rec.paused ? tr("Пауза") : tr("Идёт запись")} · ${clock(rec.seconds)}`;
    if (recPause.dataset.paused !== String(rec.paused)) {
      recPause.dataset.paused = String(rec.paused);
      recPause.replaceChildren(icon(rec.paused ? 'mic' : 'pause'), rec.paused ? tr("Продолжить") : tr("Пауза"));
    }
    recNote.textContent = [recording.note, rec.safe ? '' : tr("Запись не сохраняется на устройстве — перезагрузка её сотрёт."),
      SCREEN_STOPS ? tr("Когда экран гаснет, запись встаёт: держите его включённым.") : ''].filter(Boolean).join(' ');
  }

  /** «Скачать запись» с устройства: её можно сохранить, даже если расшифровать её не выйдет (предел инструмента). */
  async function saveRecording(item) {
    let blob;
    try {
      blob = await restore(item.id);
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    const url = URL.createObjectURL(blob);
    el('a', { href: url, download: recordingName(blob, meetingName(item.started)) }).click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);   // сразу — Safari не успевает начать загрузку
  }

  /** Записи, оставшиеся в IndexedDB: «Восстановить запись (мм:сс)», «Скачать запись» и «Удалить». */
  async function paintRestore() {
    const items = await pendingRecordings({ inactiveOnly: true });
    const rec = meeting();
    const left = items.filter((item) => item.id !== rec?.id && !recording.sending.has(item.id));
    restoreBar.hidden = !left.length;
    restoreBar.replaceChildren(...left.map((item) => {
      const back = el('button', { type: 'button', class: 'btn btn-sm btn-accent rec-restore', 'data-id': item.id },
        tr("Восстановить запись ({0})", clock(item.seconds)));
      back.addEventListener('click', () => recover(item));
      const save = el('button', { type: 'button', class: 'btn btn-sm rec-save', 'data-id': item.id }, tr("Скачать запись"));
      save.addEventListener('click', () => saveRecording(item));
      const drop = el('button', { type: 'button', class: 'btn btn-sm btn-danger rec-discard', 'data-id': item.id }, tr("Удалить"));
      drop.addEventListener('click', () => confirmDrop(async () => {
        await discard(item.id);
        await paintRestore();
      }));
      return el('div', { class: 'tr-rec-item' }, el('span', { class: 'muted small' }, tr("Незаконченная запись · {0}", when(item.started))),
        back, save, drop);
    }));
  }

  /** «Отменить» запись и «Удалить» оставшуюся — окном: звук стирается с устройства. */
  function confirmDrop(go) {
    const fail = el('p', { class: 'error' });
    const yes = el('button', { type: 'button', class: 'btn btn-danger solid', id: 'rec-drop-confirm' }, tr("Удалить запись"));
    const dialog = openDialog(root, { id: 'rec-drop-dialog', title: tr("Удалить запись?"), actions: [yes],
      body: [el('p', {}, tr("Записанный звук сотрётся с этого устройства — вернуть его нельзя.")), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await go();
        dialog.close();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  /** Запись встречи — тем же путём, что файл; куски на устройстве стираются, только когда сервер её принял. */
  async function sendMeeting(id, blob, started, seconds) {
    recording.sending.add(id);
    try {
      const file = new File([blob], recordingName(blob, meetingName(started)), { type: blob.type });
      if (await deliver([file], seconds)) await discard(id).catch(() => {});
    } finally {
      recording.sending.delete(id);
      screen?.paintRestore();
    }
  }

  async function recover(item) {
    if (recording.sending.has(item.id)) return;
    let blob;
    try {
      blob = await restore(item.id);
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    await sendMeeting(item.id, blob, item.started, item.seconds);
  }

  record.addEventListener('click', async () => {
    if (meeting() || starting) return;
    error.textContent = '';
    recording.note = '';
    starting = true;
    paintBusy();
    try {
      await startMeeting({ onTick: meetingTick });
    } catch (err) {
      error.textContent = words(err);
    } finally {
      starting = false;
      paintMeeting();
    }
  });
  recPause.addEventListener('click', () => {
    const rec = meeting();
    if (!rec) return;
    if (rec.paused) {
      recording.note = '';
      rec.resume();
    } else {
      rec.pause();
    }
    paintMeeting();
  });
  recStop.addEventListener('click', async () => {
    const rec = meeting();
    if (!rec || recording.sending.has(rec.id)) return;   // уже останавливается
    const seconds = rec.seconds;
    recording.sending.add(rec.id);            // конец записи перерисует «Восстановить» — её там нет
    recording.note = '';
    let blob;
    try {
      blob = await rec.stop();
    } catch (err) {                            // IndexedDB не прочиталась: запись — в «Восстановить» и «Скачать»
      error.textContent = words(err);
      recording.sending.delete(rec.id);
      screen?.paintRestore();
      return;
    }
    await sendMeeting(rec.id, blob, rec.started, seconds);
  });
  recCancel.addEventListener('click', () => {
    const rec = meeting();
    if (rec && !recording.sending.has(rec.id)) confirmDrop(() => rec.cancel());
  });
  /** Время и пауза — каждые 250 мс; конец записи (stop, cancel, сама встала) — и оставшиеся записи. */
  function onMeeting(event) {
    paintMeeting();
    if (!event.detail) paintRestore();
  }
  document.addEventListener('studio-meeting', onMeeting);
  window.addEventListener('focus', paintRestore);
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
  paintUpload();
  paintMeeting();
  paintRestore();
  follow(ctx.query && ctx.query.get('id'));

  /** Закрытие вкладки и ручная перезагрузка с несохранённой правкой — вопрос браузера. */
  function unload(event) {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = '';
  }
  window.addEventListener('beforeunload', unload);

  return {
    follow,
    dirty,
    leave,
    paintUpload,
    paintRestore,
    say(text) { error.textContent = text; },
    /** Запись принята: список, и она выбрана, если несохранённая правка не держит открытую. */
    async accepted(id) {
      picked = true;
      await records.load();
      if (!dirty()) await select(id);
    },
    load: () => records.load(),
    destroy() {
      window.removeEventListener('beforeunload', unload);
      window.removeEventListener('focus', paintRestore);
      document.removeEventListener('studio-meeting', onMeeting);
      mcp.destroy();
      records.stop();
    },
  };
}

/** Отказ загрузки — словами в виде раздела; раздел закрыт — его покажет следующий экран. */
function say(text) {
  if (screen) screen.say(text);
  else transfer.note = text;
}

/** Одна запись; null — принята, строка — отказ словами («Отменить загрузку» — тоже). `label` — «N из M» пачки,
 * `language` — язык распознавания (пусто — «Авто»). */
function send(file, label, language) {
  if (file.size > UPLOAD_MAX) return Promise.resolve(tr("Запись больше 200 МиБ"));
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    let settled = false;
    const show = (percent) => {
      transfer.text = label ? tr("Загружаю запись {0} · {1}%", label, percent) : tr("Загружаю запись · {0}%", percent);
      screen?.paintUpload();
    };
    const finish = (fail) => {
      if (settled) return;
      settled = true;
      transfer.abort = null;
      screen?.paintUpload();
      resolve(fail);
    };
    xhr.open('POST', '/api/transcripts');
    holdRefresh(xhr);              // автоматическая перезагрузка загрузку не оборвёт (refresh.js)
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.setRequestHeader('X-Studio-Name', encodeURIComponent(file.name));
    xhr.setRequestHeader('x-studio-csrf', me.csrf);
    if (language) xhr.setRequestHeader('X-Studio-Language', language);
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) show(Math.round(event.loaded * 100 / event.total));
    });
    xhr.addEventListener('loadend', async () => {
      if (settled) return;           // отменили: ответ уже дан
      transfer.abort = null;
      screen?.paintUpload();
      let answer = null;
      try {
        answer = JSON.parse(xhr.responseText);
      } catch {
        // не JSON — ниже словами по коду
      }
      if (xhr.status === 202 && answer) {
        kick();
        await screen?.accepted(answer.id);
        finish(null);
        return;
      }
      if (xhr.status === 401) toLogin();
      const detail = answer && answer.detail;
      if (xhr.status === 423 && detail && detail.code === 'screen_locked' && hooks.locked) {
        settled = true;
        await hooks.locked();          // студия заперта (lock.js): та же запись — после отпирания
        resolve(await send(file, label, language));
        return;
      }
      finish(words((detail && detail.message) || (xhr.status ? tr("Ошибка сервера ({0})", xhr.status) : tr("Нет связи с сервером"))));
    });
    transfer.abort = () => {
      finish(CANCELLED);           // до abort(): его loadend (синхронный) видит, что ответ уже дан
      xhr.abort();
    };
    show(0);
    xhr.send(file);
  });
}

/** Несколько записей — по одной, пока сервер принимает (очередь transcripts.QUEUE_MAX); первая неудача или
 * «Отменить загрузку» останавливает пачку, не отправленные называются словами. true — приняты все. */
async function sendAll(files, language) {
  transfer.busy = true;
  transfer.note = '';
  let all = true;
  for (const [i, file] of files.entries()) {
    const fail = await send(file, files.length > 1 ? tr("{0} из {1}", i + 1, files.length) : '', language);
    if (fail === null) continue;
    all = false;
    const left = files.slice(i).map((f) => f.name);
    say(files.length > 1 ? `${fail} ${tr("Не отправлены: {0}", left.join(', '))}` : fail);
    if (fail === CANCELLED) screen?.load();        // сервер мог успеть принять запись целиком — список покажет её
    break;
  }
  transfer.busy = false;
  transfer.text = '';
  screen?.paintUpload();
  return all;
}

export function mount(view, ctx) {
  shownEntry = history.state?.entry ?? null;
  screen = build(view, ctx);
}

/** Роутер app.js перед любой сменой адреса (раздел, «Открыть» из «В работе», push, «Назад»): правка не сохранена —
 * адрес назад, вопрос окном; уйти — снова на новый адрес. Как code.js: переход по истории (запись с меткой) не
 * трогает запись, куда ушли, — адрес раздела новой записью, «уйти» — назад на неё. Под блокировкой app.js проверяет
 * раздел, как у «Агентов». true — переход задержан. */
export function hold() {
  if (!screen || !screen.dirty() || location.hash === HOME) return false;
  const target = location.hash;
  if (history.state && history.state.entry) {
    history.pushState({ entry: shownEntry }, '', HOME);
    screen.leave(() => history.back());
  } else {
    history.replaceState({ entry: shownEntry }, '', HOME);
    screen.leave(() => location.replace(target));
  }
  return true;
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
