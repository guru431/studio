import { locale, tr } from './i18n.js';
/* Числа ответа «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 13, решение 19): у прямого подключения
 * страница сама просит у сервиса `usage` в потоке и присылает его с длительностью в PUT ответа. Сервис отверг
 * параметр потока — запрос повторяется без него, подключение запоминается в памяти вкладки (до перезагрузки). */

const USAGE_MAX = 100000000;          // как chat.USAGE_MAX: больше PUT ответа не примет (422)
export const MS_MAX = 86400000;       // как chat.MS_MAX: предел gen_ms и total_ms того же PUT
export const REJECTED = ['unknown', 'unsupported', 'unrecognized', 'not supported', 'extra fields not permitted',
  'extra inputs are not permitted', 'unexpected keyword'];   // как profiles.REJECTED
const refused = new Set();            // id подключений, что не принимают `stream_options`

/** Добавка к телу запроса прямого подключения: `stream_options`, пока подключение его не отвергло. */
export function usageBody(connId) {
  return refused.has(connId) ? {} : { stream_options: { include_usage: true } };
}

/** Ответ 400 с признаком отказа по параметру и словом `stream_options` / `include_usage`: подключение отмечено —
 * true (повторить запрос один раз без параметра). */
export function usageRefused(connId, status, text) {
  if (status !== 400 || refused.has(connId)) return false;
  const lowered = String(text || '').toLowerCase();
  if (!REJECTED.some((marker) => lowered.includes(marker))) return false;
  if (!lowered.includes('stream_options') && !lowered.includes('include_usage')) return false;
  refused.add(connId);
  return true;
}

/** `usage` сервиса → `{in, out, cached, reasoning}`: только целые от 0 до USAGE_MAX; чего нет — нет ключа
 * (как chat._usage_of). Ничего — null. */
export function usageOf(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const number = (value) => (Number.isInteger(value) && value >= 0 && value <= USAGE_MAX ? value : null);
  const found = { in: number(raw.prompt_tokens), out: number(raw.completion_tokens),
    cached: number(raw.prompt_tokens_details?.cached_tokens),
    reasoning: number(raw.completion_tokens_details?.reasoning_tokens) };
  const out = Object.fromEntries(Object.entries(found).filter(([, value]) => value !== null));
  return Object.keys(out).length ? out : null;
}

/** Скорость ответа, ток/с целым: выходные токены на время генерации; чисел нет — null. */
export function speedOf(message) {
  const out = message?.usage?.out;
  const ms = message?.gen_ms;
  return Number.isFinite(out) && out > 0 && Number.isFinite(ms) && ms > 0 ? Math.round(out / (ms / 1000)) : null;
}

/** Подпись модели под ответом со скоростью: «модель · 25 ток/с»; скорости нет — подпись как есть. */
export function withSpeed(label, message) {
  const speed = speedOf(message);
  return speed === null ? label : `${label} · ${tr("{0} ток/с", speed.toLocaleString(locale()))}`;
}
