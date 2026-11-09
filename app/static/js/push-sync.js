/* Досылка подписки push, которую служебный работник не смог отправить сам (sw.js, pushsubscriptionchange).
 *
 * Браузер сменил подписку, пока студия заперта: изменяющий POST /api/me/push запертой сессии закрыт (lock_policy —
 * иначе запертая сессия могла бы увести уведомления), работник подписку оставляет и шлёт окнам `{push: 'resync'}`.
 * Страница при запуске и по этому сообщению сверяет признак адреса сервера (`GET /api/me/push` → `endpoint`, первые
 * 16 hex SHA-256 адреса) с подпиской браузера и, если они разошлись, отправляет подписку браузера. Запертая сессия
 * получит 423 — api.js дождётся отпирания и повторит запрос, так что подписка уходит только после отпирания.
 * Только при флаге устройства `studio-push` (его ставят «Настройки») и подписке этой сессии на сервере: включать push
 * сама страница не берётся. */
import { api } from './api.js';

const PUSH_KEY = 'studio-push';
let running = null;

async function mark(endpoint) {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return Array.from(new Uint8Array(hash).slice(0, 8), (b) => b.toString(16).padStart(2, '0')).join('');
}

function enabled() {
  try {
    return localStorage.getItem(PUSH_KEY) === '1';
  } catch {
    return false;
  }
}

async function sync() {
  if (!enabled() || !('serviceWorker' in navigator) || !('PushManager' in window) || !crypto.subtle) return;
  const registration = await navigator.serviceWorker.getRegistration('/');
  const sub = registration ? await registration.pushManager.getSubscription() : null;
  if (!sub) return;                      // подписки браузера нет — флажок «Настроек» покажет это сам (pushBox)
  const answer = await api('GET', '/api/me/push');
  if (!answer || !answer.subscribed || answer.endpoint === await mark(sub.endpoint)) return;
  await api('POST', '/api/me/push', sub.toJSON());
}

/** Сверить один раз (одновременные вызовы — один запрос); сбой — молча, следующий запуск попробует снова. */
export function syncPush() {
  running ??= sync().catch(() => null).finally(() => { running = null; });
  return running;
}

export function setupPushSync() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.push === 'resync') syncPush();
  });
  syncPush();
}
