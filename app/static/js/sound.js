import { tr } from './i18n.js';
/* Звуковые сигналы (п. 37 спеки 2026-11-25-studio-skills-status-local-models): «готово», «нужно ваше действие»,
 * «ошибка» — два синтезированных тона, без файлов. Настройка устройства (`localStorage['studio-sound']`, '1' —
 * включён; по умолчанию ключа нет — тишина), выход из учётки её не стирает. Звучит только на видимой странице и
 * после первого жеста человека (браузер иначе не запускает AudioContext); тот же ключ — не чаще REPEAT_MS, любой
 * сигнал — не чаще GAP_MS. Источники: конец ответа «Чата» (answerSignal), опрос «В работе» (jobSignal), снимки
 * «Агентов» (studio-code-snapshot → agentSignals), события Open WebUI. */
import { el } from './ui.js';

export const SOUND_KEY = 'studio-sound';
/** Частоты двух тонов сигнала, Гц. */
export const SIGNALS = { done: [660, 880], ask: [880, 880], error: [330, 220] };
export const GAP_MS = 1500;
export const REPEAT_MS = 15000;
const TONE_MS = 120;
const PAUSE_MS = 60;
const PEAK = 0.08;
const RAMP_S = 0.01;
const QUIET_JOB_MS = 24 * 60 * 60 * 1000;  // «В работе» может вернуть готовое за сутки (jobs.JOBS_VISIT_MAX_SEC)

let context = null;
let lastAt = -Infinity;
const keys = new Map();          // ключ сигнала → когда звучал
const quietJobs = new Map();     // id голосового ответа → до какого времени его опрос не должен озвучивать

export function soundOn() {
  try { return localStorage.getItem(SOUND_KEY) === '1'; } catch { return false; }
}

export function setSound(on) {
  try {
    if (on) localStorage.setItem(SOUND_KEY, '1');
    else localStorage.removeItem(SOUND_KEY);
  } catch { /* хранилище недоступно — сигнал выключен */ }
}

/** Один AudioContext на страницу — по жесту человека (иначе браузер оставляет его `suspended`). */
function wake() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return null;
  if (!context || context.state === 'closed') context = new Context();
  if (context.state !== 'running') context.resume?.().catch(() => {});
  return context;
}
/* Будит каждый жест, не один: у касания активацию даёт отпускание (`pointerup`), не `pointerdown`; iOS
 * приостанавливает контекст после фона (`suspended`, `interrupted`); звук могут включить позже загрузки. */
for (const type of ['pointerup', 'keydown']) {
  document.addEventListener(type, () => { if (soundOn() && context?.state !== 'running') wake(); }, { capture: true });
}

function tone(ctx, frequency, start) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = frequency;
  const end = start + TONE_MS / 1000;
  gain.gain.setValueAtTime(0, start);
  gain.gain.linearRampToValueAtTime(PEAK, start + RAMP_S);
  gain.gain.setValueAtTime(PEAK, end - RAMP_S);
  gain.gain.linearRampToValueAtTime(0, end);
  osc.connect(gain).connect(ctx.destination);
  osc.start(start);
  osc.stop(end);
}

function play(kind) {
  const ctx = context;
  const at = ctx.currentTime;
  SIGNALS[kind].forEach((frequency, n) => tone(ctx, frequency, at + n * (TONE_MS + PAUSE_MS) / 1000));
}

/** Сигнал `kind`; `key` — событие (id ответа, задания): то же событие не звучит повторно REPEAT_MS. Итог — прозвучал ли. */
export function signal(kind, key = null) {
  if (!SIGNALS[kind] || !soundOn() || document.visibilityState !== 'visible') return false;
  if (!context || context.state !== 'running') return false;
  const now = performance.now();
  if (now - lastAt < GAP_MS) return false;
  if (key !== null) key = `${kind}:${key}`;        // пауза и конец того же ответа — разные события
  if (key !== null && now - (keys.get(key) ?? -Infinity) < REPEAT_MS) return false;
  lastAt = now;
  if (key !== null) keys.set(key, now);
  for (const [k, at] of keys) if (now - at >= REPEAT_MS) keys.delete(k);
  play(kind);
  return true;
}

