import { tr } from './i18n.js';
/* Голосовой ввод в поле (спека 2026-10-20-studio-sections-voice-design, 6.5).
 *
 * attachMic(field, host) → кнопка микрофона: нажал — запись MediaRecorder (iPhone — audio/mp4,
 * Chrome — audio/webm) с таймером в кнопке; нажал ещё раз или прошло MAX_SEC — запись уходит
 * POST /api/dictation (модель раздела «Голосовой ввод»), текст встаёт на место курсора (с пробелом,
 * если нужен) и не отправляется. Курсор — на момент нажатия, а если поле за время записи правили, —
 * нынешнее выделение поля. Звук живёт только в памяти этой записи — после ответа его нет ни
 * здесь, ни на сервере. Нет микрофона, нет разрешения, нет модели — слова в `p.mic-error` (в host);
 * модель смотрится до записи (`me.capabilities.dictation`, false — профиль перечитывается: её могли выбрать в
 * другой вкладке), слова — со ссылкой на #/settings/input. Пока идут запись или распознавание, за микрофоном
 * «×» `.mic-cancel` (и Escape на них): запись отменяется без распознавания, распознавание — обрывом запроса
 * (AbortController; WhisperX на сервере дослушает запись, текст не вставится).
 * Пока браузер спрашивает разрешение, повторные нажатия не считаются.
 *
 * startRecording(maxSec, onTick, owner) → { stop(), done: Promise<Blob | null> } или null — общий
 * рекордер (им же пишется образец голоса в настройках «Озвучки»). Запись одна на всю студию: новая
 * гасит прежнюю. cancelRecording() — отменить её (смена раздела — app.js, шторка и блокировка —
 * lock.js): микрофон отпущен, done — null, звук никуда не уходит. Кнопка `owner` ушла со страницы
 * (закрыли окно, раздел перерисовался) — запись отменяется сама; отменённая до разрешения — null.
 *
 * holdMic(owner) → release() — долгий владелец микрофона: «Разговор» чата (chat-voice.js), запись встречи
 * «Расшифровки» (recorder.js). Пока он держит микрофон, startRecording отказывает словами `owner.words` (диктовка и
 * образец голоса не стартуют). cancelRecording() владельца без `pause` («Разговор») останавливает — `owner.cancel()`;
 * владельца с `pause()` (запись встречи) шторка и блокировка ставят на паузу, а смену раздела
 * (`cancelRecording({ section: true })`, app.js) он переживает; его же нельзя перехватить: holdMic другого — отказ
 * его словами.
 *
 * Язык распознавания (SPEECH_LANGUAGES, «Авто» — без поля): голосовой ввод берёт выбор устройства
 * (`dictationLanguage`, localStorage `studio-dictation-language`, настройки «Ввод и интернет») полем формы `language`. */
import { api, loadMe, me } from './api.js';
import { icon, svg } from './icons.js';
import { el, words } from './ui.js';

export const MAX_SEC = 120;
/** Выбор языка распознавания: id для сервера (`auto` — поле не шлётся) и подпись. */
export const SPEECH_LANGUAGES = [['auto', tr("Авто")], ['ru', tr("Русский")], ['en', 'English']];
const DICTATION_LANGUAGE = 'studio-dictation-language';

/** Язык голосового ввода этого устройства: `ru`, `en` или `auto`. */
export function dictationLanguage() {
  const value = localStorage.getItem(DICTATION_LANGUAGE);
  return SPEECH_LANGUAGES.some(([id]) => id === value) ? value : 'auto';
}

export function setDictationLanguage(value) {
  if (value === 'auto') localStorage.removeItem(DICTATION_LANGUAGE);
  else localStorage.setItem(DICTATION_LANGUAGE, value);
}
export const TYPES = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
const ICON = svg('mic');

let active = null;       // { cancel() } — идущая запись (или ожидание разрешения) студии
let holder = null;       // { cancel(), words, pause? } — долгий владелец микрофона (holdMic)
// перезагрузка оборвала бы запись — автоматическую (refresh.js) идущая запись запрещает
document.addEventListener('studio-before-auto-refresh', (event) => { if (active || holder) event.preventDefault(); });

