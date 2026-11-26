/* Черновики диалогов в sessionStorage: `{scope: me.csrf, items: {id: {text, receipt?, skill?}}}`.
 * Запись чужой сессии (другой `scope`) не читается и удаляется. Смена «есть / нет» — событие
 * `studio-dialog-draft` { store, id, draft } на document. Блокировка, выход и аварийный выход стирают черновики. */
import { me } from './api.js';

export const AGENT_DRAFTS = 'studio-agent-drafts';
export const CHAT_DRAFTS = 'studio-chat-drafts';
export const OWUI_DRAFTS = 'studio-owui-drafts';
export const owuiDraftKey = (aid, chat) => `${aid}/${chat ?? ''}`;
const KEYS = [AGENT_DRAFTS, CHAT_DRAFTS, OWUI_DRAFTS];
const stores = new Map();         // ключ → хранилище
const clears = new Map();         // ключ → стереть хранилище
export let draftsLocked = false;
new MutationObserver(() => {
  if (!document.documentElement.classList.contains('shrouded')) draftsLocked = false;
}).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });

/** Одно хранилище на ключ. */
export function draftStore(key) {
  if (stores.has(key)) return stores.get(key);
  let scope;
  let items = {};

  function current() {
    if (!me || scope === me.csrf) return items;
    scope = me.csrf;
    items = {};
    try {
      const saved = JSON.parse(sessionStorage.getItem(key));
      if (saved?.scope === scope && saved.items && typeof saved.items === 'object' && !Array.isArray(saved.items)) {
        items = saved.items;
      }
      else sessionStorage.removeItem(key);
    } catch { /* В памяти страницы черновики работают и без хранилища. */ }
    return items;
  }

  function persist() {
    if (!scope) return;
    try {
      if (Object.keys(items).length) sessionStorage.setItem(key, JSON.stringify({ scope, items }));
      else sessionStorage.removeItem(key);
    } catch { /* Хранилище может быть отключено или заполнено. */ }
  }

  function announce(id, had) {
    const draft = Object.hasOwn(items, id);
    if (had !== draft) document.dispatchEvent(new CustomEvent('studio-dialog-draft', { detail: { store: key, id, draft } }));
  }

  const store = {
    saved() { return current(); },
    has(id) { return Object.hasOwn(current(), id); },
    save(id, text, receipt, skill) {
      const active = current();
      const had = Object.hasOwn(active, id);
      if (text || skill) active[id] = { text, ...(receipt ? { receipt } : {}), ...(skill ? { skill } : {}) };
      else delete active[id];
      persist();
      announce(id, had);
    },
  };
  stores.set(key, store);
  clears.set(key, () => {
    const gone = Object.keys(items);
    items = {};
    for (const id of gone) announce(id, true);
  });
  return store;
}

function clear() {
  for (const key of KEYS) {
    clears.get(key)?.();
    try { sessionStorage.removeItem(key); } catch { /* Хранилище закрыто. */ }
  }
}

document.addEventListener('studio-locked', () => {
  draftsLocked = true;
  clear();
});
document.addEventListener('studio-signed-out', clear);     // api.js toLogin, аварийный выход; login.js — тоже
document.addEventListener('submit', (event) => {
  if (event.target.matches('form[action="/logout"]')) clear();
});
