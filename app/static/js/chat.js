/* Раздел «Чат» (#/chat — новый диалог, #/chat/<id> — диалог): список диалогов, чип модели,
 * лента сообщений с markdown, поле ввода с вложениями.
 *
 * Чип модели — отмеченные модели раздела «Чат» по подключениям (GET /api/sections), новый диалог —
 * с умолчанием раздела; списки моделей сервисов при открытии меню не запрашиваются.
 *
 * Ход: вложения — POST /api/attachments по одному, затем POST /api/chats/<id>/turns. Подключение
 * через сервер отвечает потоком SSE (turn, delta, done, error); прямое — JSON с адресом и
 * сообщениями: браузер зовёт модель сам с токеном из /api/connections/<id>/token (токен — только
 * в памяти вкладки) и кладёт итог PUT .../messages/<id>; поток без [DONE] и finish_reason — ошибка
 * с прочитанным текстом, а не готовый ответ; длиннее ANSWER_MAX — как у сервера: дальше не читается,
 * обрезан с пометкой CUT_SIZE и сохранён ошибкой. «Остановить»: через сервер — POST .../stop,
 * прямое — обрыв запроса и PUT со статусом stopped.
 *
 * Ответы, которые читает эта вкладка (`jobs`), живут дольше экрана: ушли в другой диалог или
 * раздел — ответ дочитывается и сохраняется. Пока вкладка читает ответ из браузера, она раз в
 * BEAT_MS шлёт признак жизни (POST .../alive): сервер отдаёт такой ответ с `live` — другая вкладка
 * показывает его идущим и опрашивает диалог, как ответ сервера, который идёт без нас. Ответ
 * `streaming` из браузера без признака жизни (вкладку закрыли) — «прервано».
 * Неотправленное вложение крестик удаляет и с сервера (DELETE /api/attachments/<id>).
 * Раздел без модели (ctx.hint): диалоги читаются, переименовываются и удаляются; новый диалог —
 * подсказка вместо поля ввода, в прежнем поле ввода есть (ход его пару не перепроверяет).
 *
 * Из расшифровки (#/chat?transcript=<id>&ask=…, спека 2026-10-21-studio-transcript-edit-chat-design,
 * раздел 9): новый диалог, POST /api/transcripts/<id>/attachments — вложение встаёт в поле уже
 * загруженным (`meta` без файла), в поле — вопрос по `ask`; адрес — сразу #/chat без записи в историю.
 * Модель — локальная (`local_models` ссылки раздела): умолчание, иначе первая отмеченная, иначе не
 * выбрана, и «Отправить» закрыто до явного выбора. Ход и «Ответить заново», что унесут расшифровку
 * (вложением хода или из истории диалога — признак `external` сервера) нелокальной модели (у открытого
 * диалога — признак `local` сервера), — после окна «уйдёт во внешний сервис» (ход с `external_ok`;
 * 409 `external` сервера — то же окно). Ничего не отправляется само. У отправленного вложения из
 * расшифровки — ссылка «к записи».
 *
 * «Интернет» (спека 2026-10-21-studio-chat-internet, 6.8 и 6.10): кнопка #web-toggle рядом со скрепкой —
 * `web` диалога (PATCH {web}); у нового диалога — в памяти до создания, затем тем же PATCH. Видна, если
 * подключение идёт через сервер (`proxied` диалога; у нового — подключения из чипа); без ключа Exa
 * (`me.exa_key`, оболочка перечитывает профиль при возвращении на экран — событие `studio-me`) выключена, не
 * нажата и с подсказкой WEB_HINT; 409 `no_exa_key` (ключ убрали в другой вкладке) — его слова, кнопка
 * выключена. Событие SSE `tool` — плашка над ответом с крутилкой («🌐 Ищу: …»), ответ с ходом
 * перечитывается: над ним плашка из `tools` (спека 2026-10-22-studio-chat-tools-badge-reasoning, раздел 3) —
 * виды и числа вызовов, зелёная без ошибок и красная с ними, нажатие раскрывает «Источники» под ответом —
 * ссылки в новую вкладку без адреса студии. Ход, чьи
 * запросы к Exa модель составит из расшифровки (`external_web` диалога или новое вложение из неё), — после
 * окна #web-dialog (ход с `web_ok`; 409 `external_web` — то же окно); окно внешней модели — первым.
 *
 * «Рассуждение» (спека 2026-10-22-studio-chat-tools-badge-reasoning, раздел 4): кнопка #think-toggle рядом с
 * «Интернетом» — `thinking` диалога (PATCH {thinking}; у нового — в памяти до создания, затем тем же PATCH).
 * Видна, где сервер её исполняет: `thinking_ok` диалога, у нового — модель из `local_models` подключения чипа
 * (sections.is_local); 409 `no_thinking` — его слова, кнопка прячется. Обе включены — под полем подсказка
 * THINK_NOTE (подсказка «нет ключа» важнее). Событие SSE `think` — модель думает: над ответом «Думает… N с»
 * (счётчик страницы — сумма отрезков от `think` до текста или инструмента; крутится что-то одно: инструмент на
 * плашке или рассуждение) и текст рассуждения, если сервер его шлёт; готовый ответ с `reasoning`
 * — свёрнутое «💭 Рассуждала N с» (`reasoning_sec`). Рассуждение — только текстом, без разметки.
 *
 * Сканы (спека 2026-10-27-studio-ocr, 6.5): ход через сервер ждёт распознавания там — событие SSE `ocr {page, pages}`
 * (своя вкладка) или `ocr` идущего ответа (вкладка, что его не читает) дают плашку «📄 Распознаю скан · стр. N из M»
 * до первого текста модели. Прямое подключение — 409 `not_ready`: под полем NOT_READY, страница опрашивает документы
 * сканов поля ввода и вложений диалога и повторяет ход. #/chat?ocr=<id>&ask=… — как из расшифровки,
 * POST /api/ocr/<id>/attachments, модель — умолчание раздела, без окна внешней модели.
 *
 * Из поиска (#/chat/<id>?m=<id сообщения>): лента открывается не с конца, а с этого сообщения (`found`, `reveal`) —
 * верх у верха ленты и вспышка рамки `.msg-found`; длинные ответы выше дорисовываются кадрами, и место держится,
 * пока человек не тронул ленту. В адресе — только id. */
import { api, me } from './api.js';
import { calendarDay } from './time.js';
import { fieldBox } from './field.js';
import { kick } from './jobs.js';
import { renderMarkdown, renderMarkdownInto } from './markdown.js';
import { CORS } from './settings.js';
import { readSSE } from './sse.js';
import { button, el, field, focusDesktop, openDialog, plural, words } from './ui.js';
import { shareDialog, shareMenu } from './chat-share.js';

const ACCEPT = '.pdf,.docx,.txt,.md,image/*';
const RENDER_MS = 100;           // markdown идущего ответа — не чаще
const THINK_TAIL = 600;          // знаков свёрнутого бегущего рассуждения: в три строки помещается меньше
const POLL_MS = 2000;
const RECOVERY_DELAYS = [1000, 2000, 4000, 8000, 15000];
const BEAT_MS = 15000;           // признак жизни ответа из браузера; сервер ждёт его 90 с (ALIVE_SEC)
const STICK_PX = 48;             // ближе к низу — автопрокрутка не отпускает ленту
const REVEAL_PX = 12;            // сообщение из поиска — на столько ниже верха ленты
const COPIED_MS = 1500;
const CUT_LENGTH = '\n\n[ответ обрезан пределом длины модели]';   // как chat.CUT_LENGTH сервера
const CUT_SIZE = '\n\n[ответ обрезан: слишком длинный для студии]';   // как chat.CUT_SIZE сервера
const ANSWER_MAX = 2000000;      // как chat.ANSWER_MAX: длиннее PUT ответа не примет
const ASKS = {                   // вопрос в поле диалога из расшифровки
  discuss: '',
  summary: 'Составь краткие итоги этой записи: о чём говорили и к чему пришли.',
  tasks: 'Выдели из записи принятые решения и задачи: что сделать, кто отвечает, сроки, если названы.',
};
const SCAN_ASKS = {              // вопрос в поле диалога из «Распознавания»
  discuss: '',
  summary: 'Кратко перескажи этот документ: о чём он и что в нём главное.',
  translate: 'Переведи этот документ на русский',
};
const NOT_READY_MS = 3000;       // прямое подключение: опрос скана, затем ход заново
const SCAN_RUNNING = ['queued', 'working', 'cancelling'];   // как ocr.RUNNING
const NOT_READY = 'скан ещё распознаётся — отправлю, когда будет готов';
const EXTERNAL_NOTE = 'Запись уйдёт во внешний сервис — выберите модель';
const WEB_HINT = 'Задайте ключ Exa в Настройках → Прочее → Интернет';
const WEB_KINDS = ['search', 'page', 'youtube'];   // как web.KINDS: с ключом через сервер — все
const WEB_TITLE = 'Интернет: поиск, страницы и субтитры YouTube';
const WEB_ASK = 'Запросы, составленные из расшифровки, могут уйти в Exa — отправить?';
const TOOL_WORDS = { search: 'Ищу', page: 'Читаю', youtube: 'Субтитры' };   // строка хода по виду инструмента
const THINK_TITLE = 'Рассуждение: модель думает перед ответом, ход мысли виден';
const THINK_NOTE = 'С рассуждением модель реже ищет в интернете';   // замер 2026-10-22: 5 поисков из 10 против 10
const ICONS = {
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/>',
  bulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 00-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0012 3z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  clip: '<path d="M20 11.5l-8.2 8.2a5 5 0 01-7.1-7.1l8.6-8.6a3.4 3.4 0 014.8 4.8l-8.4 8.4a1.8 1.8 0 01-2.6-2.6l7.6-7.6"/>',
  send: '<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  doc: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
};

const jobs = new Map();          // id диалога → ответ, который читает эта вкладка
const tokens = new Map();        // id подключения → { token, url } прямого подключения (память вкладки)
let screen = null;               // смонтированный раздел

