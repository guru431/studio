/* Полная перезагрузка сохраняет адрес чата и метку истории. Одноразовый флаг в записи
 * переживает reload без local/sessionStorage и просит раздел заново прочитать кэши моста.
 * Флаг `true` — кнопка «Обновить»: после загрузки окно забирает управление всеми идущими сессиями
 * (claimRequested). Автоматическая перезагрузка после сбоя ставит `'auto'`: кэши читаются заново, а сессии других
 * окон не трогаются; своё окно остаётся владельцем, потому что id окна переживает перезагрузку (code-window.js).
 * Компьютер не на связи (409 `offline`) — не сбой страницы: перезагрузка его не вернёт, поток live ждёт `online` сам.
 * Несохранённое молча не теряется: идущая загрузка файла (holdRefresh) или вето события `studio-before-auto-refresh`
 * (черновики полей, запись голоса, ответ, который читает вкладка, правка файла или расшифровки) — вместо перезагрузки
 * плашка #studio-refresh-offer с «Обновить», автоматических попыток для этого адреса больше нет. */
import { api } from './api.js';
import { WINDOW } from './code-window.js';
import { tr } from './i18n.js';

const KEY = 'studioRefresh';
const RETRIES = 'studioRefreshRetries';
const OWNER = 'studioWindow';
const asked = history.state?.[KEY];
export const refreshRequested = asked === true || asked === 'auto';
export const claimRequested = asked === true;
if (asked !== undefined) {
  const state = { ...history.state };
  delete state[KEY];
  history.replaceState(state, '');
}

let pending = null;
let timer;
let probing = false;
let cancels = 0;             // cancelRefresh: проба /health в пути не перезагружает, даже если сбой снова взвёл тот же адрес
let claimAgain = false;      // повтор кнопки «Обновить», перенёсшей не все сессии, — тоже с переносом
let offered = false;         // перезагрузку запретило несохранённое — плашка вместо неё, до перехода в другой адрес
let uploads = 0;             // идущие загрузки файлов (holdRefresh)
const delays = [5000, 10000, 20000, 40000, 60000];
/** Попыток подряд для одного адреса (счёт переживает перезагрузку): дальше — только кнопка «Обновить». */
const ATTEMPTS_MAX = 10;
let attempts = history.state?.[RETRIES]?.hash === location.hash ? history.state[RETRIES].attempts : 0;
if (attempts) {
  const state = { ...history.state };
  delete state[RETRIES];
  history.replaceState(state, '');
}

function reload(automatic) {
  clearTimeout(timer);
  pending = null;
  const state = { ...history.state, [KEY]: automatic && !claimAgain ? 'auto' : true, [OWNER]: WINDOW };
  if (automatic) state[RETRIES] = { hash: location.hash, attempts };
  else delete state[RETRIES];
  history.replaceState(state, '');
  location.reload();
}

export function reloadStudio() { reload(false); }

/** Сбой связи, срока, сервера или модуля — перезагрузка с паузами. `claim` — повтор кнопки «Обновить» (app.js: не все
 * сессии перенеслись): он и перенесёт оставшиеся. */
export function refreshAfterFailure(error, { claim = false } = {}) {
  if (claim) claimAgain = true;
  if (error && !['network', 'timeout', 'busy', 'module', 'updated'].includes(error.code)
    && error.name !== 'TimeoutError' && !(error.status >= 500)) return;
  if (pending !== location.hash) {
    if (pending !== null) attempts = 0;
    pending = location.hash;
    scheduleRetry();
  }
}

/** Сбой разбирают на месте (app.js: повтор профиля при запуске) — отложенная перезагрузка не нужна; отказ повтора
 * снова зовёт refreshAfterFailure. Проба `/health`, начатая тем же `online`, после этого не перезагрузит. */
export function cancelRefresh() {
  clearTimeout(timer);
  pending = null;
  cancels += 1;
}

