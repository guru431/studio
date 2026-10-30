/* Один снимок Git открытого проекта для шапки, дерева и вкладки Git. Только память страницы. */
import { api } from './api.js';

export function projectGit(aid, pid) {
  let state = { data: null, error: null, loading: false };
  let pending = null;
  let readAt = 0;
  let alive = true;
  let gen = 0;
  const listeners = new Set();
  function publish(next) {
    if (!alive) return;
    state = next;
    for (const fn of listeners) fn(state);
  }
  return {
    subscribe(fn) {
      listeners.add(fn);
      fn(state);
      return () => listeners.delete(fn);
    },
    refresh(force = false) {
      if (!alive) return Promise.resolve();
      if (pending) return pending;
      // Переключение вида сразу после открытия проекта использует тот же снимок.
      if (!force && readAt && Date.now() - readAt < 5000) return Promise.resolve();
      publish({ ...state, loading: true });
      const mine = gen;
      pending = api('GET', `/api/code/agents/${aid}/projects/${pid}/git`).then(
        (data) => { if (mine === gen) publish({ data, error: null, loading: false }); },
        (error) => { if (mine === gen) publish({ data: null, error, loading: false }); },
      ).finally(() => {
        if (mine === gen) {
          readAt = Date.now();
          pending = null;
        }
      });
      return pending;
    },
    clear() {
      gen += 1;
      pending = null;
      publish({ data: null, error: null, loading: false });
      readAt = 0;
    },
    unmount() {
      alive = false;
      listeners.clear();
    },
  };
}

const MARKS = {
  conflict: ['!', 'Конфликт слияния'], deleted: ['D', 'Удалён'], added: ['A', 'Добавлен в индекс'],
  untracked: ['U', 'Не отслеживается'], renamed: ['R', 'Переименован'], modified: ['M', 'Изменён'],
};

export function fileMark(status) {
  const s = String(status || '').trim();
  let kind;
  if (s === '??') kind = 'untracked';
  else if (/U/.test(s) || s === 'AA' || s === 'DD') kind = 'conflict';
  else if (s.includes('D')) kind = 'deleted';
  else if (s.includes('R')) kind = 'renamed';
  else if (s.includes('A')) kind = 'added';
  else kind = 'modified';
  const [label, title] = MARKS[kind];
  return { kind, label, title };
}
