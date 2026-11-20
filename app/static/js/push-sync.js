/* Досылка подписки push, которую служебный работник не смог отправить сам (sw.js, pushsubscriptionchange).
 *
 * Браузер сменил подписку, пока студия заперта: изменяющий POST /api/me/push запертой сессии закрыт (lock_policy —
 * иначе запертая сессия могла бы увести уведомления), работник подписку оставляет и шлёт окнам `{push: 'resync'}`.
 * Страница при запуске и по этому сообщению сверяет признак адреса сервера (`GET /api/me/push` → `endpoint`, первые
 * 16 hex SHA-256 адреса) с подпиской браузера и, если они разошлись, отправляет подписку браузера. Запертая сессия
 * получит 423 — api.js дождётся отпирания и повторит запрос, так что подписка уходит только после отпирания.
 * Только при флаге устройства `studio-push:<id учётки>` (его ставят «Настройки») и подписке этой сессии на сервере:
 * включать push сама страница не берётся.
 *
 * Подписка на сервере живёт с сессией: выход, конец срока, «Завершить», аварийный выход её снимают. Флаг стоит,
 * разрешение и подписка браузера живы, а у сессии подписки нет — плашка «Push выключился после выхода из учётки»:
 * «Включить» отправляет ту же подписку браузера, «Не нужно» снимает флаг. Без нажатия push не включается.
 *
 * Язык уведомлений: смена языка интерфейса перезагружает страницу (i18n.js), и при запуске страница сверяет языки
 * push и ntfy этой сессии (`me.notify_langs`) с языком устройства — разошлись, `PATCH /api/me` `notify_lang`. */
import { api, me } from './api.js';
import { language, tr } from './i18n.js';
import { el, words } from './ui.js';
import { forgetRemembered } from './ntfy-settings.js';

const PUSH_KEY = 'studio-push';
let running = null;

async function mark(endpoint) {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return Array.from(new Uint8Array(hash).slice(0, 8), (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Флаг устройства «push включён» для учётки `me` (только '1'; ключей и адреса подписки в localStorage нет):
 * `pushFlag()` — прочесть, `pushFlag(true|false)` — поставить или снять. Прежний общий флаг `studio-push`
 * переходит первой учётке, что его прочтёт. */
export function pushFlag(on) {
  const key = `${PUSH_KEY}:${me.id}`;
  try {
    if (on === undefined) {
      if (localStorage.getItem(key) === null && localStorage.getItem(PUSH_KEY) === '1') {
        localStorage.setItem(key, '1');
        localStorage.removeItem(PUSH_KEY);
      }
      return localStorage.getItem(key) === '1';
    }
    if (on) localStorage.setItem(key, '1');
    else localStorage.removeItem(key);
  } catch {
    // хранилище недоступно: флажок «Настроек» покажет состояние сервера при следующем открытии
  }
  return false;
}

/** Плашка «Push выключился после выхода»: одна на страницу, уходит после выбора. */
function offer(sub) {
  if (document.getElementById('push-restore')) return;
  const text = el('span', { id: 'push-restore-text' }, tr("Push на этом устройстве выключился после выхода из учётки"));
  const yes = el('button', { type: 'button', class: 'btn btn-accent', id: 'push-restore-yes' }, tr("Включить"));
  const no = el('button', { type: 'button', class: 'btn', id: 'push-restore-no' }, tr("Не нужно"));
  const bar = el('div', { id: 'push-restore', class: 'update-bar', role: 'status' }, text, yes, no);
  yes.addEventListener('click', async () => {
    yes.disabled = no.disabled = true;
    try {
      await api('POST', '/api/me/push', { ...sub.toJSON(), lang: language() });
      bar.remove();
    } catch (err) {
      text.textContent = words(err);
      yes.disabled = no.disabled = false;
    }
  });
  no.addEventListener('click', () => {
    pushFlag(false);
    bar.remove();
  });
  document.body.append(bar);
}

async function sync() {
  if (!pushFlag() || !('serviceWorker' in navigator) || !('PushManager' in window) || !crypto.subtle) return;
  const registration = await navigator.serviceWorker.getRegistration('/');
  const sub = registration ? await registration.pushManager.getSubscription() : null;
  if (!sub) return;                      // подписки браузера нет — флажок «Настроек» покажет это сам (pushBox)
  const answer = await api('GET', '/api/me/push');
  if (!answer || typeof answer.subscribed !== 'boolean') return;
  if (!answer.subscribed) {              // сессия новая: подписка прежней ушла с ней
    if ('Notification' in window && Notification.permission === 'granted') offer(sub);
    return;
  }
  if (answer.endpoint === await mark(sub.endpoint)) return;
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

// Явный «Выйти» забывает сервер и тему ntfy этого браузера; конец срока, «Завершить» с другого устройства и
// аварийный выход запомненное не трогают — после нового входа форма их подставит.
document.addEventListener('submit', (event) => {
  if (event.target.matches('form[action="/logout"]')) forgetRemembered();
});

export function setupPushSync() {
  syncLanguage();
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.push === 'resync') syncPush();
  });
  syncPush();
}