function clock(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Отменить идущую запись: микрофон отпущен, её done — null; долгий владелец (holdMic) останавливается, а запись
 * встречи (владелец с `pause`) встаёт на паузу. `section` — смена раздела: запись встречи её переживает. */
export function cancelRecording({ section = false } = {}) {
  if (active) active.cancel();
  if (!holder) return;
  if (!holder.pause) holder.cancel();
  else if (!section) holder.pause();
}

/** Микрофон — долгому владельцу `{ cancel(), words, pause? }`: идущая запись отменяется, новые отказывают его
 * словами. Микрофон держит запись встречи — отказ её словами (Error). Итог — release(), чтобы отпустить. */
export function holdMic(owner) {
  if (holder && holder !== owner && holder.pause) throw new Error(holder.words);
  cancelRecording();
  holder = owner;
  return () => { if (holder === owner) holder = null; };
}

/** Запись с микрофона до stop() или maxSec; null — отменили, пока браузер спрашивал разрешение.
 * Отказ — Error со словами. */
export async function startRecording(maxSec, onTick, owner = null) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    throw new Error(tr("Браузер не умеет записывать звук"));
  }
  if (holder) throw new Error(holder.words);
  cancelRecording();
  let cancelled = false;
  const mine = { cancel() { cancelled = true; } };
  active = mine;
  const release = () => { if (active === mine) active = null; };
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    release();
    throw new Error(err && err.name === 'NotAllowedError'
      ? tr("Нет разрешения на микрофон — разрешите его в настройках браузера") : tr("Микрофон недоступен"));
  }
  if (cancelled || (owner && !owner.isConnected)) {
    for (const track of stream.getTracks()) track.stop();
    release();
    return null;
  }
  const type = TYPES.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  const started = Date.now();
  let timer = null;
  const stop = () => { if (recorder.state !== 'inactive') recorder.stop(); };
  mine.cancel = () => { cancelled = true; stop(); };
  recorder.addEventListener('dataavailable', (event) => { if (event.data && event.data.size) chunks.push(event.data); });
  const done = new Promise((resolve) => {
    recorder.addEventListener('stop', () => {
      clearInterval(timer);
      for (const track of stream.getTracks()) track.stop();
      release();
      resolve(cancelled ? null : new Blob(chunks, { type: (recorder.mimeType || type || 'audio/webm').split(';')[0] }));
    });
  });
  timer = setInterval(() => {
    if (owner && !owner.isConnected) {
      mine.cancel();
      return;
    }
    const sec = (Date.now() - started) / 1000;
    if (onTick) onTick(sec);
    if (sec >= maxSec) stop();
  }, 250);
  recorder.start();
  return { stop, done };
}

/** Имя файла записи по её типу: сервер и шлюз узнают формат по нему, если тип потерялся. */
export function recordingName(blob, base = 'voice') {
  const type = blob.type || '';
  return base + (type.includes('mp4') ? '.m4a' : type.includes('ogg') ? '.ogg' : '.webm');
}

function insertAt(field, text, [start, end]) {
  const before = field.value.slice(0, start);
  const after = field.value.slice(end);
  const lead = before && !/\s$/.test(before) ? ' ' : '';
  const tail = after && !/^\s/.test(after) ? ' ' : '';
  field.value = before + lead + text + tail + after;
  const caret = (before + lead + text + tail).length;
  field.focus();
  field.setSelectionRange(caret, caret);
  field.dispatchEvent(new Event('input', { bubbles: true }));
}

/** Модель голосового ввода выбрана (`me.capabilities.dictation`); профиля нет — пусть ответит сервер. */
function dictationReady() {
  return me?.capabilities?.dictation !== false;
}

