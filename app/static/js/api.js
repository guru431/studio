/* Запросы к API студии.
 *
 * api(метод, путь, тело, { signal, headers }): путь — полный, '/api/...'; тело — объект (уходит JSON) или FormData;
 * signal — обрыв запроса (AbortSignal, например срок).
 * Не-GET несёт заголовок CSRF из me.csrf (профиля ещё нет — из useCsrf). Ответ: JSON → объект, 204 → null, прочее (например,
 * поток text/event-stream для readSSE) → сам Response. Отказ → ApiError(status, code, message, detail)
 * из `detail` ответа; code 'login' (сессия кончилась) уводит на страницу входа.
 * 423 `screen_locked` (студия заперта, lock.js) при заданном hooks.locked ждёт отпирания и повторяет
 * тот же запрос: опрос хода задания не показывает ошибку из-за блокировки.
 *
 * Модули разделов импортируют './api.js' — один экземпляр на страницу (app.js — не импортировать:
 * его адрес с ?v= и второй импорт без версии запустил бы оболочку дважды). */

export class ApiError extends Error {
  constructor(status, code, message, detail = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;        // объект detail ответа целиком (например, left у неверного PIN)
  }
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

export async function loadMe() {
  me = await api('GET', '/api/me');
  return me;
}

export async function api(method, path, body, { signal, headers: extraHeaders } = {}) {
  const headers = { ...extraHeaders };
  let payload;
  const token = me ? me.csrf : csrf;
  if (method !== 'GET' && token) headers['x-studio-csrf'] = token;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers['content-type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin', signal });
  } catch (err) {
    // обрыв и срок (AbortSignal.timeout отклоняет TimeoutError) — как есть: это не «нет связи»
    if (err && (err.name === 'AbortError' || err.name === 'TimeoutError')) throw err;
    throw new ApiError(0, 'network', 'Нет связи с сервером');
  }
  if (!response.ok) {
    let code = 'http';
    let message = `Ошибка сервера (${response.status})`;
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
    if (code === 'login') location.href = '/login';
    if (code === 'screen_locked' && hooks.locked) {
      await hooks.locked();
      return api(method, path, body, { signal, headers: extraHeaders });
    }
    throw new ApiError(response.status, code, message, fields);
  }
  if (response.status === 204) return null;
  if ((response.headers.get('content-type') || '').startsWith('application/json')) return response.json();
  return response;
}