/** Конец ответа «Чата»: пауза хода — `ask`, ошибка — `error`, готово — `done`; ключ — id ответа (он же id строки
 * «В работе»). `quiet` — ход «Разговора»: итог в «В работе» молчит, даже если опрос надолго приостановился. */
export function answerSignal(message, { quiet = false } = {}) {
  if (!message) return false;
  const key = message.id ?? null;
  const kind = message.pending ? 'ask' : message.status === 'error' ? 'error' : message.status === 'done' ? 'done' : null;
  if (!kind) return false;
  if (quiet) {
    const now = performance.now();
    for (const [id, until] of quietJobs) if (until <= now) quietJobs.delete(id);
    if (key !== null && kind !== 'ask') quietJobs.set(key, now + QUIET_JOB_MS);
    return false;
  }
  return signal(kind, key);
}

/** Новое готовое опроса «В работе»: отменённое человеком — без сигнала; хоть один итог не `done` — `error`, иначе
 * `done`; ключ — id первого. */
export function jobSignal(fresh) {
  const now = performance.now();
  const ended = (fresh || []).filter((f) => f.state !== 'cancelled' && (quietJobs.get(f.id) ?? 0) <= now);
  if (!ended.length) return false;
  return signal(ended.some((f) => f.state !== 'done') ? 'error' : 'done', ended[0].id);
}

/** Сигналы снимка «Агентов» компьютера `aid`: `before` — «aid:чат → состояние» (ведёт сама). Первый снимок компьютера —
 * без сигналов; стало `asking` — `ask`; было `running` или `asking`, стало не `running` — `done`. */
export function agentSignals(before, aid, items) {
  const prefix = `${aid}:`;
  const first = ![...before.keys()].some((k) => k.startsWith(prefix)) && !before.has(`${prefix}*`);
  const now = new Map(items.map((item) => [`${prefix}${item.chat}`,
    item.asking || (item.running && item.queue_paused) ? 'asking' : item.running ? 'running' : 'idle']));
  const out = [];
  if (!first) {
    for (const [key, was] of before) {
      if (!key.startsWith(prefix) || key === `${prefix}*`) continue;
      const state = now.get(key) || 'idle';
      if (state === 'asking' && was !== 'asking') out.push('ask');
      else if ((was === 'running' || was === 'asking') && state !== 'running' && state !== 'asking') out.push('done');
    }
    for (const [key, state] of now) if (!before.has(key) && state === 'asking') out.push('ask');
  }
  for (const key of [...before.keys()]) if (key.startsWith(prefix)) before.delete(key);
  before.set(`${prefix}*`, 'seen');
  for (const [key, state] of now) before.set(key, state);
  return [...new Set(out)];
}

const agents = new Map();
document.addEventListener('studio-code-snapshot', ({ detail }) => {
  const kinds = agentSignals(agents, detail.aid, detail.items || []);
  for (const kind of ['ask', 'error', 'done']) if (kinds.includes(kind)) { signal(kind); break; }
});

/** «Настройки → Уведомления»: флажок `#sound-on` и проба `#sound-test`. */
export function soundBlock() {
  const box = el('input', { type: 'checkbox', id: 'sound-on', checked: soundOn() });
  const test = el('button', { type: 'button', class: 'btn btn-sm', id: 'sound-test', disabled: !soundOn() }, tr("Проверить"));
  const probe = () => {
    const ctx = wake();
    if (!ctx) return;
    const go = () => { lastAt = -Infinity; signal('done'); };
    if (ctx.state === 'running') go();
    else ctx.resume?.().then(go, () => {});
  };
  box.addEventListener('change', () => {
    setSound(box.checked);
    test.disabled = !box.checked;
    if (box.checked) probe();
  });
  test.addEventListener('click', probe);
  return el('section', { class: 'block', id: 'sound-settings' }, el('h2', {}, tr("Звук на устройстве")),
    el('label', { class: 'check' }, box, tr("Звуковые сигналы")), test,
    el('p', { class: 'muted small' }, tr("Только на этом устройстве и пока Студия на экране")));
}
