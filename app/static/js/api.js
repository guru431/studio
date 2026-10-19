/* Запросы к API студии.
 *
 * api(метод, путь, тело): путь — полный, '/api/...'; тело — объект (уходит JSON) или FormData.
 * Не-GET несёт заголовок CSRF из me.csrf. Ответ: JSON → объект, 204 → null, прочее (например,
 * поток text/event-stream для readSSE) → сам Response. Отказ → ApiError(status, code, message)
 * из `detail` ответа; code 'login' (сессия кончилась) уводит на страницу входа.
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

export async function loadMe() {
  me = await api('GET', '/api/me');
  return me;
}

export async function api(method, path, body) {
  const headers = {};
  let payload;
  if (method !== 'GET' && me) headers['x-studio-csrf'] = me.csrf;
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
    throw new ApiError(response.status, code, message);
  }
  if (response.status === 204) return null;
  if ((response.headers.get('content-type') || '').startsWith('application/json')) return response.json();
  return response;
}
