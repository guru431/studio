/* Запросы к API студии.
 *
 * api(метод, путь, тело): путь — полный, '/api/...'; тело — объект (уходит JSON) или FormData.
 * Не-GET несёт заголовок CSRF из me.csrf (профиля ещё нет — из useCsrf). Ответ: JSON → объект, 204 → null, прочее (например,
 * поток text/event-stream для readSSE) → сам Response. Отказ → ApiError(status, code, message)
 * из `detail` ответа; code 'login' (сессия кончилась) уводит на страницу входа.
 * 423 `screen_locked` (студия заперта, lock.js) при заданном hooks.locked ждёт отпирания и повторяет
 * тот же запрос: опрос хода задания не показывает ошибку из-за блокировки.
 *
 * Модули разделов импортируют './api.js' — один экземпляр на страницу (app.js — не импортировать:
 * его адрес с ?v= и второй импорт без версии запустил бы оболочку дважды). */

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
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

export async function api(method, path, body) {
  const headers = {};
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
    response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(0, 'network', 'Нет связи с сервером');
  }
  if (!response.ok) {
    let code = 'http';
    let message = `Ошибка сервера (${response.status})`;
    try {
      const detail = (await response.json()).detail;
      if (detail && typeof detail === 'object') {
        code = detail.code || code;
        message = detail.message || message;
      } else if (typeof detail === 'string') message = detail;
    } catch {
      // тело не JSON — остаётся общий текст
    }
    if (code === 'login') location.href = '/login';
    if (code === 'screen_locked' && hooks.locked) {
      await hooks.locked();
      return api(method, path, body);
    }
    throw new ApiError(response.status, code, message);
  }
  if (response.status === 204) return null;
  if ((response.headers.get('content-type') || '').startsWith('application/json')) return response.json();
  return response;
}