function icon(name, cls = 'ico') {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
  return box.firstChild;
}

function iconButton(name, label, attrs = {}) {
  return el('button', { type: 'button', class: 'icon-btn', 'aria-label': label, title: label, ...attrs }, icon(name));
}

function elapsed(since) {
  const s = Math.max(0, Math.floor((Date.now() - since) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function groupOf(updated) {
  const days = calendarDay(Date.now() / 1000).number - calendarDay(updated).number;
  return days <= 0 ? 'Сегодня' : days === 1 ? 'Вчера' : 'Ранее';
}

/** Начало text не длиннее max единиц UTF-16 и без половины эмодзи на краю: одиночный старший
 * суррогат PUT ответа не принял бы (422). */
function clip(text, max) {
  const cut = text.slice(0, max);
  return /[\uD800-\uDBFF]$/.test(cut) ? cut.slice(0, -1) : cut;
}

function notify(job, final) {
  if (screen) screen.refresh(job, final);
}

function finish(job) {
  if (jobs.get(job.chatId) === job) jobs.delete(job.chatId);
  notify(job, true);
}

/* ---- чтение ответа ------------------------------------------------------------ */

/* секунды рассуждения хода — сумма отрезков, как reasoning_sec сервера: идущий — от своего первого `think` */
function thinkSec(job) {
  return (job.thinkSum || 0) + (job.thinking ? (Date.now() - job.thinkFrom) / 1000 : 0);
}

/* модель кончила думать (пошёл текст ответа или инструмент, ответ кончился): секунды — счётчиком страницы до
   перечитывания */
function thinkEnd(job) {
  if (!job.thinking) return;
  job.thinkSum = thinkSec(job);
  job.thinking = false;
  job.message.reasoning_sec = job.thinkSum;
}

function append(job, text) {
  thinkEnd(job);
  job.first = true;
  job.tool = null;                 // модель снова пишет — инструмент кончился
  job.scan = null;                 // и скан распознан
  job.message.content += text;
  notify(job, false);
}

/* плашка скана: сервер шлёт `page` — страниц с итогом, а показываем ту, что распознаётся сейчас */
function scanLine({ page, pages }) {
  return `📄 Распознаю скан · стр. ${Math.min(page + 1, pages)} из ${pages}`;
}

/* строка хода инструмента: «Ищу: запрос»; подпись YouTube сервер начинает словом «субтитры» */
function toolLine({ kind, label }) {
  const what = TOOL_WORDS[kind] || 'Инструмент';
  let text = String(label || '');
  if (kind === 'youtube') text = text.replace(/^субтитры\s+/, '');
  return text ? `${what}: ${text}` : `${what}…`;
}

function adoptIds(job, data) {
  if (job.user) job.user.id = data.user_message_id;
  job.message.id = data.assistant_message_id;
}

async function readServer(job, response) {
  job.message.via = 'server';
  let cut = true;
  try {
    for await (const { event, data } of readSSE(response)) {
      if (event === 'turn') adoptIds(job, data);
      else if (event === 'delta' && data && typeof data.text === 'string') append(job, data.text);
      else if (event === 'replace' && data && typeof data.text === 'string') {
        job.message.content = data.text;
        job.first = true;
        notify(job, false);
      } else if (event === 'cleaner' && data && typeof data.raw === 'string') job.message.cleaner_raw = data.raw;
      else if (event === 'ocr' && data && Number.isFinite(data.page) && Number.isFinite(data.pages)) {
        job.scan = data;                           // ответ ждёт распознавания скана — плашка до первого текста
        notify(job, false);
      } else if (event === 'wait' && data) {       // {seconds} — ждём карту (503 шлюза), {} — шлюз принял запрос
        job.scan = null;
        job.wait = Number.isFinite(data.seconds) ? Date.now() - data.seconds * 1000 : null;
        notify(job, false);
      } else if (event === 'closed') {             // сессия вышла или «Чат» заперт: перечитать через вход/блокировку
        break;
      } else if (event === 'tool' && data && typeof data.kind === 'string') {
        thinkEnd(job);                             // крутится одно: инструмент, а не «Думает…»
        job.scan = null;
        job.tool = data;
        job.tooled = true;                         // по концу — перечитать: источники в ответе с сервера
        (job.message.calls ||= []).push({ kind: data.kind });   // плашка до перечитывания — без итогов шагов
        notify(job, false);
      } else if (event === 'think' && data) {      // {text} — ход мысли виден, {} — модель только думает
        job.tool = null;                           // модель снова думает — инструмент кончился
        job.scan = null;
        if (!job.thinking) job.thinkFrom = Date.now();
        job.thinking = true;
        if (typeof data.text === 'string') job.message.reasoning = (job.message.reasoning || '') + data.text;
        notify(job, false);
      } else if (event === 'done' || event === 'error') {
        job.message.status = event === 'done' ? data.status : 'error';
        if (event === 'error') job.message.error = data.message;
        if (event === 'done' && Number.isFinite(data.reasoning_sec)) job.doneSec = data.reasoning_sec;
        cut = false;
        break;
      }
    }
  } catch {
    // связь оборвалась — ответ на сервере идёт дальше, диалог перечитается
  }
  if (cut) job.lost = true;
  thinkEnd(job);
  // секунды, что записал сервер, — а не часы страницы: иначе «1 с» до перечитывания и «2 с» после
  if (job.doneSec !== undefined) job.thinkSum = job.message.reasoning_sec = job.doneSec;
  finish(job);
}

/* Токен — только тому адресу, для которого сервер его выдал: подключение сменило адрес (здесь,
 * в другой вкладке или у администратора) — прежний токен новому адресу не уходит. */
async function tokenFor(connId, url) {
  const known = tokens.get(connId);
  if (known && known.url === url) return known.token;
  tokens.delete(connId);
  const fresh = await api('GET', `/api/connections/${connId}/token`);
  if (fresh.chat_url !== url) throw new Error('Адрес подключения сменился — отправьте сообщение ещё раз');
  tokens.set(connId, { token: fresh.token, url });
  return fresh.token;
}

async function serviceRefusal(response) {
  let detail = '';
  try {
    const error = (await response.json()).error;
    detail = String((error && error.message) || error || '').slice(0, 300);
  } catch {
    // не JSON — без подробностей
  }
  return `Сервис ответил ошибкой (${response.status})${detail ? `: ${detail}` : ''}`;
}

async function readBrowser(job, turn) {
  job.message.via = 'browser';
  adoptIds(job, turn);
  // признак жизни; ответ уже не идёт (диалог удалили, историю стёрли) — читать его дальше незачем
  const beat = setInterval(() => {
    api('POST', `/api/chats/${job.chatId}/messages/${job.message.id}/alive`).catch((err) => {
      if (err.code === 'gone' || err.code === 'state') job.controller.abort();
    });
  }, BEAT_MS);
  let status = 'done';
  try {
    const token = await tokenFor(job.connId, turn.url);
    let response;
    try {
      response = await fetch(turn.url, { method: 'POST', signal: job.controller.signal,
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: turn.model, messages: turn.messages, stream: true }),
        credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
    } catch (err) {
      throw err instanceof TypeError ? new Error(CORS) : err;
    }
    if (response.status === 401 || response.status === 403) tokens.delete(job.connId);
    if (!response.ok) throw new Error(await serviceRefusal(response));
    // конец ответа — [DONE] или finish_reason; поток без них (обрыв, HTML вместо потока, пустой
    // ответ) — ошибка, а не готовый ответ; `length` — обрезан пределом длины модели
    let ending = null;
    try {
      for await (const { data } of readSSE(response)) {
        if (data === '[DONE]') {
          ending ||= 'stop';
          break;
        }
        if (!data || typeof data !== 'object') continue;
        if (data.error) throw new Error(`Модель прервала ответ: ${String(data.error.message || data.error).slice(0, 300)}`);
        for (const choice of data.choices || []) {
          const text = choice && choice.delta && choice.delta.content;
          if (typeof text === 'string' && text) append(job, text);
          if (choice && typeof choice.finish_reason === 'string' && choice.finish_reason) ending = choice.finish_reason;
        }
        if (job.message.content.length > ANSWER_MAX) {     // как сервер: дальше не читать, прочитанное — ошибкой
          job.message.content = clip(job.message.content, ANSWER_MAX) + CUT_SIZE;
          throw new Error('Ответ слишком длинный — обрезан');
        }
      }
    } catch (err) {
      throw err instanceof TypeError ? new Error('Связь с сервисом оборвалась') : err;
    }
    if (!ending) throw new Error('Модель оборвала ответ: поток кончился без признака конца');
    if (ending === 'length') {
      job.message.content += CUT_LENGTH;
      throw new Error('Ответ обрезан пределом длины модели');
    }
  } catch (err) {
    if (err && err.name === 'AbortError') status = 'stopped';
    else {
      status = 'error';
      job.message.error = words(err);
    }
  }
  clearInterval(beat);
  job.message.status = status;
  try {
    await api('PUT', `/api/chats/${job.chatId}/messages/${job.message.id}`, { content: job.message.content, status });
  } catch (err) {
    if (err.code !== 'state' && err.code !== 'gone') {
      job.message.status = 'error';                 // готовый, но не сохранённый — иначе строка под ним пуста
      job.message.error = `Ответ не сохранился: ${words(err)}`;
    }
  }
  finish(job);
}

/* ---- источники ответа («Интернет») --------------------------------------------- */

function isWebLink(url) {
  try {
    return ['http:', 'https:'].includes(new URL(url).protocol);
  } catch {
    return false;                                       // не адрес — текстом
  }
}

/* ссылка — только http(s) и в новую вкладку без Referer: сайты не узнают адрес студии */
function sourceLink(url, text) {
  const name = text || url || '';
  return isWebLink(url) ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, name)
    : el('span', {}, name);
}

/* страница и ролик с ошибкой — адрес, который составила модель (и, может быть, отвергла студия), —
   текстом, не ссылкой */
function stepLink(s) {
  return s.error ? el('span', {}, s.title || s.url || '') : sourceLink(s.url, s.title);
}

/* «Источники (N)» — число ссылок (найденное поиском, страница, ролик), а не шагов */
function linkCount(steps) {
  const urls = steps.flatMap((s) => {
    if (s.kind === 'search') return (Array.isArray(s.results) ? s.results : []).map((x) => x && x.url);
    return s.error ? [] : [s.url];
  });
  return urls.filter(isWebLink).length;
}

function duration(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${rest}` : `${m}:${rest}`;
}

function stepNode(s) {
  const li = el('li', { class: `web-step web-${s.kind}` });
  if (s.kind === 'search') {
    li.append(s.query ? `Поиск: «${s.query}»` : 'Поиск');
    const results = Array.isArray(s.results) ? s.results : [];
    if (results.length) {
      li.append(el('ul', {}, ...results.map((x) => el('li', {}, sourceLink(x.url, x.title),
        x.published ? el('span', { class: 'muted' }, ` · ${x.published}`) : null))));
    }
  } else if (s.kind === 'page') li.append('Страница: ', stepLink(s));
  else if (s.kind === 'youtube') {
    const about = [s.channel, s.duration ? duration(s.duration) : '', s.lang].filter(Boolean).join(' · ');
    li.append('Ролик: ', stepLink(s), about ? el('span', { class: 'muted' }, ` · ${about}`) : null);
  } else li.append('Инструмент');
  if (s.error) li.append(el('span', { class: 'error' }, ` — ${s.error}`));
  return li;
}

const count = (n, forms) => `${n} ${plural(n, forms)}`;

/* плашка над ответом: «🌐 Интернет (Exa): 2 поиска, 1 страница · ▶ YouTube: 1 ролик — 1 ошибка»; YouTube идёт
   не через Exa — своей частью, вызов несуществующего инструмента — «неверный вызов» */
function badgeText(steps) {
  const of = (kind) => steps.filter((s) => s.kind === kind).length;
  const exa = [];
  if (of('search')) exa.push(count(of('search'), ['поиск', 'поиска', 'поисков']));
  if (of('page')) exa.push(count(of('page'), ['страница', 'страницы', 'страниц']));
  const parts = exa.length ? [`🌐 Интернет (Exa): ${exa.join(', ')}`] : [];
  if (of('youtube')) parts.push(`▶ YouTube: ${count(of('youtube'), ['ролик', 'ролика', 'роликов'])}`);
  const wrong = steps.filter((s) => !WEB_KINDS.includes(s.kind)).length;
  if (wrong) parts.push(count(wrong, ['неверный вызов', 'неверных вызова', 'неверных вызовов']));
  const errors = steps.filter((s) => s.error).length;
  return parts.join(' · ') + (errors ? ` — ${count(errors, ['ошибка', 'ошибки', 'ошибок'])}` : '');
}

function sourcesNode(steps) {
  return el('details', { class: 'web-steps' }, el('summary', {}, `Источники (${linkCount(steps)})`),
    el('ul', { class: 'web-list' }, ...steps.map(stepNode)));
}

/* ---- экран -------------------------------------------------------------------- */

function build(view, ctx) {
  let chatId;                  // показанный диалог; null — новый, ещё не созданный
  let chat = null;             // { chat, messages } с сервера
  let links = [];              // подключения раздела «Чат» с отмеченными моделями (GET /api/sections)
  let chatDefault = null;      // { connection_id, model } — умолчание раздела
  let draft = null;            // { connection_id, model } нового диалога
  let atts = [];               // вложения поля ввода: { file, meta, error, busy, transcript }; из расшифровки — без file
  let needModel = false;       // диалог из расшифровки без локальной модели: «Отправить» — после явного выбора
  let draftWeb = false;        // «Интернет» нового диалога: PATCH после его создания
  let draftThink = false;      // «Рассуждение» нового диалога — так же
  let sending = false;
  let stick = true;
  let found = null;            // { id, lit } сообщение из поиска (?m=): держим на виду, пока человек не тронул ленту
  let opened = 0;              // медленный ответ прежнего диалога не перебьёт новый
  let poll = null;
  let notReady = null;         // повтор хода прямого подключения, пока скан распознаётся (409 `not_ready`)
  let paintTimer = null;
  let lastPaint = 0;
  let menuFor = null;          // пункт списка с открытым меню
  let alive = true;
  let needsReload = false;
  let failures = 0;
  let historyRead = null;
  let pendingTurn = null;      // request id and unsent draft: this view's memory only
  const parts = new WeakMap(); // сообщение → его узлы в ленте
  const unfolded = new Set();  // id ответов, чьё рассуждение человек раскрыл: лента перестраивается целиком

  /* разметка */
  const list = el('nav', { class: 'chat-list', 'aria-label': 'Диалоги' });
  const fresh = el('button', { type: 'button', class: 'btn chat-new', id: 'chat-new' }, icon('plus'), 'Новый чат');
  const aside = el('aside', { id: 'chats', class: 'chats' }, fresh, list);
  const backdrop = el('div', { class: 'chats-backdrop', hidden: true });
  const toggle = iconButton('list', 'Диалоги', { id: 'chats-toggle', class: 'icon-btn chats-toggle', 'aria-controls': 'chats',
    'aria-expanded': 'false' });
  const chipName = el('span', { class: 'chip-name' });
  const chip = el('button', { type: 'button', id: 'model-chip', class: 'model-chip', 'aria-haspopup': 'true',
    'aria-expanded': 'false' }, el('span', { class: 'dot', 'aria-hidden': 'true' }), chipName, icon('down', 'ico caret'));
  const modelMenu = el('div', { id: 'model-menu', class: 'model-menu', hidden: true });
  const connName = el('span', { class: 'chat-conn' });
  const head = el('header', { class: 'head chat-head' }, toggle,
    el('div', { class: 'model-wrap' }, chip, modelMenu), connName);
  const modelNote = el('p', { class: 'chat-model-note error', id: 'model-note', 'aria-live': 'polite' });
  const feed = el('div', { class: 'feed' });
  const scroller = el('div', { id: 'messages', class: 'messages' }, feed);
  const notice = el('p', { class: 'chat-error error', id: 'chat-error', 'aria-live': 'polite' });
  const chipsBox = el('div', { class: 'att-chips' });
  const input = el('textarea', { id: 'chat-input', rows: '1', placeholder: 'Напишите сообщение…', 'aria-label': 'Сообщение',
    maxlength: '200000' });
  const file = el('input', { type: 'file', id: 'attach', class: 'sr', accept: ACCEPT, multiple: true });
  const clip = el('label', { class: 'icon-btn attach', title: 'Прикрепить файл' }, icon('clip'),
    el('span', { class: 'sr' }, 'Прикрепить файл'), file);
  const send = el('button', { type: 'submit', id: 'send', class: 'send composer-go', 'aria-label': 'Отправить',
    title: 'Отправить' }, icon('send'));
  const stop = el('button', { type: 'button', id: 'stop', class: 'send composer-go', 'aria-label': 'Остановить',
    title: 'Остановить', hidden: true }, icon('stop'));
  // на узком экране слово кнопки прячется (.toggle-word), остаётся значок: слово — в aria-label и title
  const webToggle = el('button', { type: 'button', id: 'web-toggle', class: 'chip web-toggle', 'aria-pressed': 'false',
    'aria-label': 'Интернет', title: WEB_TITLE, hidden: true }, icon('globe'), el('span', { class: 'toggle-word' }, 'Интернет'));
  const thinkToggle = el('button', { type: 'button', id: 'think-toggle', class: 'chip think-toggle', 'aria-pressed': 'false',
    'aria-label': 'Рассуждение', title: THINK_TITLE, hidden: true }, icon('bulb'),
  el('span', { class: 'toggle-word' }, 'Рассуждение'));
  const webNote = el('p', { class: 'web-note muted small', id: 'web-note', 'aria-live': 'polite' });
  const inputBox = fieldBox(input);
  const form = el('form', { class: 'composer', id: 'composer' }, chipsBox, inputBox.box, inputBox.error,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, clip, webToggle, thinkToggle), send, stop),
    webNote);
  const main = el('section', { class: 'chat-main' }, head, modelNote, scroller, notice, form);
  const root = el('div', { class: 'chat' }, aside, backdrop, main);
  view.append(root);
  const sharing = shareMenu(root, { id: 'chat-share', failed: (err) => { notice.textContent = words(err); } });
  const share = sharing.button;
  share.hidden = true;
  head.append(share);

  function shareContext() {
    share.hidden = !chatId;
    sharing.set(chatId ? { title: chat?.chat.id === chatId ? chat.chat.title || 'Чат' : 'Чат', share: `/api/chats/${chatId}/share`,
      export: `/api/chats/${chatId}/export` } : null);
  }

  /* выдвижная панель списка (≤ 760px) */
  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список диалогов */
  async function loadList() {
    let items;
    try {
      items = (await api('GET', '/api/chats')).items;
    } catch (err) {
      list.replaceChildren(el('p', { class: 'error small chat-list-note' }, words(err)));
      return;
    }
    if (!items.length) {
      list.replaceChildren(el('p', { class: 'muted small chat-list-note' }, 'Диалогов пока нет'));
      return;
    }
    const nodes = [];
    let group = null;
    for (const item of items) {
      const name = groupOf(item.updated);
      if (name !== group) {
        group = name;
        nodes.push(el('p', { class: 'chat-group' }, name));
      }
      nodes.push(listItem(item));
    }
    list.replaceChildren(...nodes);
  }

  function listItem(item) {
    const link = el('a', { href: `#/chat/${item.id}`, class: 'chat-link', 'aria-current': item.id === chatId ? 'page' : null },
      item.title || 'Новый диалог');
    const more = iconButton('more', 'Действия с диалогом', { class: 'icon-btn chat-more', 'aria-haspopup': 'true' });
    const li = el('div', { class: 'chat-item', 'data-id': item.id }, link, more);
    link.addEventListener('click', () => panel(false));
    more.addEventListener('click', (event) => {
      event.stopPropagation();
      const was = menuFor === li;
      closeItemMenu();
      if (!was) openItemMenu(li, item);
    });
    return li;
  }

  function openItemMenu(li, item) {
    const share = el('button', { type: 'button', class: 'menu-item chat-share' }, 'Поделиться чатом');
    const rename = el('button', { type: 'button', class: 'menu-item chat-rename' }, 'Переименовать');
    const remove = el('button', { type: 'button', class: 'menu-item chat-delete btn-danger' }, 'Удалить');
    rename.addEventListener('click', () => { closeItemMenu(); renameDialog(item); });
    remove.addEventListener('click', () => { closeItemMenu(); deleteDialog(item); });
    share.addEventListener('click', () => { closeItemMenu(); panel(false); shareDialog(root, `/api/chats/${item.id}/share`); });
    li.append(el('div', { class: 'chat-menu', role: 'menu' }, share, rename, remove));
    menuFor = li;
  }

  function closeItemMenu() {
    if (menuFor) menuFor.querySelector('.chat-menu')?.remove();
    menuFor = null;
  }

  function renameDialog(item) {
    const title = el('input', { class: 'input', name: 'title', maxlength: '200', value: item.title || '' });
    const fail = el('p', { class: 'error' });
    const save = button('Сохранить', { class: 'btn btn-accent', id: 'rename-save' });
    const dialog = openDialog(root, { id: 'rename-dialog', title: 'Переименовать диалог',
      body: [field('Название', title, 'title'), fail], actions: [save] });
    const submit = async () => {
      save.disabled = true;
      try {
        const out = await api('PATCH', `/api/chats/${item.id}`, { title: title.value });
        if (chat && chat.chat.id === out.id) chat.chat = out;
        shareContext();
        dialog.close();
        loadList();
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    };
    save.addEventListener('click', submit);
    title.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    });
    title.focus();
    title.select();
  }

  function deleteDialog(item) {
    const fail = el('p', { class: 'error' });
    const confirm = button('Удалить', { class: 'btn btn-danger solid', id: 'chat-delete-confirm' });
    const dialog = openDialog(root, { id: 'chat-delete-dialog', title: 'Удалить диалог?',
      body: [el('p', {}, `«${item.title || 'Новый диалог'}» удалится вместе с вложениями.`), fail], actions: [confirm] });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await api('DELETE', `/api/chats/${item.id}`);
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
        return;
      }
      const job = jobs.get(item.id);
      if (job) job.controller.abort();
      dialog.close();
      if (chatId === item.id) newChat();
      loadList();
    });
  }

  function newChat() {
    panel(false);
    if (location.hash === '#/chat') open(null);
    else ctx.navigate('chat');
  }

  /* чип модели */
  function chosen() {
    if (chat) return { connection_id: chat.chat.connection_id, model: chat.chat.model };
    if (draft) return draft;
    return chatDefault ? { ...chatDefault } : { connection_id: null, model: '' };
  }

  function paintChip() {
    const { connection_id: connId, model } = chosen();
    // ни подключения, ни модели (раздел без модели, подключение диалога удалено) — «по умолчанию» нечему
    const none = connId === null && !model;
    chipName.textContent = none ? 'модель не выбрана' : model || 'модель по умолчанию';
    chip.classList.toggle('off', none);
    const link = links.find((l) => l.connection.id === connId);
    connName.textContent = link ? link.connection.name : '';
    paintGuard();
    paintThink();
    paintWeb();
  }

  /* «Интернет» (как web.tools_for): 'hidden' — прямое подключение, кнопки нет; 'nokey' — через сервер без
   * ключа Exa, кнопка выключена с подсказкой; 'ready' — ход даст модели все инструменты. Путь — `proxied`
   * открытого диалога или подключения из чипа нового, ключ — профиль (перечитывается при возвращении) */
  function webState() {
    if (chat?.chat.cleaner || links.find((l) => l.connection.id === chosen().connection_id)?.connection.cleaner) return 'hidden';
    let proxied;
    if (chat) proxied = !!chat.chat.proxied;
    else {
      const link = links.find((l) => l.connection.id === chosen().connection_id);
      proxied = !!link && !!link.connection.proxied;
    }
    if (!proxied) return 'hidden';
    return me && me.exa_key ? 'ready' : 'nokey';
  }

  function webTools() {
    return webState() === 'ready' ? WEB_KINDS : [];
  }

  function webOn() {
    return webState() === 'ready' && (chat ? !!chat.chat.web : draftWeb);
  }

  function paintWeb() {
    const mode = webState();
    const hint = mode === 'nokey' ? WEB_HINT : '';
    webToggle.hidden = mode === 'hidden';
    webToggle.disabled = mode === 'nokey';
    webToggle.setAttribute('aria-pressed', String(webOn()));
    webToggle.title = hint || WEB_TITLE;
    webNote.textContent = hint || (webOn() && thinkOn() ? THINK_NOTE : '');
  }

  /* 409 `no_exa_key`: ключ убрали в другой вкладке — профиль этой вкладки ещё не знал */
  function noKey(err) {
    if (err.code !== 'no_exa_key') return false;
    if (me) me.exa_key = false;
    notice.textContent = words(err);
    paintWeb();
    return true;
  }

  async function switchWeb() {
    if (!chat) {
      draftWeb = !draftWeb;
      paintWeb();
      return;
    }
    webToggle.disabled = true;
    try {
      chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, { web: !webOn() });
      notice.textContent = '';
    } catch (err) {
      if (!noKey(err)) notice.textContent = words(err);
    } finally {
      webToggle.disabled = false;
    }
    paintWeb();
  }

  /* «Рассуждение» (как chat._thinking_ok сервера): у открытого диалога — `thinking_ok`, у нового — модель чипа
   * локальная (роль `local-*` srvllm через сервер, `local_models` подключения) */
  function thinkOk() {
    return chat ? !!chat.chat.thinking_ok : isLocal(chosen());
  }

  function thinkOn() {
    return thinkOk() && (chat ? !!chat.chat.thinking : draftThink);
  }

  function paintThink() {
    thinkToggle.hidden = !thinkOk();
    thinkToggle.setAttribute('aria-pressed', String(thinkOn()));
  }

  async function switchThink() {
    if (!chat) {
      draftThink = !draftThink;
    } else {
      thinkToggle.disabled = true;
      try {
        chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, { thinking: !thinkOn() });
        notice.textContent = '';
      } catch (err) {
        if (err.code === 'no_thinking') chat.chat.thinking_ok = false;   // модель сменили в другой вкладке
        notice.textContent = words(err);
      } finally {
        thinkToggle.disabled = false;
      }
    }
    paintThink();
    paintWeb();
  }

  /* правило локальной модели для расшифровки: пара нового диалога — из отмеченных (`local_models`
   * ссылки), у открытого диалога — признак сервера `local` (sections.is_local и для модели, которую
   * уже сняли с отметки, и для диалога без модели) */
  function isLocal({ connection_id: connId, model }) {
    const link = links.find((l) => l.connection.id === connId);
    return !!link && (link.local_models || []).includes(model);
  }

  /* ход унесёт расшифровку во внешний сервис без подтверждения: в этом ходе (новое вложение) или в
   * истории открытого диалога (`external` сервера — после смены модели, и для «Ответить заново») */
  function needsConsent(regenerate) {
    const fresh = !regenerate && atts.some((a) => a.transcript);
    if (chat) return !!chat.chat.external || (fresh && !chat.chat.local);
    return fresh && !isLocal(chosen());
  }

  /* ход даст модели Exa, а запросы она составит из расшифровки без подтверждения: в истории (`external_web`
   * сервера; он не считает неотправленное вложение) или в этом ходе (новое вложение из расшифровки) */
  function needsWebConsent(regenerate) {
    if (!webOn() || !webTools().includes('search')) return false;
    const fresh = !regenerate && atts.some((a) => a.transcript);
    return fresh || (!!chat && !!chat.chat.external_web);
  }

  function localPair() {
    if (chatDefault && isLocal(chatDefault)) return { ...chatDefault };
    for (const link of links) {
      const model = link.models.find((m) => (link.local_models || []).includes(m));
      if (model) return { connection_id: link.connection.id, model };
    }
    return null;
  }

  function blocked() {
    return needModel && !chatId && atts.some((a) => a.transcript);
  }

  function paintGuard() {
    modelNote.textContent = blocked() ? EXTERNAL_NOTE : '';
    send.disabled = sending || blocked();
  }

  /* окна подтверждений — по очереди: сначала внешняя модель, затем Exa; каждое несёт уже данное другое */
  function askExternal(regenerate, webOk) {
    const link = links.find((l) => l.connection.id === chosen().connection_id);
    const confirm = button('Отправить', { class: 'btn btn-accent', id: 'external-confirm' });
    const dialog = openDialog(root, { id: 'external-dialog', title: 'Отправить расшифровку?', actions: [confirm],
      body: [el('p', {}, `Расшифровка уйдёт во внешний сервис «${link ? link.connection.name : 'подключение диалога'}» — отправить?`)] });
    confirm.addEventListener('click', () => {
      dialog.close();
      submit(regenerate, true, webOk);
    });
  }

  function askWeb(regenerate, externalOk) {
    const confirm = button('Отправить', { class: 'btn btn-accent', id: 'web-confirm' });
    const dialog = openDialog(root, { id: 'web-dialog', title: 'Расшифровка и интернет', actions: [confirm],
      body: [el('p', {}, WEB_ASK)] });
    confirm.addEventListener('click', () => {
      dialog.close();
      submit(regenerate, externalOk, true);
    });
  }

  async function fromTranscript(tid, ask) {
    history.replaceState(history.state, '', '#/chat');      // перезагрузка не сделает второе вложение
    open(null);
    if (ctx.hint) return;                           // без модели чата новый диалог не начать
    notice.textContent = '';
    input.value = ASKS[ask] || '';
    autosize();
    await sectionsReady;
    draft = localPair();
    needModel = !draft;
    draft ||= { connection_id: null, model: '' };
    paintChip();
    let meta;
    try {
      meta = await api('POST', `/api/transcripts/${encodeURIComponent(tid)}/attachments`);
    } catch (err) {
      notice.textContent = words(err);
      draft = null;                                 // вложения нет — обычный новый диалог: чип — как его создаст сервер
      needModel = false;
      paintChip();
      return;
    }
    atts.push({ file: null, meta, error: '', busy: false, transcript: tid });
    paintAtts();
  }

  /* из «Распознавания» (#/chat?ocr=<id>&ask=…): как из расшифровки, но модель — умолчание раздела и без окна
     внешней модели (спека OCR 6.4: документ свой) */
  async function fromScan(sid, ask) {
    history.replaceState(history.state, '', '#/chat');      // перезагрузка не сделает второе вложение
    open(null);
    if (ctx.hint) return;
    notice.textContent = '';
    input.value = SCAN_ASKS[ask] || '';
    autosize();
    let meta;
    try {
      meta = await api('POST', `/api/ocr/${encodeURIComponent(sid)}/attachments`);
    } catch (err) {
      notice.textContent = words(err);
      return;
    }
    atts.push({ file: null, meta, error: '', busy: false, scan: sid });
    paintAtts();
  }

  function showText(a) {
    const tokens = (a.meta.tokens / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 });
    openDialog(root, { id: 'att-text-dialog', title: a.meta.name, cancelText: 'Закрыть',
      body: [el('p', { class: 'muted small' }, `${a.meta.chars.toLocaleString('ru-RU')} зн. · ~${tokens} тыс. токенов`),
        el('pre', { class: 'att-text' }, a.meta.text)] });
  }

  function openModels(open) {
    modelMenu.hidden = !open;
    chip.setAttribute('aria-expanded', String(open));
    if (!open) return;
    const now = chosen();
    const shown = links.filter((l) => l.models.length);
    if (!shown.length) {
      modelMenu.replaceChildren(el('p', { class: 'muted small' }, 'Нет отмеченных моделей — выберите в настройках раздела «Чат»'));
      return;
    }
    modelMenu.replaceChildren(...shown.map(({ connection: conn, models }) => {
      const group = el('div', { class: 'model-group', 'data-conn': String(conn.id) },
        shown.length > 1 ? el('p', { class: 'model-group-name' }, conn.name) : null);
      for (const id of models) {
        const option = el('button', { type: 'button', class: 'model-option', 'data-conn': String(conn.id), 'data-model': id,
          'aria-current': conn.id === now.connection_id && id === now.model ? 'true' : null }, id);
        option.addEventListener('click', () => choose(conn.id, id));
        group.append(option);
      }
      return group;
    }));
  }

  async function choose(connId, model) {
    openModels(false);
    needModel = false;                             // явный выбор
    if (!chat) {
      draft = { connection_id: connId, model };
      paintChip();
      return;
    }
    try {
      chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, { connection_id: connId, model });
      notice.textContent = '';
    } catch (err) {
      notice.textContent = words(err);
    }
    paintChip();
  }

  /* лента */
  function liveOf(m) {
    const job = jobs.get(chatId);
    return job && m.id && job.message.id === m.id ? job.message : m;
  }

  function reading(m) {
    for (const job of jobs.values()) if (job.message === m) return job;
    return null;
  }

  function attLabel(a) {
    return el('span', { class: 'att' }, icon(a.kind === 'image' ? 'image' : 'doc'), el('b', {}, a.name),
      // идущий скан (`ocr` ответа загрузки или вложения диалога) знаков ещё не знает — их скажет перечитанный диалог
      a.kind === 'document' && !a.ocr ? el('span', { class: 'muted' }, `${Number(a.chars || 0).toLocaleString('ru-RU')} зн.`) : null,
      a.source && a.source.exists ? el('a', { class: 'att-source',
        href: `#/transcribe?id=${encodeURIComponent(a.source.transcript_id)}` }, 'к записи') : null,
      // скан вложения кончился с нераспознанными страницами: тот же документ — снова в очередь, текст — его итог
      a.rescan ? button('Распознать заново', { class: 'btn btn-sm att-rescan' }, async (event) => {
        event.currentTarget.disabled = true;
        try {
          await api('POST', `/api/attachments/${encodeURIComponent(a.id)}/ocr`);
          kick();
          notice.textContent = '';
        } catch (err) {
          notice.textContent = words(err);
        }
        reload();
      }) : null);
  }

  function messageNode(m, last) {
    if (m.role === 'user') {
      const box = el('div', { class: 'msg user', 'data-id': m.id });
      if (m.attachments && m.attachments.length) box.append(el('div', { class: 'atts' }, ...m.attachments.map(attLabel)));
      if (m.content) box.append(el('div', { class: 'bubble' }, m.content));
      return box;
    }
    const md = el('div', { class: 'md' });
    const meta = el('p', { class: 'msg-meta' });
    const actions = el('div', { class: 'msg-actions' });
    const steps = Array.isArray(m.tools) && m.tools.length ? sourcesNode(m.tools) : null;
    const badge = el('button', { type: 'button', class: 'web-badge', hidden: true });
    const think = el('div', { class: 'think-box' });
    badge.addEventListener('click', () => {                    // «Источники» этого ответа — там тексты ошибок
      if (!steps) return;
      steps.open = true;
      steps.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
    // ответ ждёт карту (503 шлюза): «Не ждать» — один узел на сообщение, перерисовка счётчика его не пересоздаёт
    const nowait = button('Не ждать', { class: 'btn btn-sm msg-nowait', hidden: true }, async () => {
      nowait.disabled = true;
      try {
        await api('POST', `/api/chats/${chatId}/nowait`);
      } catch (err) {
        if (err.code !== 'state') notice.textContent = words(err);   // state — уже не ждёт
      } finally {
        nowait.disabled = false;
      }
    });
    const node = el('div', { class: 'msg assistant', 'data-id': m.id }, el('span', { class: 'mark', 'aria-hidden': 'true' }),
      el('div', { class: 'msg-body' }, badge, think, md, steps, meta, nowait, actions));
    parts.set(m, { badge, steps, think, thought: '', said: '', md, meta, nowait, actions, last, shown: null });
    paint(m);
    return node;
  }

  /* плашка: идёт инструмент — он с крутилкой; есть шаги с сервера — итог зелёным или красным; вызовы этого хода
     до перечитывания — итог без цвета (ошибок ещё не знаем); нет ничего — плашки нет (ответ по памяти) */
  function paintBadge(p, m, job) {
    const tools = Array.isArray(m.tools) && m.tools.length ? m.tools : null;
    const steps = tools || m.calls || [];
    // скан: своя вкладка — событие `ocr`, ответ, который читает не она, — `ocr` сообщения с сервера
    const scan = m.status === 'streaming' ? (job ? job.scan : m.ocr) : null;
    const busy = m.status === 'streaming' && job && job.tool;
    p.badge.hidden = !busy && !scan && !steps.length;
    if (p.badge.hidden) return;
    if (busy || scan) {
      p.badge.className = 'web-badge busy';
      p.badge.replaceChildren(el('span', { class: 'spin', 'aria-hidden': 'true' }), busy
        ? `${job.tool.kind === 'youtube' ? '▶' : '🌐'} ${toolLine(job.tool)}` : scanLine(scan));
    } else {
      p.badge.className = `web-badge${tools ? (tools.some((s) => s.error) ? ' bad' : ' ok') : ''}`;
      p.badge.replaceChildren(badgeText(steps));
    }
    p.badge.disabled = !p.steps;
    p.badge.setAttribute('aria-label', p.steps ? `${p.badge.textContent} — показать источники` : p.badge.textContent);
  }

  /* строка рассуждения под плашкой: модель думает — «Думает… N с» с крутилкой и бегущим текстом (если он виден),
     свёрнутым до нескольких строк — нажатие (с клавиатуры — Enter или пробел) раскрывает; ответ с рассуждением —
     свёрнутое «💭 Рассуждала N с». Узел строится заново при смене вида (модель думает и после текста) и вместе с
     лентой (конец ответа, опрос), а раскрытое человеком остаётся раскрытым (`unfolded`). Текст — textContent:
     свёрнутому бегущему — хвост THINK_TAIL знаков, раскрытому — дописывается кусками (`p.said` — что уже в узле): не
     раскладывать заново 60 тыс. знаков каждые RENDER_MS */
  function paintThought(p, m, job) {
    const text = m.reasoning || '';
    const busy = m.status === 'streaming' && (job ? !!job.thinking : !m.content && !!text);
    const mode = busy ? 'busy' : text ? 'done' : '';
    const unfold = (on) => (on ? unfolded.add(m.id) : unfolded.delete(m.id));
    if (mode !== p.thought) {
      p.thought = mode;
      p.said = '';
      if (mode === 'busy') {
        const run = el('div', { class: 'think-text', role: 'button', tabindex: '0' });
        const fold = () => {
          const on = unfolded.has(m.id);
          run.classList.toggle('open', on);
          run.setAttribute('aria-expanded', String(on));
          run.title = on ? 'Свернуть' : 'Показать целиком';
        };
        const flip = () => {
          unfold(!unfolded.has(m.id));
          p.said = '';
          fold();
          paintThought(p, m, reading(m));
        };
        run.addEventListener('click', flip);
        run.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return;
          e.preventDefault();
          flip();
        });
        fold();
        p.think.replaceChildren(el('div', { class: 'think busy' }, el('p', { class: 'think-line' },
          el('span', { class: 'spin', 'aria-hidden': 'true' }), el('span', { class: 'think-sec' })), run));
      } else if (mode === 'done') {
        const details = el('details', { class: 'think' }, el('summary', {}), el('div', { class: 'think-text' }));
        details.open = unfolded.has(m.id);
        details.addEventListener('toggle', () => unfold(details.open));
        p.think.replaceChildren(details);
      } else p.think.replaceChildren();
    }
    if (!mode) return;
    const box = p.think.querySelector('.think-text');
    const folded = busy && !unfolded.has(m.id);
    const shown = folded ? text.slice(-THINK_TAIL) : text;
    if (p.said !== shown) {
      if (p.said && shown.startsWith(p.said) && box.lastChild) box.lastChild.appendData(shown.slice(p.said.length));
      else box.textContent = shown;
      p.said = shown;
      if (folded) box.scrollTop = box.scrollHeight;     // свёрнутый — виден конец
    }
    if (busy) {
      const sec = job ? thinkSec(job) : m.reasoning_sec;
      p.think.querySelector('.think-sec').textContent = `Думает… ${Math.max(0, Math.round(sec || 0))} с`;
    } else p.think.querySelector('summary').textContent = `💭 Рассуждала ${Math.max(1, Math.round(m.reasoning_sec || 0))} с`;
  }

  function paint(m) {
    const p = parts.get(m);
    if (!p) return;
    const job = reading(m);
    paintBadge(p, m, job);
    paintThought(p, m, job);
    const displayed = String((m.cleaner_show_raw ? m.cleaner_raw : m.content) ?? '');
    const final = m.status !== 'streaming';
    if (p.shown !== displayed || p.final !== final) {
      p.shown = displayed;
      p.final = final;
      if (displayed.length <= 2000) p.md.replaceChildren(renderMarkdown(displayed, { highlight: final }));
      else if (!final) p.md.replaceChildren(el('pre', { class: 'code-stream-text' }, displayed));
      else renderMarkdownInto(p.md, displayed, {
        valid: () => p.md.isConnected && parts.get(m) === p && p.shown === displayed && p.final,
        update: (change) => { change(); scrollDown(false); },
      });
    }
    let text = '';
    let kind = '';
    // карта занята (503 шлюза): своя вкладка — событие `wait`, ответ, который читает не она, — `wait` с сервера
    const waiting = m.status === 'streaming' && (job ? job.wait != null : Number.isFinite(m.wait));
    p.nowait.hidden = !waiting;
    if (waiting) text = `жду карту · ${job ? elapsed(job.wait) : elapsed(Date.now() - m.wait * 1000)}`;
    else if (m.status === 'streaming') {
      // инструмент — на плашке, модель думает — строкой рассуждения: модель уже загружена; скан — на плашке
      if (job) text = job.tool || job.scan || job.first || job.thinkFrom ? '' : `загружаю модель · ${elapsed(job.started)}`;
      else if (m.via === 'browser') text = m.live ? 'ответ идёт в другой вкладке…' : 'прервано';
      else text = m.ocr ? '' : 'ответ ещё идёт…';
    } else if (m.status === 'stopped') text = 'остановлено';
    else if (m.status === 'error') {
      text = m.error || (m.content ? 'ответ оборвался' : 'ответ не получен');
      kind = ' error';
    }
    p.meta.textContent = text;
    p.meta.className = `msg-meta${kind}`;
    p.actions.replaceChildren();
    if (m.status === 'streaming' && (job || m.via !== 'browser' || m.live)) return;   // «прервано» можно переспросить
    if (m.content) {
      const copy = button('Копировать', { class: 'btn btn-sm msg-copy' }, async () => {
        try {
          await navigator.clipboard.writeText(displayed);
          copy.textContent = 'Скопировано';
        } catch {
          copy.textContent = 'Не вышло';
        }
        setTimeout(() => { copy.textContent = 'Копировать'; }, COPIED_MS);
      });
      p.actions.append(copy);
    }
    if (m.cleaner_raw != null) p.actions.append(button(m.cleaner_show_raw ? 'Восстановленный ответ' : 'Ответ модели',
      { class: 'btn btn-sm msg-cleaner-view' }, () => { m.cleaner_show_raw = !m.cleaner_show_raw; paint(m); }));
    if (chat?.chat.cleaner && m.content) p.actions.append(button('Очищенная копия',
      { class: 'btn btn-sm msg-cleaner-copy' }, async () => {
        try {
          const result = await api('POST', `/api/chats/${chatId}/cleaner-copy`, { message_id: m.id });
          const area = el('textarea', { class: 'input cleaner-copy-text', readonly: true, rows: '12' });
          area.value = result.text;
          const copy = button('Копировать', { class: 'btn btn-accent' }, async () => {
            try {
              await navigator.clipboard.writeText(result.text);
              copy.textContent = 'Скопировано';
            } catch { copy.textContent = 'Не вышло'; }
          });
          openDialog(root, { id: 'cleaner-copy-dialog', title: 'Очищенная копия', body: [area,
            el('p', { class: 'muted small' }, 'Проверьте текст: распознавание может пропустить чувствительные данные.')],
          actions: [copy], cancelText: 'Закрыть' });
        } catch (err) { notice.textContent = words(err); }
      }));
    if (p.last) p.actions.append(button('Ответить заново', { class: 'btn btn-sm msg-retry' }, () => submit(true)));
  }

  function renderFeed() {
    if (!chat) return;
    const msgs = chat.messages.map(liveOf);
    if (!msgs.length) {
      feed.replaceChildren(emptyNote());
      return;
    }
    feed.replaceChildren(...msgs.map((m, i) => messageNode(m, i === msgs.length - 1)));
  }

  function emptyNote() {
    return el('div', { class: 'empty chat-empty' }, el('span', { class: 'badge', 'aria-hidden': 'true' }, icon('chat')),
      el('p', {}, 'Напишите сообщение или прикрепите документ — PDF, DOCX, TXT, MD или фото'));
  }

  function scrollDown(force) {
    if (force) {
      stick = true;
      found = null;
    }
    if (found) reveal();
    else if (stick) scroller.scrollTop = scroller.scrollHeight;
  }

  /* сообщение из поиска: его верх — у верха ленты. Длинные ответы выше него дорисовываются кадрами (markdown.js) —
     каждая дорисовка зовёт scrollDown, и оно снова ставится на место; первое касание ленты отпускает. Нет такого
     сообщения (удалено, другой диалог) — конец ленты, как обычно. */
  function reveal() {
    const node = [...feed.children].find((n) => n.dataset.id === found.id);
    if (!node) {
      scrollDown(true);
      return;
    }
    stick = false;
    if (!found.lit) {
      found.lit = true;
      node.classList.add('msg-found');
    }
    scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - REVEAL_PX;
  }

  function remoteStream() {
    return !!chat && chat.messages.some((m) => m.role === 'assistant' && m.status === 'streaming' && m.via === 'server'
      && liveOf(m) === m);
  }

  /* ответ из браузера, который читает другая вкладка: остановить его отсюда нельзя, только дождаться */
  function otherTab() {
    return !!chat && chat.messages.some((m) => m.role === 'assistant' && m.status === 'streaming' && m.via === 'browser'
      && m.live && liveOf(m) === m);
  }

  function syncBusy() {
    const busy = jobs.has(chatId) || remoteStream();
    send.hidden = busy;
    stop.hidden = !busy;
    paintGuard();
  }

  function schedulePoll() {
    clearTimeout(poll);
    poll = null;
    if (!alive || document.visibilityState === 'hidden' || navigator.onLine === false) return;
    if (needsReload) {
      if (failures < RECOVERY_DELAYS.length) poll = setTimeout(reload, RECOVERY_DELAYS[failures]);
    } else if ((remoteStream() || otherTab()) && !jobs.has(chatId)) poll = setTimeout(reload, POLL_MS);
  }

  /* отправка принята: она уходит из поля, даже если открыт уже другой диалог (поле общее) — чипы по самим
     объектам, текст — только тот же, что ушёл (набранное заново остаётся) */
  function acknowledgeTurn(attempt) {
    if (pendingTurn === attempt) pendingTurn = null;
    if (attempt.regenerate || attempt.acknowledged) return;
    attempt.acknowledged = true;
    if (input.value === attempt.text) { input.value = ''; autosize(); }
    atts = atts.filter((a) => !attempt.sent.includes(a));
    paintAtts();
  }

  async function reload() {
    if (!alive || !chatId || document.visibilityState === 'hidden') return;
    const id = chatId;
    const mine = opened;
    if (historyRead && historyRead.mine === mine) return;
    const own = { mine, controller: new AbortController() };
    historyRead = own;
    const timeout = setTimeout(() => own.controller.abort(), 15000);
    const attempt = pendingTurn?.id === id && pendingTurn.uncertain ? pendingTurn : null;
    try {
      const options = { signal: own.controller.signal };
      const [data, receipt] = await Promise.all([
        api('GET', `/api/chats/${id}`, undefined, options),
        attempt ? api('GET', `/api/chats/${id}/turns/${attempt.requestId}`, undefined, options) : null,
      ]);
      if (!alive || mine !== opened || chatId !== id) return;
      chat = data;
      shareContext();
      if (receipt?.accepted && data.messages.some((message) => message.id === receipt.assistant_message_id)) {
        acknowledgeTurn(attempt);
      }
      needsReload = !!(pendingTurn?.id === id && pendingTurn.uncertain);
      failures = needsReload ? failures + 1 : 0;
      notice.textContent = '';
    } catch (err) {
      if (!alive || mine !== opened) return;
      notice.textContent = words(err);
      needsReload = err.status !== 404 && err.code !== 'request_gone';
      failures++;
      schedulePoll();
      return;
    } finally {
      clearTimeout(timeout);
      if (historyRead === own) historyRead = null;
    }
    renderFeed();
    paintChip();
    syncBusy();
    schedulePoll();
    scrollDown(false);
  }

  async function open(id) {
    panel(false);
    openModels(false);
    if (!ctx.hint || id !== null) focusDesktop(input);
    if (id === chatId && (id === null || chat)) return;
    const mine = ++opened;
    chatId = id;
    found = null;                                    // ?m= нового адреса ставит query() следом
    shareContext();
    chat = null;
    needsReload = false;
    failures = 0;
    historyRead?.controller.abort();
    historyRead = null;
    draftWeb = false;
    draftThink = false;
    clearTimeout(poll);
    stopWaiting();                                  // ушли из диалога — скан больше не ждём
    notice.textContent = '';
    let listed = false;
    for (const link of list.querySelectorAll('.chat-link')) {
      const here = link.closest('.chat-item').dataset.id === id;
      listed ||= here;
      if (here) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    if (id !== null && !listed) {                    // диалог новее списка (другая вкладка, ссылка)
      firstList.then(() => {                         // первый список раздела ещё мог не прийти
        if (chatId === id && ![...list.querySelectorAll('.chat-item')].some((n) => n.dataset.id === id)) loadList();
      });
    }
    syncBusy();
    paintChip();
    form.hidden = id === null && !!ctx.hint;         // без модели новый диалог не начать; прежний — по правилам сервера
    if (id === null) {
      feed.replaceChildren(ctx.hint ? ctx.hint() : emptyNote());
      return;
    }
    feed.replaceChildren(el('p', { class: 'muted feed-note' }, 'Загружаю диалог…'));
    try {
      const data = await api('GET', `/api/chats/${encodeURIComponent(id)}`);
      if (mine !== opened) return;
      chat = data;
      shareContext();
    } catch (err) {
      if (mine !== opened) return;
      feed.replaceChildren(el('p', { class: 'error feed-note' }, words(err)));
      needsReload = err.status !== 404;
      schedulePoll();
      return;
    }
    renderFeed();
    paintChip();
    syncBusy();
    schedulePoll();
    scrollDown(!found);                              // из поиска — к сообщению, иначе — в конец
  }

  /* поле ввода и вложения */
  function paintAtts() {
    chipsBox.replaceChildren(...atts.map((a) => {
      const name = a.file ? a.file.name : a.meta.name;
      const image = a.file ? a.file.type.startsWith('image/') : a.meta.kind === 'image';
      const remove = iconButton('x', `Убрать ${name}`, { class: 'icon-btn att-remove' });
      remove.addEventListener('click', () => {
        atts = atts.filter((x) => x !== a);
        dropWaiting();
        paintAtts();
        // уже загружено, но не отправлено — и с сервера; отказ не важен: остаток уберёт уборка
        if (a.meta) api('DELETE', `/api/attachments/${a.meta.id}`).catch(() => {});
      });
      let label = el('span', { class: 'att-name' }, name);
      if (a.transcript || a.scan) {                  // весь передаваемый текст — по нажатию
        label = el('button', { type: 'button', class: 'att-name att-open', title: 'Показать текст' }, name);
        label.addEventListener('click', () => showText(a));
      }
      return el('span', { class: `att-chip${a.error ? ' bad' : ''}` }, icon(image ? 'image' : 'doc'), label,
        a.transcript || a.scan ? el('span', { class: 'muted att-state' }, `${a.meta.chars.toLocaleString('ru-RU')} зн.`) : null,
        a.busy ? el('span', { class: 'muted att-state' }, 'загружаю…') : null,
        a.error ? el('span', { class: 'error att-state' }, a.error) : null,
        sending ? null : remove);
    }));
    paintGuard();
  }

  function autosize() {
    input.style.height = 'auto';
    // пустое — в одну строку: подсказка, не влезшая в строку, иначе растянула бы рамку до двух
    if (input.value) input.style.height = `${input.scrollHeight}px`;
  }

  async function upload(a) {
    if (a.meta) return true;
    a.busy = true;
    a.error = '';
    paintAtts();
    try {
      const body = new FormData();
      body.append('file', a.file, a.file.name);
      a.meta = await api('POST', '/api/attachments', body);
      return true;
    } catch (err) {
      a.error = words(err);
      return false;
    } finally {
      a.busy = false;
      paintAtts();
    }
  }

  /* 409 `not_ready`: раз в NOT_READY_MS — документы сканов поля ввода (`ocr` ответа загрузки) и вложений
     сообщений диалога (`ocr` идущего скана, ход с неготовым сканом из истории) — только состояние (`brief`);
     пока какой-то идёт — ждём, затем ход заново (ни одного id — просто ход заново). Смена диалога, уход из
     раздела и «Отправить» человека ожидание снимают; правка поля и вложений — только у обычного хода
     (dropWaiting): по таймеру ушло бы не то, что человек отправлял, а повтор поле и вложения не берёт */
  function waitScans(args) {
    const mine = { timer: 0, regenerate: args[0] };
    notReady = mine;
    const step = async () => {
      const ids = [...atts.map((a) => a.meta && a.meta.ocr && a.meta.ocr.id),
        ...(chat ? chat.messages : []).flatMap((m) => (m.attachments || []).map((a) => a.ocr && a.ocr.id))]
        .filter(Boolean);
      try {
        for (const id of ids) {
          const { state } = await api('GET', `/api/ocr/${encodeURIComponent(id)}?brief=1`);
          if (notReady !== mine) return;
          if (SCAN_RUNNING.includes(state)) {
            mine.timer = setTimeout(step, NOT_READY_MS);
            return;
          }
        }
      } catch {
        // документа нет или сеть — решит ход
      }
      if (notReady === mine) submit(...args);
    };
    mine.timer = setTimeout(step, NOT_READY_MS);
  }

  function stopWaiting() {
    if (notReady) clearTimeout(notReady.timer);
    notReady = null;
  }

  function dropWaiting() {
    if (!notReady || notReady.regenerate) return;
    stopWaiting();
    if (notice.textContent === NOT_READY) notice.textContent = '';
  }

  async function submit(regenerate, externalOk = false, webOk = false) {
    stopWaiting();                                 // ждали скан — ход идёт сейчас
    const text = input.value;
    if (sending || jobs.has(chatId) || (!regenerate && !text.trim() && !atts.length)) return;
    if (!regenerate && blocked()) return;
    if (!externalOk && needsConsent(regenerate)) {
      askExternal(regenerate, webOk);
      return;
    }
    if (!webOk && needsWebConsent(regenerate)) {
      askWeb(regenerate, externalOk);
      return;
    }
    sending = true;
    notice.textContent = '';
    syncBusy();
    paintAtts();
    try {
      await turn(regenerate, text, externalOk, webOk);
    } catch (err) {
      // вкладка не знала (другая вкладка сменила модель, включила «Интернет», ключ Exa появился)
      if (err.code === 'external') askExternal(regenerate, webOk);
      else if (err.code === 'external_web') askWeb(regenerate, externalOk);
      else if (err.code === 'not_ready') {        // прямое подключение: скан ждёт страница
        notice.textContent = NOT_READY;
        waitScans([regenerate, externalOk, webOk]);
      } else if (!noKey(err)) notice.textContent = words(err);
    } finally {
      sending = false;
      syncBusy();
      paintAtts();
    }
  }

  async function turn(regenerate, text, externalOk, webOk) {
    if (!chatId) {
      const created = await api('POST', '/api/chats', draft || {});
      ++opened;
      chatId = created.id;
      chat = { chat: created, messages: [] };
      shareContext();
      draft = null;
      history.replaceState(history.state, '', `#/chat/${created.id}`);
      loadList();
      const think = draftThink && thinkOk();     // «Рассуждение», включённое до создания диалога
      draftThink = false;
      if (think) {
        chat.chat = await api('PATCH', `/api/chats/${created.id}`, { thinking: true });
        paintThink();
      }
      if (draftWeb && webState() === 'ready') {    // «Интернет», включённый до создания диалога
        draftWeb = false;
        chat.chat = await api('PATCH', `/api/chats/${created.id}`, { web: true });
        paintWeb();
      }
    }
    const id = chatId;
    const sent = regenerate ? [] : atts.slice();
    for (const a of sent) {
      if (!(await upload(a))) return;              // отказ — словами у чипа
    }
    if (chatId !== id) return;
    let cleanerTicket;
    if (chat.chat.cleaner) {
      let retry;
      let local = false;
      const rules = [];
      do {
        retry = false;
        notice.textContent = 'Проверяю данные в Cleaner…';
        const shown = await api('POST', `/api/chats/${id}/cleaner-preview`,
          { ...(regenerate ? { regenerate: true } : { text, attachment_ids: sent.map((a) => a.meta.id) }),
            cleaner_rules: rules, cleaner_local: local });
        notice.textContent = '';
        if (chatId !== id) return;
        cleanerTicket = await new Promise((resolve) => {
          const confirm = button(local ? 'Отправить оригиналы локальной модели' : 'Отправить проверенный текст', { class: 'btn btn-accent', id: 'cleaner-confirm',
            disabled: !shown.deliverable });
          const content = el('div', { class: 'cleaner-preview-text' }, ...shown.messages.map((m) =>
            m.role === 'system' ? el('details', {}, el('summary', {}, 'Инструкция модели'), el('pre', {}, m.content))
              : el('section', {}, el('b', {}, m.role === 'user' ? 'Пользователь' : 'Помощник'), el('pre', {}, m.content))));
          const typeNames = { PERSON: 'Люди', ORG: 'Организации', ADDRESS: 'Адреса', EMAIL: 'Почта', PHONE: 'Телефоны',
            BIRTHDATE: 'Даты рождения', PASSPORT: 'Паспорта', INN: 'ИНН', SNILS: 'СНИЛС', OGRN: 'ОГРН', KPP: 'КПП',
            OMS: 'Полисы', CARD: 'Карты', BANK_ACCOUNT: 'Счета', SECRET: 'Секреты', ACCOUNT: 'Учётные записи',
            IP: 'IP', MAC: 'MAC', HOST: 'Узлы', DOMAIN: 'Домены', SID: 'SID', DN: 'LDAP', TERM: 'Термины' };
          const counts = Object.entries(shown.counts || {}).map(([type, count]) => `${typeNames[type] || type}: ${count}`).join(' · ');
          const fragment = el('input', { class: 'input', id: 'cleaner-fragment', maxlength: '2000',
            placeholder: 'Фрагмент исходного текста', autocomplete: 'off' });
          const type = el('select', { class: 'input', id: 'cleaner-type' }, ...Object.entries({
            TERM: 'Термин', PERSON: 'Человек', ORG: 'Организация', SECRET: 'Секрет', EMAIL: 'Почта',
            PHONE: 'Телефон', ADDRESS: 'Адрес', HOST: 'Узел', ACCOUNT: 'Учётная запись',
          }).map(([value, name]) => el('option', { value }, name)));
          const edit = el('details', { id: 'cleaner-edit' }, el('summary', {}, 'Исправить замены'),
            field('Фрагмент', fragment, 'fragment'), field('Тип', type, 'type'));
          let accepted = false;
          for (const [action, label] of [['mask', 'Скрыть и перепроверить'], ['unmask', 'Показать и перепроверить']]) {
            edit.append(button(label, { class: 'btn btn-sm', id: `cleaner-${action}` }, () => {
              if (!fragment.value.trim()) { fragment.focus(); return; }
              rules.push({ action, value: fragment.value, type: type.value });
              retry = true;
              dialog.close();
            }));
          }
          const switchRoute = button(local ? 'Вернуться к облачному предпросмотру' : 'Выбрать локальную модель', { class: 'btn', id: 'cleaner-local' }, () => {
            local = !local;
            retry = true;
            dialog.close();
          });
          const dialog = openDialog(root, { id: 'cleaner-preview-dialog', title: local ? 'Оригиналы для локальной модели' : 'Что получит модель', body: [
            local ? el('p', {}, `Оригиналы получит ${shown.destination.model} (${shown.destination.url}). Проверьте состав перед подтверждением.`)
              : el('p', {}, counts ? `Замены: ${counts}` : 'Автоматических замен нет.'), content, local ? null : edit,
            el('p', { class: 'muted small' }, 'Проверьте текст: распознавание может что-то пропустить. '
              + 'Правки замен действуют только в этом диалоге. Изображения через Cleaner не передаются.'),
            !shown.deliverable ? el('p', { class: 'error' }, 'Отправка заблокирована политикой Cleaner.') : null,
          ].filter(Boolean), actions: [switchRoute, confirm], cancelText: 'Вернуться' });
          confirm.addEventListener('click', () => { accepted = true; dialog.close(); });
          dialog.addEventListener('close', () => resolve(accepted ? shown.ticket : null), { once: true });
        });
      } while (retry && chatId === id);
      if (!cleanerTicket || chatId !== id || (!regenerate && input.value !== text)) return;
    }
    const before = chat.messages;
    const user = regenerate ? null : { id: null, role: 'user', content: text, status: 'done',
      attachments: sent.map((a) => (a.transcript ? { ...a.meta, source: { transcript_id: a.transcript, exists: true } } : a.meta)) };
    const message = { id: null, role: 'assistant', content: '', status: 'streaming', via: null, attachments: [] };
    const job = { chatId: id, connId: chat.chat.connection_id, user, message, started: Date.now(), first: false,
      lost: false, controller: new AbortController() };
    const kept = regenerate && before.length && before[before.length - 1].role === 'assistant' ? before.slice(0, -1) : before;
    chat.messages = [...kept, ...(user ? [user] : []), message];
    jobs.set(id, job);
    renderFeed();
    syncBusy();
    scrollDown(true);
    let reply;
    const signature = JSON.stringify([id, regenerate, text, sent.map((a) => a.meta.id)]);
    if (pendingTurn?.signature !== signature) {
      pendingTurn = { signature, requestId: crypto.randomUUID(), id, regenerate, text, sent };
    }
    const attempt = pendingTurn;
    try {
      reply = await api('POST', `/api/chats/${id}/turns`, {
        request_id: attempt.requestId,
        ...(regenerate ? { regenerate: true } : { text, attachment_ids: sent.map((a) => a.meta.id) }),
        ...(externalOk ? { external_ok: true } : {}), ...(webOk ? { web_ok: true } : {}),
        ...(cleanerTicket ? { cleaner_ticket: cleanerTicket } : {}) });
    } catch (err) {
      jobs.delete(id);
      const uncertain = err.status === 0 || err.name === 'TimeoutError' || err.status === 503;
      if (uncertain) attempt.uncertain = true;
      else if (pendingTurn === attempt) pendingTurn = null;
      if (chatId === id) {
        chat.messages = before;
        renderFeed();
        if (uncertain) { needsReload = true; failures = 0; schedulePoll(); }
      }
      throw err;
    }
    acknowledgeTurn(attempt);
    if (reply.replayed) {
      jobs.delete(id);
      if (chatId === id) { chat.messages = before; needsReload = true; await reload(); }
      return;
    }
    if (chatId === id && externalOk) chat.chat.external = false;   // ответ этой модели — след подтверждения
    if (chatId === id && webOk) chat.chat.external_web = false;    // сервер записал время подтверждения
    loadList();
    if (reply instanceof Response) {
      kick();                                      // ответ через сервер — в «В работе»
      readServer(job, reply);
    }
    else readBrowser(job, reply);
  }

  /* ответ, который читает вкладка: перерисовка не чаще RENDER_MS */
  function refresh(job, final) {
    if (job.chatId !== chatId || !chat) {
      if (final) loadList();
      return;
    }
    if (!parts.has(job.message)) renderFeed();
    if (!final) {
      if (paintTimer) return;
      paintTimer = setTimeout(() => {
        paintTimer = null;
        lastPaint = Date.now();
        paint(job.message);
        scrollDown(false);
      }, Math.max(0, RENDER_MS - (Date.now() - lastPaint)));
      return;
    }
    clearTimeout(paintTimer);
    paintTimer = null;
    renderFeed();                                    // «Ответить заново» — у последнего
    syncBusy();
    scrollDown(false);
    loadList();
    // источники ответа и после ошибки — «Распознать заново» у вложений: с сервера
    if (job.lost || job.tooled || job.message.status === 'error') reload();
  }

  /* события */
  const onDocClick = (event) => {
    if (!modelMenu.hidden && !modelMenu.contains(event.target) && !chip.contains(event.target)) openModels(false);
    if (menuFor && !menuFor.contains(event.target)) closeItemMenu();
  };
  const onKey = (event) => {
    if (event.key !== 'Escape') return;
    if (!modelMenu.hidden) {
      openModels(false);
      chip.focus();
    }
    closeItemMenu();
    panel(false);
  };
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKey);
  document.addEventListener('studio-me', paintWeb);     // профиль перечитан: ключ Exa могли задать или убрать
  const resume = () => {
    if (!alive || document.visibilityState === 'hidden') { clearTimeout(poll); return; }
    if (needsReload || remoteStream() || otherTab()) { failures = 0; reload(); }
  };
  window.addEventListener('online', resume);
  document.addEventListener('visibilitychange', resume);
  const tick = setInterval(() => {
    const job = jobs.get(chatId);
    if (job && (!job.first || job.thinking)) paint(job.message);   // счётчики «загружаю модель» и «Думает…»
  }, 1000);

  fresh.addEventListener('click', newChat);
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  chip.addEventListener('click', () => openModels(modelMenu.hidden));
  webToggle.addEventListener('click', switchWeb);
  thinkToggle.addEventListener('click', switchThink);
  scroller.addEventListener('scroll', () => {
    stick = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - STICK_PX;
  }, { passive: true });
  for (const type of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {   // человек тронул ленту — она его
    scroller.addEventListener(type, () => { found = null; }, { passive: true });
  }
  input.addEventListener('input', () => {
    autosize();
    dropWaiting();
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit(false);
  });
  file.addEventListener('change', () => {
    for (const f of file.files) atts.push({ file: f, meta: null, error: '', busy: false });
    file.value = '';
    dropWaiting();
    paintAtts();
  });
  stop.addEventListener('click', async () => {
    const job = jobs.get(chatId);
    stop.disabled = true;
    try {
      if (job) job.controller.abort();             // прямое подключение: обрыв запроса и PUT stopped
      if (chatId && (!job || job.message.via !== 'browser')) {
        await api('POST', `/api/chats/${chatId}/stop`);
        if (!job) reload();
      }
    } catch (err) {
      notice.textContent = words(err);
    } finally {
      stop.disabled = false;
    }
  });

  const sectionsReady = api('GET', '/api/sections').then(({ sections }) => {
    const section = sections.find((s) => s.id === 'chat');
    links = section.links;
    chatDefault = section.default;
    paintChip();
  }, (err) => { notice.textContent = words(err); });
  const firstList = loadList();

  return {
    open,
    refresh,
    query(query) {                                 // #/chat?transcript=<id>&ask=… или ?ocr=<id>&ask=…
      const tid = query && query.get('transcript');
      const sid = query && query.get('ocr');
      const mid = query && query.get('m');           // #/chat/<id>?m=<сообщение> — из поиска
      found = chatId && mid && /^[\w-]+$/.test(mid) ? { id: mid, lit: false } : null;
      if (found && chat) reveal();                   // диалог уже открыт; иначе — open() по приходе истории
      if (tid) fromTranscript(tid, query.get('ask') || 'discuss');
      else if (sid) fromScan(sid, query.get('ask') || 'discuss');
    },
    destroy() {
      sharing.unmount();
      alive = false;
      historyRead?.controller.abort();
      window.removeEventListener('online', resume);
      document.removeEventListener('visibilitychange', resume);
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('studio-me', paintWeb);
      clearInterval(tick);
      clearTimeout(poll);
      stopWaiting();
      clearTimeout(paintTimer);
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
  screen.open(ctx.id);
  screen.query(ctx.query);
}

export function update(ctx) {
  if (!screen) return;
  screen.open(ctx.id);
  screen.query(ctx.query);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
