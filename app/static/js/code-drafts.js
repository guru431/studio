import { me } from './api.js';

const KEY = 'studio-agent-drafts';
let scope;
let items = {};

function current() {
  if (!me || scope === me.csrf) return items;
  scope = me.csrf;
  items = {};
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY));
    if (saved?.scope === scope && saved.items && typeof saved.items === 'object' && !Array.isArray(saved.items)) {
      items = saved.items;
    }
    else sessionStorage.removeItem(KEY);
  } catch { /* В памяти страницы черновики работают и без хранилища. */ }
  return items;
}

function persist() {
  if (!scope) return;
  try {
    if (Object.keys(items).length) sessionStorage.setItem(KEY, JSON.stringify({ scope, items }));
    else sessionStorage.removeItem(KEY);
  } catch { /* Хранилище может быть отключено или заполнено. */ }
}

export function savedDrafts() { return current(); }

export function saveDraft(key, text, receipt) {
  const active = current();
  if (text) active[key] = { text, ...(receipt ? { receipt } : {}) };
  else delete active[key];
  persist();
}

function clear() {
  items = {};
  try { sessionStorage.removeItem(KEY); } catch { /* Хранилище закрыто. */ }
}

document.addEventListener('studio-locked', clear);
document.addEventListener('studio-signed-out', clear);     // api.js toLogin, аварийный выход; login.js — тоже
document.addEventListener('submit', (event) => {
  if (event.target.matches('form[action="/logout"]')) clear();
});
