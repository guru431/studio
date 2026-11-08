/* Паспорт подключения (спека 2026-10-21-studio-connection-check-design, раздел 8): «Проверить» и «Пробный
 * ответ» в карточке подключения настроек раздела.
 *
 * check(conn, browser) — через сервер POST /api/connections/<id>/check; прямое подключение проверяет сам
 * браузер: токен и адреса — из одного ответа /token (токен живёт только в памяти запроса), GET models_url
 * с ключом, ответил 200 — тот же GET без ключа (открытый список — ключ «не проверен»), у OpenRouter — ещё
 * GET …/key; итог — PUT /api/connections/<id>/passport (время, источник «браузер» и возможности ставит
 * сервер). Ответ обоих — { passport, found }: found — найденные модели разделов.
 * probe(conn, model, browser) — одно короткое сообщение модели чата (только не srvllm); браузером — итог
 * тем же PUT полем probes.
 * passportBlock(...) — <details>: строка «Проверено 21.10 17:40 · адрес отвечает · ключ принят», раскрытый —
 * адрес, ключ, профиль, число моделей и возможности моделей раздела с источником. Текст — только textContent. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { el } from './ui.js';

export const NO_ANSWER = 'нет ответа: сервис недоступен или не разрешает запросы из браузера (CORS)';
export const PROBE_WARNING = 'Отправит одно короткое сообщение; у платного сервиса это расход.';
const PROBE_TEXT = 'Ответь одним словом: ok';
const PROBE_TOKENS = 16;
const MODELS_MAX = 500;
const ID_MAX = 200;
const WORDS_MAX = 300;

const CAPS = { generate: 'генерация', edit: 'правка', mask: 'кисть', strength: 'сила', seed: 'seed',
  video: 'ролик из картинки', diarize: 'расшифровка с собеседниками', transcribe: 'расшифровка без собеседников',
  dictation: 'голосовой ввод', dictation_ru: 'голосовой ввод (только русский)', voices: 'озвучка своими образцами',
  tts: 'озвучка голосом по имени', describe: '«Описать»', translate: '«EN»', vision: 'смотрит фото',
  chat: 'отвечает' };
const SOURCES = { catalog: 'встроенный перечень srvllm', metadata: 'по описанию модели у сервиса',
  name: 'по названию, не проверено', probe: 'пробный ответ', probe_browser: 'пробный ответ, по словам браузера' };
const PROFILES = { srvllm: 'шлюз srvllm', s06: 'шлюз s06', openai: 'OpenAI', openrouter: 'OpenRouter', compatible: 'OpenAI-совместимый' };
const NOT_GATEWAY = 'нет кисти, силы, seed, «Описать», «EN», роликов и образцов голоса; расшифровка — без собеседников';
const KEY_ORDER = ['refused', 'accepted', 'limited', 'unverified', 'unknown'];
const UNVERIFIED = { state: 'unverified', code: 200,
  words: 'сервис отдаёт список без ключа — ключ проверит первый запрос или «Пробный ответ»' };

/* ---- проверка ------------------------------------------------------------------------ */

async function tokenOf(conn) {
  try {
    return await api('GET', `/api/connections/${conn.id}/token`);
  } catch (err) {
    if (err.status === 403 || err.status === 404) throw new Error('Токен этого подключения браузеру не выдаётся');
    throw err;
  }
}

function keyWords(code) {
  if (code === 200) return { state: 'accepted', code, words: 'принят' };
  if (code === 401) return { state: 'refused', code, words: 'не принят (401): введите токен заново' };
  if (code === 403) return { state: 'limited', code, words: 'принят, маршрут закрыт (403)' };
  return { state: 'unknown', code, words: `неясно: сервис ответил ${code}` };
}

