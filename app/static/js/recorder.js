import { tr } from './i18n.js';
/* Запись встречи для «Расшифровки» (спека 2026-11-21-studio-source-capabilities-design, п. 38): длинная запись с
 * микрофона с паузой и защитой от потери.
 *
 * startMeeting({ onTick }) → { id, started, pause(), resume(), stop(): Promise<Blob>, cancel(), paused, seconds, safe }
 * или null (шторка или блокировка пришли, пока браузер спрашивал разрешение). Отказ — Error со словами.
 * - MediaRecorder: TYPES по порядку (`audio/webm;codecs=opus`, иначе `audio/mp4` — iPhone); кусок — каждые CHUNK_MS
 *   в IndexedDB DB (`chunks` по `[id, n]`, сводка — `recordings` по id: начало, секунды, тип). Звук не уходит
 *   никуда, пока его не загрузит «Расшифровка»; перезагрузка, закрытая вкладка, сбой — теряется не больше куска.
 *   На паузе и при уходе вкладки с экрана записанное дописывается сразу (requestData).
 * - Микрофон — владелец через mic.js `holdMic` с `pause`: диктовка в это время не стартует, шторка и блокировка
 *   (`cancelRecording()`) ставят паузу, смену раздела запись переживает. На паузе дорожка выключена (`enabled`).
 * - Wake Lock экрана, пока идёт запись (на паузе отпущен; вернулась на экран — снова). На iPhone (SCREEN_STOPS)
 *   запись всё равно встаёт, когда экран гаснет, — «Расшифровка» говорит это словами.
 * - Время — только записанное (без пауз); onTick(seconds, paused) и событие `studio-meeting` документа
 *   (`detail` — { id, seconds, paused }, конец записи — null: точка у рельса, app.js) — каждые TICK_MS.
 * - stop() — Blob всей записи (куски в IndexedDB остаются до discard: загрузку подтверждает «Расшифровка»);
 *   cancel() — запись стёрта.
 *
 * pendingRecordings() → [{ id, started, seconds }] — записи в IndexedDB (и идущая), restore(id) → Blob,
 * discard(id). */
import { holdMic } from './mic.js';

export const CHUNK_MS = 10000;
export const TYPES = ['audio/webm;codecs=opus', 'audio/mp4'];
export const SCREEN_STOPS = /iP(hone|ad|od)/.test(navigator.userAgent)
  || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
const DB = 'studio-rec';
const TICK_MS = 250;
const BUSY = tr("Микрофон занят: идёт запись встречи");

let current = null;              // идущая запись встречи студии
let starting = false;            // браузер спрашивает разрешение
let opened = null;               // Promise<IDBDatabase>

function openDb() {
  if (!opened) {
    opened = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB, 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('chunks', { keyPath: ['id', 'n'] });
        request.result.createObjectStore('recordings', { keyPath: 'id' });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    }).catch((err) => {
      opened = null;
      throw err;
    });
  }
  return opened;
}

const result = (request) => new Promise((resolve, reject) => {
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error);
});
const finished = (tx) => new Promise((resolve, reject) => {
  tx.oncomplete = () => resolve();
  tx.onerror = tx.onabort = () => reject(tx.error);
});
const piecesOf = (id) => IDBKeyRange.bound([id, 0], [id, Infinity]);

/** Идущая запись встречи или null. */
export function meeting() {
  return current;
}

/** Записи в IndexedDB, старые первыми; хранилище недоступно — пусто. */
export async function pendingRecordings() {
  try {
    const store = (await openDb()).transaction('recordings').objectStore('recordings');
    const all = await result(store.getAll());
    return all.map(({ id, started, seconds }) => ({ id, started, seconds })).sort((a, b) => a.started - b.started);
  } catch {
    return [];
  }
}

/** Запись из IndexedDB одним Blob. */
export async function restore(id) {
  const tx = (await openDb()).transaction(['chunks', 'recordings']);
  const [meta, chunks] = await Promise.all([result(tx.objectStore('recordings').get(id)),
    result(tx.objectStore('chunks').getAll(piecesOf(id)))]);
  if (!meta || !chunks.length) throw new Error(tr("Запись не найдена на этом устройстве"));
  return new Blob(chunks.map((chunk) => chunk.data), { type: meta.type });
}

export async function discard(id) {
  const tx = (await openDb()).transaction(['chunks', 'recordings'], 'readwrite');
  tx.objectStore('chunks').delete(piecesOf(id));
  tx.objectStore('recordings').delete(id);
  await finished(tx);
}

