import { message as translateMessage, tr } from './i18n.js';
/* Запросы к API студии.
 *
 * api(метод, путь, тело, { signal, headers }): путь — полный, '/api/...'; тело — объект (уходит JSON), FormData или Blob;
 * signal — обрыв запроса (AbortSignal, например срок).
 * Не-GET несёт заголовок CSRF из me.csrf (профиля ещё нет — из useCsrf). Ответ: JSON → объект, 204 → null, прочее (например,
 * поток text/event-stream для readSSE) → сам Response. Отказ → ApiError(status, code, message, detail)
 * из `detail` ответа; code 'login' (сессия кончилась) уводит на страницу входа (toLogin — с адресом раздела).
 * 423 `screen_locked` (студия заперта, lock.js) при заданном hooks.locked ждёт отпирания и повторяет
 * тот же запрос: опрос хода задания не показывает ошибку из-за блокировки. `unlock: false` — без экрана блокировки:
 * 423 уходит отказом (фоновая проверка, которую пользователь не просил, не должна запирать открытый раздел).
 *
 * Модули разделов импортируют './api.js' — один экземпляр на страницу (app.js — не импортировать:
 * его адрес с ?v= и второй импорт без версии запустил бы оболочку дважды). */

export class ApiError extends Error {
  constructor(status, code, message, detail = null) {
    super(translateMessage(message));
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;        // объект detail ответа целиком (например, left у неверного PIN)
  }
}

/** Ответа нет, а запрос, может быть, дошёл: связь (status 0), срок (TimeoutError), прокси или мост не ответили
 * вовремя (502/503/504). Изменяющий запрос такой отказ не доказывает — сверять или повторять идемпотентно. */
export function unknownOutcome(err) {
  return err?.name === 'TimeoutError' || err?.status === 0 || [502, 503, 504].includes(err?.status);
}

/** Профиль вошедшего (GET /api/me); обновляет loadMe(). */
export let me = null;

/** locked — ожидание отпирания (lock.js): обещание, которое разрешится, когда студию откроют. */
export const hooks = { locked: null };

let csrf = null;

/** CSRF сессии, пока профиля нет (запертая студия получает его из GET /api/lock). */
export function useCsrf(token) {
  csrf = token;
}

/** Страница входа с текущим адресом студии: после входа сервер вернёт сюда же (auth.login_target). Событие
 * `studio-signed-out` стирает черновики «Агентов» вкладки (code-drafts.js). */
export function toLogin() {
  document.dispatchEvent(new Event('studio-signed-out'));
  location.href = '/login' + location.hash;
}

/** Вкладки студии одного браузера делят cookie сессии. Страница входа (login.js) открывается только без сессии и
 * сообщает об этом каналом `studio-session`: остальные вкладки уходят на вход сразу, а не при следующем опросе —
 * иначе после выхода и чужого входа фоновая вкладка показывала бы данные прежней учётки, а спрашивала новую. */
export function watchSession() {
  if (!('BroadcastChannel' in globalThis)) return;
  new BroadcastChannel('studio-session').addEventListener('message', (event) => {
    if (event.data === 'signed-out') toLogin();
  });
}

/** Профиль заново. В браузере за это время вошла другая учётка (канал не дошёл: вкладка спала) — страница прежней
 * скрывается и перезагружается, профиль не подменяется. */
export async function loadMe() {
  const fresh = await api('GET', '/api/me');
  if (me && fresh.id !== me.id) {
    document.documentElement.classList.add('shrouded');
    location.reload();
    return new Promise(() => {});
  }
  me = fresh;
  return me;
}

export async function api(method, path, body, { signal, headers: extraHeaders, keepalive = false, unlock = true } = {}) {
  const headers = { ...extraHeaders };
  let payload;
  const token = me ? me.csrf : csrf;
  if (method !== 'GET' && token) headers['x-studio-csrf'] = token;
  if (body instanceof FormData || body instanceof Blob) payload = body;   // Blob (файл) — как есть, тип — в headers
  else if (body !== undefined) {
    headers['content-type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', signal, keepalive });
  } catch (err) {
    // обрыв и срок (AbortSignal.timeout отклоняет TimeoutError) — как есть: это не «нет связи»
    if (err && (err.name === 'AbortError' || err.name === 'TimeoutError')) throw err;
    throw new ApiError(0, 'network', tr("Нет связи с сервером"));
  }
  if (!response.ok) {
    let code = 'http';
    let message = tr("Ошибка сервера ({0})", response.status);
    let fields = null;
    try {
      const detail = (await response.json()).detail;
      if (detail && typeof detail === 'object') {
        code = detail.code || code;
        message = detail.message || message;
        fields = detail;
      } else if (typeof detail === 'string') message = detail;
    } catch {
      // тело не JSON — остаётся общий текст
    }
    if (code === 'login') toLogin();
    if (code === 'email_required') location.href = '/account/email';
    if (code === 'screen_locked' && hooks.locked && unlock) {
      await hooks.locked();
      return api(method, path, body, { signal, headers: extraHeaders });
    }
    throw new ApiError(response.status, code, message, fields);
  }
  if (response.status === 204) return null;
  if ((response.headers.get('content-type') || '').startsWith('application/json')) return response.json();
  return response;
}