async function send(url, token, body) {
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  if (body) headers['Content-Type'] = 'application/json';
  return fetch(url, { method: body ? 'POST' : 'GET', headers, body: body ? JSON.stringify(body) : undefined,
    credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
}

async function jsonOf(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** Строка паспорта одного GET: адрес, время, ключ; тело — для списка моделей. */
async function route(url, token) {
  const row = { path: new URL(url).pathname, ms: null, status: null, count: null, key: null };
  const start = performance.now();
  let response;
  try {
    response = await send(url, token);
  } catch {
    return { row: { ...row, reach: { ok: false, words: NO_ANSWER } }, body: null };
  }
  return { row: { ...row, ms: Math.round(performance.now() - start), status: response.status,
    reach: { ok: true, words: 'отвечает' }, key: keyWords(response.status) }, body: await jsonOf(response) };
}

function modality(list) {
  return (Array.isArray(list) ? list : []).filter((m) => typeof m === 'string' && m.length <= 32).slice(0, 16);
}

/** Модели ответа /models в форме PUT …/passport (пределы сервера). */
function modelsOf(body) {
  const data = body && Array.isArray(body.data) ? body.data : [];
  return data.filter((m) => m && typeof m.id === 'string' && m.id.length > 0 && m.id.length <= ID_MAX)
    .slice(0, MODELS_MAX).map((m) => {
      const out = { id: m.id };
      if (Number.isInteger(m.context_length) && m.context_length >= 0) out.context_length = m.context_length;
      if (m.architecture && typeof m.architecture === 'object') {
        out.architecture = { input_modalities: modality(m.architecture.input_modalities),
          output_modalities: modality(m.architecture.output_modalities) };
      }
      return out;
    });
}

async function browserCheck(conn) {
  const { token, models_url: url } = await tokenOf(conn);
  const first = await route(url, token);
  const routes = [first.row];
  let models;
  if (first.row.status === 200) {
    models = modelsOf(first.body);
    first.row.count = models.length;
    const open = await route(url, null);
    if (open.row.status === 200) first.row.key = { ...UNVERIFIED };
  }
  if (new URL(url).hostname === 'openrouter.ai') routes.push((await route(url.replace(/\/models$/, '/key'), token)).row);
  return api('PUT', `/api/connections/${conn.id}/passport`, models ? { routes, models } : { routes });
}

/** Проверка: через сервер или (прямое подключение) браузером. → { passport, found } */
export function check(conn, browser) {
  return browser ? browserCheck(conn) : api('POST', `/api/connections/${conn.id}/check`);
}

/* ---- пробный ответ ------------------------------------------------------------------- */

async function browserProbe(conn, model) {
  const { token, chat_url: url } = await tokenOf(conn);
  let body = { model, messages: [{ role: 'user', content: PROBE_TEXT }], max_tokens: PROBE_TOKENS, stream: false };
  const start = performance.now();
  let result;
  try {
    let response = await send(url, token, body);
    let answer = await jsonOf(response);
    const reason = () => String((answer && answer.error && answer.error.message) || '');
    if (response.status === 400 && reason().includes('max_tokens')) {      // модели рассуждений OpenAI
      body = { model, messages: body.messages, max_completion_tokens: PROBE_TOKENS, stream: false };
      response = await send(url, token, body);
      answer = await jsonOf(response);
    }
    const ms = Math.round(performance.now() - start);
    result = response.status === 200 ? { ok: true, ms, words: `ответила за ${ms} мс` }
      : { ok: false, ms, words: `не ответила: код ${response.status}${reason() ? ` — ${reason()}` : ''}`.slice(0, WORDS_MAX) };
  } catch {
    result = { ok: false, ms: null, words: NO_ANSWER };
  }
  return api('PUT', `/api/connections/${conn.id}/passport`, { probes: { [model]: result } });
}

/** Пробный ответ модели чата: через сервер или браузером. */
export function probe(conn, model, browser) {
  return browser ? browserProbe(conn, model) : api('POST', `/api/connections/${conn.id}/probe`, { model });
}

/* ---- блок паспорта ------------------------------------------------------------------- */

function stamp(ts) {
  return dateTime(ts, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).replace(',', '');
}

/** Строки паспорта, которые говорят о разделе; null — у раздела проб нет (речь шлюза). */
function routesOf(passport, conn, section) {
  if (conn.kind !== 'srvllm') return passport.routes;
  if (section === 'chat') return passport.routes.filter((r) => r.path.endsWith('/v1/models'));
  if (section === 'photo' || section === 'video') return passport.routes.filter((r) => r.path.endsWith('/slot/models'));
  return null;
}

function keyOf(routes) {
  const keys = routes.map((r) => r.key).filter(Boolean);
  for (const state of KEY_ORDER) {
    const key = keys.find((k) => k.state === state);
    if (key) return key;
  }
  return null;
}

function capsText(model) {
  if (model.absent) return 'нет у шлюза';
  const bySource = new Map();
  for (const c of model.caps) bySource.set(c.source, [...(bySource.get(c.source) || []), CAPS[c.name] || c.name]);
  const parts = [...bySource].map(([source, names]) => `${names.join(', ')} — ${SOURCES[source] || source}`);
  if (model.context_length) parts.push(`контекст ${model.context_length}`);
  return parts.length ? parts.join('; ') : 'возможности не известны';
}

function line(cls, ...text) {
  return el('li', { class: cls }, ...text);
}

/** <details class="passport"> карточки: models — модели раздела (их возможности), label — подпись модели. */
export function passportBlock({ conn, section, models, label, error = '', busy = false, open = false, onToggle }) {
  const passport = conn.passport;
  const routes = passport ? routesOf(passport, conn, section) : null;
  const parts = [];
  if (busy) parts.push('Проверяю…');
  else parts.push(passport && passport.checked ? `Проверено ${stamp(passport.checked)}` : 'Не проверено');
  if (passport && passport.checked && routes) {
    const down = routes.find((r) => !r.reach.ok);
    parts.push(down ? `адрес: ${down.reach.words}` : 'адрес отвечает');
    const key = keyOf(routes);
    if (key) parts.push(`ключ ${key.words}`);
  } else if (passport && passport.checked) parts.push('не проверяется: проба занимала бы карту');
  if (passport && passport.source === 'browser') parts.push('по словам браузера');
  const lines = [];
  if (passport && passport.checked) {
    if (routes) {
      for (const r of routes) {
        const where = routes.length > 1 ? `Адрес ${r.path}` : 'Адрес';
        lines.push(line('passport-reach', `${where} — ${r.reach.words}${r.ms !== null ? `, ${r.ms} мс` : ''}`));
      }
      const key = keyOf(routes);
      if (key) lines.push(line('passport-key', `Ключ — ${key.words}`));
      const counted = routes.find((r) => r.count !== null);
      if (counted) lines.push(line('passport-count', `Модели — ${counted.count}`));
    } else lines.push(line('passport-reach', 'Адрес — не проверяется: проба занимала бы карту'));
    lines.push(line('passport-profile', `Профиль — ${PROFILES[passport.profile] || passport.profile}`
      + (passport.profile === 'srvllm' ? '' : `: ${NOT_GATEWAY}`)));
  }
  if (passport) {
    const byId = new Map(passport.models.map((m) => [m.id, m]));
    const shown = [...models];
    if (conn.kind === 'srvllm' && section === 'photo') {
      for (const m of passport.models) if (m.caps.some((c) => c.name === 'describe') && !shown.includes(m.id)) shown.push(m.id);
    }
    for (const id of shown) {
      const model = byId.get(id);
      if (model) lines.push(line('passport-model', `${label(id)}: ${capsText(model)}`));
      const tried = (passport.probes || {})[id];
      if (tried) {
        lines.push(line(`passport-probe${tried.ok ? '' : ' error'}`, `${label(id)}: пробный ответ — ${tried.words}`
          + ` (${stamp(tried.at)}${tried.source === 'browser' ? ', по словам браузера' : ''})`));
      }
    }
  }
  if (error) lines.push(line('passport-error error', error));
  const details = el('details', { class: 'passport', open: open || !!error },
    el('summary', { class: 'passport-summary' }, parts.join(' · ')), el('ul', { class: 'passport-lines' }, ...lines));
  if (onToggle) details.addEventListener('toggle', () => onToggle(details.open));
  return details;
}
