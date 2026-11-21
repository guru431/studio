import { tr } from './i18n.js';
/* «Разговор» в диалоге (спека 2026-11-21-studio-source-capabilities-design, п. 13): голос → обычный ход → озвучка
 * ответа, по кругу. Кнопку держит chat.js, сюда — только сам круг.
 *
 * startConversation(chat) → stop(). `chat`: send(text, update) — обычный ход диалога с этим текстом, `update(content,
 * final)` — по мере `delta`; итог — статус ответа или false (хода не было: слова уже в диалоге); state(name) —
 * `listening`, `hearing`, `thinking`, `speaking`, `off`; failed(words) — слова под лентой.
 *
 * - Микрофон — владелец через mic.js `holdMic`: диктовка в это время не стартует, `cancelRecording()` (смена раздела,
 *   шторка, блокировка) останавливает разговор. Ещё стоп — кнопкой и Escape.
 * - Конец фразы — по энергии (`AnalyserNode`, RMS выше LEVEL — речь): тишина SILENCE_MS после не меньше SPEECH_MS речи;
 *   фраза — до PHRASE_MS (речь была — уходит, не было — запись заново). Запись — MediaRecorder того же потока.
 * - Распознавание — тем же маршрутом, что микрофон поля (`POST /api/dictation`, язык голосового ввода устройства).
 * - Ответ — по мере `delta` целыми предложениями в очередь `POST /api/speech/say` (по одной фразе учётки за раз,
 *   следующая озвучивается, пока играет предыдущая) и воспроизведение по порядку; кончил говорить — снова слушает.
 *   Звук не сохраняется; весь текст — в истории диалога, как у обычного хода.
 * - Звук — один AudioContext на студию: создаётся и разблокируется нажатием на кнопку (iOS иначе молчит), он же
 *   слушает микрофон. */
import { api } from './api.js';
import { TYPES, dictationLanguage, holdMic, recordingName } from './mic.js';
import { words } from './ui.js';

export const SILENCE_MS = 1200;
export const SPEECH_MS = 400;
export const PHRASE_MS = 60000;
const LEVEL = 0.02;              // RMS кадра: выше — речь
const FRAME_MS = 50;
const SAY_MAX = 1000;            // как speeches.SAY_MAX
// конец предложения: знак и пробел после него (с закрывающими кавычками и скобками) или перевод строки
const BOUNDARY = /[.!?…]+["»”')\]]*\s+|\n+/g;
const NO_SOURCE = tr("Для «Разговора» нужен источник озвучки через API — выберите его в разделе «Озвучка»");

let audio = null;                // AudioContext студии

/** AudioContext, разблокированный нажатием: зовётся прямо из обработчика кнопки (iOS — кадр тишины). */
function unlock() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!audio) audio = new Context();
  if (audio.state === 'suspended') audio.resume().catch(() => {});
  const silence = audio.createBufferSource();
  silence.buffer = audio.createBuffer(1, 1, 22050);
  silence.connect(audio.destination);
  silence.start(0);
  return audio;
}

/** Предложения куска ответа не длиннее SAY_MAX; без букв и цифр (разметка, код) — пропускаются. */
function sentences(chunk) {
  const out = [];
  for (const raw of chunk.split(BOUNDARY)) {
    let text = raw.replace(/\s+/g, ' ').trim();
    while (text.length > SAY_MAX) {
      const cut = text.lastIndexOf(' ', SAY_MAX);
      const at = cut > 0 ? cut : SAY_MAX;
      out.push(text.slice(0, at));
      text = text.slice(at).trim();
    }
    if (text) out.push(text);
  }
  return out.filter((s) => /[\p{L}\p{N}]/u.test(s));
}

/** Конец последнего целого предложения в тексте; нет — 0. */
function lastBoundary(text) {
  let end = 0;
  for (const match of text.matchAll(BOUNDARY)) end = match.index + match[0].length;
  return end;
}

