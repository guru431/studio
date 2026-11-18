/* Полная перезагрузка сохраняет адрес чата и метку истории. Одноразовый флаг в записи
 * переживает reload без local/sessionStorage и просит раздел заново прочитать кэши моста. */
import { api } from './api.js';
import { WINDOW } from './code-window.js';

const KEY = 'studioRefresh';
const RETRIES = 'studioRefreshRetries';
export const refreshRequested = history.state?.[KEY] === true;
if (refreshRequested) {
  const state = { ...history.state };
  delete state[KEY];
  history.replaceState(state, '');
}

let pending = null;
let timer;
let probing = false;
const delays = [5000, 10000, 20000, 40000, 60000];
let attempts = history.state?.[RETRIES]?.hash === location.hash ? history.state[RETRIES].attempts : 0;
if (attempts) {
  const state = { ...history.state };
  delete state[RETRIES];
  history.replaceState(state, '');
}

function reload(automatic) {
  clearTimeout(timer);
  pending = null;
  const state = { ...history.state, [KEY]: true };
  if (automatic) state[RETRIES] = { hash: location.hash, attempts };
  else delete state[RETRIES];
  history.replaceState(state, '');
  location.reload();
}

export function reloadStudio() { reload(false); }

export function refreshAfterFailure(error) {
  if (error && !['offline', 'network', 'timeout', 'busy', 'module', 'updated'].includes(error.code)
    && error.name !== 'TimeoutError' && !(error.status >= 500)) return;
  if (pending !== location.hash) {
    if (pending !== null) attempts = 0;
    pending = location.hash;
    scheduleRetry();
  }
}

function scheduleRetry() {
  clearTimeout(timer);
  if (pending === null || document.visibilityState === 'hidden') return;
  timer = setTimeout(retry, delays[Math.min(attempts, delays.length - 1)]);
}

async function retry() {
  if (probing || pending !== location.hash || document.visibilityState === 'hidden') return;
  const allowed = () => document.dispatchEvent(new Event('studio-before-auto-refresh', { cancelable: true }));
  if (!allowed()) { scheduleRetry(); return; }
  probing = true;
  const hash = pending;
  attempts += 1;
  try {
    // Не заменяем открытую страницу ошибкой браузера, пока недоступна сама Студия.
    const response = await fetch('/health', { cache: 'no-store', signal: AbortSignal.timeout(7000) });
    if (response.ok && pending === hash && location.hash === hash && document.visibilityState !== 'hidden'
      && allowed()) {
      reload(true);
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