/* `dialog()` — id диалога или подключение черновика, в чьё поле идёт текст. */
export function attachMic(field, host, { dialog = null } = {}) {
  const button = el('button', { type: 'button', class: 'icon-btn mic', 'aria-label': tr("Голосовой ввод"), title: tr("Голосовой ввод") });
  // «×» рядом с идущей записью или распознаванием: отменить без распознавания (встаёт за микрофоном при первом показе)
  const cancel = el('button', { type: 'button', class: 'icon-btn mic-cancel', hidden: true }, icon('x'));
  const error = el('p', { class: 'mic-error error small', 'aria-live': 'polite' });
  host.append(error);
  let recording = null;
  let starting = false;    // браузер спрашивает разрешение
  let sending = false;
  let controller = null;   // идущее распознавание: «×» обрывает запрос

  function paint(sec) {
    button.classList.toggle('on', !!recording);
    button.classList.toggle('busy', sending || starting);
    button.setAttribute('aria-label', recording ? tr("Остановить запись") : tr("Голосовой ввод"));
    button.innerHTML = ICON;                           // константа выше
    if (recording) button.append(el('span', { class: 'mic-time' }, clock(sec || 0)));
    const busy = !!recording || sending;
    if (busy && !cancel.isConnected && button.isConnected) button.after(cancel);
    cancel.hidden = !busy;
    const label = recording ? tr("Отменить запись") : tr("Отменить распознавание");
    cancel.setAttribute('aria-label', label);
    cancel.title = label;
  }

  /** «×» или Escape: запись — отменить (звук никуда не уходит), распознавание — оборвать запрос. */
  function drop() {
    const focused = document.activeElement === cancel;
    if (recording) cancelRecording();
    else if (controller) controller.abort();
    else return;
    if (focused) button.focus();                       // «×» сейчас спрячется — фокус остаётся у поля значков
  }
  cancel.addEventListener('click', drop);
  for (const node of [button, cancel]) {
    node.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !(recording || sending)) return;
      event.preventDefault();                          // окно, где стоит поле, не закрывается
      event.stopPropagation();
      drop();
    });
  }

  /** Нет модели голосового ввода — слова со ссылкой на настройки, а не две минуты записи впустую. */
  function noModel() {
    error.replaceChildren(tr("Модель голосового ввода не выбрана — "),
      el('a', { href: '#/settings/input' }, tr("открыть настройки")));
  }

  button.addEventListener('click', async () => {
    if (sending || starting) return;
    if (recording) {
      recording.stop();
      return;
    }
    error.textContent = '';
    const caret = [field.selectionStart ?? field.value.length, field.selectionEnd ?? field.value.length];
    const value = field.value;                       // поле правили во время записи — курсор уже другой
    const target = dialog?.();
    const inTarget = () => {
      const current = dialog?.();
      return target?.chat_id === current?.chat_id && target?.connection_id === current?.connection_id;
    };
    starting = true;
    paint();
    if (!dictationReady()) {
      try {
        await loadMe();                              // модель могли выбрать в другой вкладке
      } catch {
        // нет связи — профиль прежний
      }
      if (!dictationReady()) {
        starting = false;
        paint();
        noModel();
        return;
      }
    }
    try {
      recording = await startRecording(MAX_SEC, paint, button);
    } catch (err) {
      error.textContent = words(err);
      return;
    } finally {
      starting = false;
      paint();
    }
    if (!recording) return;                            // отменили, пока спрашивали разрешение
    paint(0);
    const blob = await recording.done;
    recording = null;
    paint();
    if (!blob || !button.isConnected || !inTarget()) return;
    sending = true;
    controller = new AbortController();
    const { signal } = controller;
    paint();
    try {
      const body = new FormData();
      body.append('audio', blob, recordingName(blob));
      const language = dictationLanguage();
      if (language !== 'auto') body.append('language', language);
      for (const [key, value] of Object.entries(target || {})) {
        if (value != null) body.append(key, String(value));
      }
      const { text } = await api('POST', '/api/dictation', body, { signal });
      if (!button.isConnected || !inTarget()) return;
      const at = field.value === value ? caret
        : [field.selectionStart ?? field.value.length, field.selectionEnd ?? field.value.length];
      if (text) insertAt(field, text, at);
      else error.textContent = tr("Ничего не распознано — скажите ещё раз");
    } catch (err) {
      if (!signal.aborted) {
        if (err?.code === 'no_model') noModel();
        else error.textContent = words(err);
      }
    } finally {
      sending = false;
      controller = null;
      paint();
    }
  });
  paint();
  return button;
}