/** Идущая загрузка файла (XHR разделов): пока она не кончилась, автоматическая перезагрузка её не оборвёт. */
export function holdRefresh(xhr) {
  uploads += 1;
  xhr.addEventListener('loadend', () => { uploads -= 1; }, { once: true });
}

function allowed() {
  return !uploads && document.dispatchEvent(new Event('studio-before-auto-refresh', { cancelable: true }));
}

/** Плашка вместо молчаливой перезагрузки: несохранённое остаётся, обновить — по нажатию (как автоматическая). */
function offer() {
  offered = true;
  clearTimeout(timer);
  if (document.getElementById('studio-refresh-offer')) return;
  const bar = document.createElement('div');
  bar.id = 'studio-refresh-offer';
  bar.className = 'update-bar';
  bar.setAttribute('role', 'status');
  const words = document.createElement('span');
  words.textContent = tr("Связь восстановилась — обновите Студию, когда сохраните изменения");
  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'btn btn-accent';
  go.id = 'studio-refresh-go';
  go.textContent = tr("Обновить");
  go.addEventListener('click', () => reload(true));
  bar.append(words, go);
  document.body.append(bar);
}

function scheduleRetry() {
  clearTimeout(timer);
  if (pending === null || offered || document.visibilityState === 'hidden' || attempts >= ATTEMPTS_MAX) return;
  timer = setTimeout(retry, delays[Math.min(attempts, delays.length - 1)]);
}

async function retry() {
  if (probing || offered || pending !== location.hash || document.visibilityState === 'hidden') return;
  probing = true;
  const hash = pending;
  const cancelled = cancels;
  attempts += 1;
  try {
    // Не заменяем открытую страницу ошибкой браузера, пока недоступна сама Студия.
    const response = await fetch('/health', { cache: 'no-store', signal: AbortSignal.timeout(7000) });
    if (response.ok && cancelled === cancels && pending === hash && location.hash === hash && document.visibilityState !== 'hidden') {
      if (allowed()) reload(true);
      else offer();
      return;
    }
  } catch { /* Следующая попытка проверит восстановление связи. */ }
  finally { probing = false; }
  scheduleRetry();
}

window.addEventListener('hashchange', () => {
  if (pending === location.hash) return;
  pending = null;
  attempts = 0;
  claimAgain = false;
  offered = false;
  document.getElementById('studio-refresh-offer')?.remove();
  clearTimeout(timer);
});
window.addEventListener('online', retry);
document.addEventListener('visibilitychange', scheduleRetry);

/** То же «Продолжить здесь» для всех работающих сессий доступных компьютеров.
 * Каталог активности не открывает PIN. Независимые отказы не мешают остальным переносам;
 * внешний Codex writer не копируем (fork: false), завершённые сессии не трогаем. */
export async function claimActiveSessions() {
  const [{ items: activity }, { items: agents }] = await Promise.all([
    api('GET', '/api/code/activity', undefined, { signal: AbortSignal.timeout(10000) }),
    api('GET', '/api/agents', undefined, { signal: AbortSignal.timeout(10000) }),
  ]);
  const capable = new Set(agents.filter((agent) => agent.chat_control).map((agent) => agent.id));
  const requests = activity.filter(({ aid }) => capable.has(aid)).flatMap(({ aid, items }) =>
    items.filter((item) => item.running || item.asking).map((item) => {
      const suffix = item.engine && item.engine !== 'claude' ? `?engine=${encodeURIComponent(item.engine)}` : '';
      return api('POST', `/api/code/agents/${aid}/projects/${item.project}/chats/${item.chat}/claim${suffix}`,
        { fork: false }, { headers: { 'x-studio-window': WINDOW }, signal: AbortSignal.timeout(10000) });
    }));
  const results = await Promise.allSettled(requests);
  return results.filter((result) => result.status === 'rejected' || result.value?.available === false).length;
}
