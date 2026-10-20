/* Голосовой ввод в поле (спека 2026-10-20-studio-sections-voice-design, 6.5).
 *
 * attachMic(field, host) → кнопка микрофона: нажал — запись MediaRecorder (iPhone — audio/mp4,
 * Chrome — audio/webm) с таймером в кнопке; нажал ещё раз или прошло MAX_SEC — запись уходит
 * POST /api/dictation (модель раздела «Голосовой ввод»), текст встаёт на место курсора (с пробелом,
 * если нужен) и не отправляется. Звук живёт только в памяти этой записи — после ответа его нет ни
 * здесь, ни на сервере. Нет микрофона, нет разрешения, нет модели — слова в `p.mic-error` (в host).
 *
 * startRecording(maxSec, onTick) → { stop(), done: Promise<Blob> } — общий рекордер (им же пишется
 * образец голоса в настройках «Озвучки»). */
import { api } from './api.js';
import { el, words } from './ui.js';

export const MAX_SEC = 120;
const TYPES = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
const ICON = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/>'
  + '<path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';

function clock(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Запись с микрофона до stop() или maxSec. Отказ — Error со словами. */
export async function startRecording(maxSec, onTick) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    throw new Error('Браузер не умеет записывать звук');
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    throw new Error(err && err.name === 'NotAllowedError'
      ? 'Нет разрешения на микрофон — разрешите его в настройках браузера' : 'Микрофон недоступен');
  }
  const type = TYPES.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  const started = Date.now();
  let timer = null;
  const stop = () => { if (recorder.state !== 'inactive') recorder.stop(); };
  recorder.addEventListener('dataavailable', (event) => { if (event.data && event.data.size) chunks.push(event.data); });
  const done = new Promise((resolve) => {
    recorder.addEventListener('stop', () => {
      clearInterval(timer);
      for (const track of stream.getTracks()) track.stop();
      resolve(new Blob(chunks, { type: (recorder.mimeType || type || 'audio/webm').split(';')[0] }));
    });
  });
  timer = setInterval(() => {
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
  let sending = false;

  function paint(sec) {
    button.classList.toggle('on', !!recording);
    button.classList.toggle('busy', sending);
    button.setAttribute('aria-label', recording ? 'Остановить запись' : 'Голосовой ввод');
    button.innerHTML = ICON;                           // константа выше
    if (recording) button.append(el('span', { class: 'mic-time' }, clock(sec || 0)));
  }

  button.addEventListener('click', async () => {
    if (sending) return;
    if (recording) {
      recording.stop();
      return;
    }
    error.textContent = '';
    const caret = [field.selectionStart ?? field.value.length, field.selectionEnd ?? field.value.length];
    try {
      recording = await startRecording(MAX_SEC, paint);
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    paint(0);
    const blob = await recording.done;
    recording = null;
    sending = true;
    paint();
    try {
      const body = new FormData();
      body.append('audio', blob, recordingName(blob));
      const { text } = await api('POST', '/api/dictation', body);
      if (text) insertAt(field, text, caret);
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
