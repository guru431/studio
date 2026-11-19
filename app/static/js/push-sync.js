/* Досылка подписки push, которую служебный работник не смог отправить сам (sw.js, pushsubscriptionchange).
 *
 * Браузер сменил подписку, пока студия заперта: изменяющий POST /api/me/push запертой сессии закрыт (lock_policy —
 * иначе запертая сессия могла бы увести уведомления), работник подписку оставляет и шлёт окнам `{push: 'resync'}`.
 * Страница при запуске и по этому сообщению сверяет признак адреса сервера (`GET /api/me/push` → `endpoint`, первые
 * 16 hex SHA-256 адреса) с подпиской браузера и, если они разошлись, отправляет подписку браузера. Запертая сессия
 * получит 423 — api.js дождётся отпирания и повторит запрос, так что подписка уходит только после отпирания.
 * Только при флаге устройства `studio-push` (его ставят «Настройки») и подписке этой сессии на сервере: включать push
 * сама страница не берётся.
 *
 * Язык уведомлений: смена языка интерфейса перезагружает страницу (i18n.js), и при запуске страница сверяет языки
 * push и ntfy этой сессии (`me.notify_langs`) с языком устройства — разошлись, `PATCH /api/me` `notify_lang`. */
import { api, me } from './api.js';
import { language } from './i18n.js';

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
  await api('POST', '/api/me/push', { ...sub.toJSON(), lang: language() });
}

/** Сверить один раз (одновременные вызовы — один запрос); сбой — молча, следующий запуск попробует снова. */
export function syncPush() {
  running ??= sync().catch(() => null).finally(() => { running = null; });
  return running;
}

/** Уведомления этой сессии — на языке интерфейса устройства; сбой — молча, следующий запуск попробует снова. */
function syncLanguage() {
  const langs = me && Array.isArray(me.notify_langs) ? me.notify_langs : [];
  if (!langs.some((lang) => lang !== language())) return;
  api('PATCH', '/api/me', { notify_lang: language() })
    .then((answer) => { if (answer && me) me.notify_langs = answer.notify_langs; })
    .catch(() => null);
}

export function setupPushSync() {
  syncLanguage();
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.push === 'resync') syncPush();
  });
  syncPush();
}
