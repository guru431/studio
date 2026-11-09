/* Голосовой ввод в поле (спека 2026-10-20-studio-sections-voice-design, 6.5).
 *
 * attachMic(field, host) → кнопка микрофона: нажал — запись MediaRecorder (iPhone — audio/mp4,
 * Chrome — audio/webm) с таймером в кнопке; нажал ещё раз или прошло MAX_SEC — запись уходит
 * POST /api/dictation (модель раздела «Голосовой ввод»), текст встаёт на место курсора (с пробелом,
 * если нужен) и не отправляется. Курсор — на момент нажатия, а если поле за время записи правили, —
 * нынешнее выделение поля. Звук живёт только в памяти этой записи — после ответа его нет ни
 * здесь, ни на сервере. Нет микрофона, нет разрешения, нет модели — слова в `p.mic-error` (в host).
 * Пока браузер спрашивает разрешение, повторные нажатия не считаются.
 *
 * startRecording(maxSec, onTick, owner) → { stop(), done: Promise<Blob | null> } или null — общий
 * рекордер (им же пишется образец голоса в настройках «Озвучки»). Запись одна на всю студию: новая
 * гасит прежнюю. cancelRecording() — отменить её (смена раздела — app.js, шторка и блокировка —
 * lock.js): микрофон отпущен, done — null, звук никуда не уходит. Кнопка `owner` ушла со страницы
 * (закрыли окно, раздел перерисовался) — запись отменяется сама; отменённая до разрешения — null. */
import { api } from './api.js';
import { svg } from './icons.js';
import { el, words } from './ui.js';

export const MAX_SEC = 120;
const TYPES = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
const ICON = svg('mic');

let active = null;       // { cancel() } — идущая запись (или ожидание разрешения) студии

function clock(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Отменить идущую запись: микрофон отпущен, её done — null. */
export function cancelRecording() {
  if (active) active.cancel();
}

/** Запись с микрофона до stop() или maxSec; null — отменили, пока браузер спрашивал разрешение.
 * Отказ — Error со словами. */
export async function startRecording(maxSec, onTick, owner = null) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    throw new Error('Браузер не умеет записывать звук');
  }
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
      ? 'Нет разрешения на микрофон — разрешите его в настройках браузера' : 'Микрофон недоступен');
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

export function attachMic(field, host) {
  const button = el('button', { type: 'button', class: 'icon-btn mic', 'aria-label': 'Голосовой ввод', title: 'Голосовой ввод' });
  const error = el('p', { class: 'mic-error error small', 'aria-live': 'polite' });
  host.append(error);
  let recording = null;
  let starting = false;    // браузер спрашивает разрешение
  let sending = false;

  function paint(sec) {
    button.classList.toggle('on', !!recording);
    button.classList.toggle('busy', sending || starting);
    button.setAttribute('aria-label', recording ? 'Остановить запись' : 'Голосовой ввод');
    button.innerHTML = ICON;                           // константа выше
    if (recording) button.append(el('span', { class: 'mic-time' }, clock(sec || 0)));
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
    starting = true;
    paint();
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
    if (!blob || !button.isConnected) return;          // отменена: звук не уходит
    sending = true;
    paint();
    try {
      const body = new FormData();
      body.append('audio', blob, recordingName(blob));
      const { text } = await api('POST', '/api/dictation', body);
      if (!button.isConnected) return;                 // поле уже убрали
      const at = field.value === value ? caret
        : [field.selectionStart ?? field.value.length, field.selectionEnd ?? field.value.length];
      if (text) insertAt(field, text, at);
      else error.textContent = 'Ничего не распознано — скажите ещё раз';
    } catch (err) {
      error.textContent = words(err);
    } finally {
      sending = false;
      paint();
    }
  });
  paint();
  return button;
}
