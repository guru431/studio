/* Полная перезагрузка сохраняет адрес чата и метку истории. Одноразовый флаг в записи
 * переживает reload без local/sessionStorage и просит раздел заново прочитать кэши моста. */
import { api } from './api.js';
import { WINDOW } from './code-window.js';

const KEY = 'studioRefresh';
export const refreshRequested = history.state?.[KEY] === true;
if (refreshRequested) {
  const state = { ...history.state };
  delete state[KEY];
  history.replaceState(state, '');
}

export function reloadStudio() {
  history.replaceState({ ...history.state, [KEY]: true }, '');
  location.reload();
}

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