export async function startMeeting({ onTick } = {}) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    throw new Error(tr("Браузер не умеет записывать звук"));
  }
  if (current || starting) throw new Error(BUSY);
  let handle = null;
  let interrupted = false;       // шторка или блокировка — пока спрашивали разрешение
  const release = holdMic({ words: BUSY,
    cancel: () => { if (handle) handle.cancel(); else interrupted = true; },
    pause: () => { if (handle) handle.pause(); else interrupted = true; } });
  starting = true;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    release();
    throw new Error(err && err.name === 'NotAllowedError'
      ? tr("Нет разрешения на микрофон — разрешите его в настройках браузера") : tr("Микрофон недоступен"));
  } finally {
    starting = false;
  }
  if (interrupted) {
    for (const track of stream.getTracks()) track.stop();
    release();
    return null;
  }
  const type = TYPES.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const kind = (recorder.mimeType || type || 'audio/webm').split(';')[0];
  const id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const started = Date.now();
  const pieces = [];             // вся запись в памяти: stop() отдаёт её, не читая IndexedDB
  let n = 0;
  let total = 0;                 // записанные мс до последней паузы
  let since = 0;                 // начало идущего отрезка (performance.now)
  let running = false;
  let safe = true;               // куски ложатся в IndexedDB
  let lock = null;               // Wake Lock экрана
  let dropping = false;          // cancel(): куски стираются до конца записи — «Восстановить» её не увидит
  let writing = openDb().then(() => {}, () => { safe = false; });

  const elapsed = () => (total + (running ? performance.now() - since : 0)) / 1000;

  function save(data) {
    const at = elapsed();
    const number = n++;
    writing = writing.then(async () => {
      const buffer = await data.arrayBuffer();
      const tx = (await openDb()).transaction(['chunks', 'recordings'], 'readwrite');
      tx.objectStore('chunks').put({ id, n: number, data: buffer });
      tx.objectStore('recordings').put({ id, started, seconds: at, type: kind });
      await finished(tx);
    }).catch(() => { safe = false; });
  }

  async function wake() {
    if (!running || (lock && !lock.released) || document.visibilityState !== 'visible' || !navigator.wakeLock) return;
    try {
      const got = await navigator.wakeLock.request('screen');
      if (running && !(lock && !lock.released)) lock = got;
      else got.release().catch(() => {});
    } catch {
      // экономия заряда, нет разрешения — запись идёт и без него
    }
  }

  function sleep() {
    if (lock) lock.release().catch(() => {});
    lock = null;
  }

  function announce() {
    const seconds = elapsed();
    if (onTick) onTick(seconds, !running);
    document.dispatchEvent(new CustomEvent('studio-meeting', { detail: { id, seconds, paused: !running } }));
  }

  function onVisibility() {
    if (document.visibilityState === 'visible') wake();
    else if (recorder.state === 'recording') recorder.requestData();   // экран гаснет — записанное в IndexedDB
  }

  recorder.addEventListener('dataavailable', (event) => {
    if (!event.data || !event.data.size) return;
    pieces.push(event.data);
    save(event.data);
  });
  const timer = setInterval(announce, TICK_MS);
  // конец записи — stop(), cancel() или сам рекордер (дорожка кончилась): микрофон отпущен, куски дописаны
  const stopped = new Promise((resolve) => {
    recorder.addEventListener('stop', async () => {
      clearInterval(timer);
      if (running) total += performance.now() - since;
      running = false;
      sleep();
      document.removeEventListener('visibilitychange', onVisibility);
      for (const track of stream.getTracks()) track.stop();
      release();
      if (current === handle) current = null;
      await writing;
      if (dropping) await discard(id).catch(() => {});
      document.dispatchEvent(new CustomEvent('studio-meeting', { detail: null }));
      resolve();
    }, { once: true });
  });

  function pause() {
    if (!running || recorder.state !== 'recording') return;
    total += performance.now() - since;
    running = false;
    recorder.requestData();
    recorder.pause();
    for (const track of stream.getTracks()) track.enabled = false;
    sleep();
    announce();
  }

  function resume() {
    if (running || recorder.state !== 'paused') return;
    for (const track of stream.getTracks()) track.enabled = true;
    recorder.resume();
    since = performance.now();
    running = true;
    wake();
    announce();
  }

  handle = {
    id, started, pause, resume,
    async stop() {
      if (recorder.state !== 'inactive') recorder.stop();
      await stopped;
      return new Blob(pieces, { type: kind });
    },
    async cancel() {
      dropping = true;
      if (recorder.state !== 'inactive') recorder.stop();
      await stopped;
    },
    get paused() { return !running; },
    get seconds() { return elapsed(); },
    get safe() { return safe; },
  };
  current = handle;
  document.addEventListener('visibilitychange', onVisibility);
  recorder.start(CHUNK_MS);
  since = performance.now();
  running = true;
  wake();
  announce();
  return handle;
}