export function startConversation(chat) {
  const context = unlock();
  let stopped = false;
  let stream = null;
  let source = null;
  let analyser = null;
  let recorder = null;           // { rec, drop() } — идущая запись фразы
  let playing = null;            // идущий звук фразы ответа
  const controller = new AbortController();

  const release = holdMic({ cancel: () => stop(), words: tr("Микрофон занят: идёт «Разговор»") });
  const onKey = (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (document.querySelector('.popup-menu:popover-open, dialog[open]')) return;   // Escape — им
    stop();
  };
  document.addEventListener('keydown', onKey);

  function stop() {
    if (stopped) return;
    stopped = true;
    release();
    document.removeEventListener('keydown', onKey);
    controller.abort();
    if (recorder) recorder.drop();
    if (playing) playing.stop();
    if (stream) for (const track of stream.getTracks()) track.stop();
    if (source) source.disconnect();
    chat.state('off');
  }

  /** Одна фраза: Blob записи или null (фразы не было, остановили). */
  function listen() {
    return new Promise((resolve) => {
      chat.state('listening');
      const type = TYPES.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
      const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      const chunks = [];
      const frame = new Float32Array(analyser.fftSize);
      const started = performance.now();
      let last = started;
      let speech = 0;
      let quiet = 0;
      let heard = false;
      let keep = false;
      let timer = null;
      const end = (keepIt) => {
        keep = keepIt;
        clearInterval(timer);
        if (rec.state !== 'inactive') rec.stop();
      };
      recorder = { drop: () => end(false) };
      rec.addEventListener('dataavailable', (event) => { if (event.data && event.data.size) chunks.push(event.data); });
      rec.addEventListener('stop', () => {
        recorder = null;
        resolve(keep && !stopped ? new Blob(chunks, { type: (rec.mimeType || type || 'audio/webm').split(';')[0] }) : null);
      });
      rec.start();
      timer = setInterval(() => {
        const now = performance.now();
        const step = now - last;
        last = now;
        analyser.getFloatTimeDomainData(frame);
        let sum = 0;
        for (const v of frame) sum += v * v;
        if (Math.sqrt(sum / frame.length) > LEVEL) {
          speech += step;
          quiet = 0;
          if (!heard && speech >= SPEECH_MS) {
            heard = true;
            chat.state('hearing');
          }
        } else {
          quiet += step;
          if (!heard && quiet >= SILENCE_MS) speech = 0;      // короткие шумы не копятся во фразу
        }
        if (heard && quiet >= SILENCE_MS) end(true);
        else if (now - started >= PHRASE_MS) end(heard);
      }, FRAME_MS);
    });
  }

  async function recognize(blob) {
    const body = new FormData();
    body.append('audio', blob, recordingName(blob));
    const language = dictationLanguage();
    if (language !== 'auto') body.append('language', language);
    const { text } = await api('POST', '/api/dictation', body, { signal: controller.signal });
    return (text || '').trim();
  }

  /** Звук фразы, готовый к воспроизведению; null — без звука (слова уже сказаны, источник MCP — стоп). */
  async function say(text) {
    if (stopped) return null;
    try {
      const response = await api('POST', '/api/speech/say', { text }, { signal: controller.signal });
      return await context.decodeAudioData(await response.arrayBuffer());
    } catch (err) {
      if (stopped || err?.name === 'AbortError') return null;
      if (err?.code === 'say_mcp') {
        chat.failed(NO_SOURCE);
        stop();
      } else if (err?.code !== 'text') chat.failed(words(err));
      return null;
    }
  }

  function play(buffer) {
    return new Promise((resolve) => {
      if (stopped) { resolve(); return; }
      chat.state('speaking');
      const node = context.createBufferSource();
      node.buffer = buffer;
      node.connect(context.destination);
      node.onended = () => {
        if (playing === node) playing = null;
        resolve();
      };
      playing = node;
      node.start();
    });
  }

  /** Ход с текстом фразы; ответ — по предложениям в озвучку и воспроизведение по порядку. */
  async function answer(text) {
    let spoken = 0;
    let fetching = Promise.resolve();
    let speaking = Promise.resolve();
    const enqueue = (sentence) => {
      fetching = fetching.then(() => say(sentence));
      const sound = fetching;
      speaking = speaking.then(async () => {
        const buffer = await sound;
        if (buffer) await play(buffer);
      });
    };
    const update = (content, final) => {
      if (stopped) return;
      const rest = content.slice(spoken);
      const cut = final ? rest.length : lastBoundary(rest);
      if (!cut) return;
      for (const sentence of sentences(rest.slice(0, cut))) enqueue(sentence);
      spoken += cut;
    };
    const status = await chat.send(text, update);
    if (status === false) {
      stop();                                        // хода не было: слова — в диалоге
      return;
    }
    await speaking;
  }

  (async () => {
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
        throw new Error(tr("Браузер не умеет записывать звук"));
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (err) {
        throw new Error(err && err.name === 'NotAllowedError'
          ? tr("Нет разрешения на микрофон — разрешите его в настройках браузера") : tr("Микрофон недоступен"));
      }
      if (stopped) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      source = context.createMediaStreamSource(stream);
      analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      while (!stopped) {
        const blob = await listen();
        if (!blob || stopped) continue;
        chat.state('thinking');
        let text;
        try {
          text = await recognize(blob);
        } catch (err) {
          if (stopped) break;
          chat.failed(words(err));
          if (err?.code === 'no_model') break;      // модели голосового ввода нет — круг бесполезен
          continue;
        }
        if (!text) {
          chat.failed(tr("Ничего не распознано — скажите ещё раз"));
          continue;
        }
        chat.failed('');
        await answer(text);
      }
    } catch (err) {
      if (!stopped) chat.failed(words(err));
    }
    stop();
  })();
  return stop;
}
