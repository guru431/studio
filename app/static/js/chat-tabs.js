/* Вкладки диалогов «Чата» над лентой (спека 2026-11-25-studio-skills-status-local-models, п. 26); вид — ряд вкладок
 * «Агентов» (.code-tabs-row, .code-tabbar, .code-tab).
 *
 * chatTabs({ open, create, info }) → { node, show(chat | null), sync(ids), unmount() }:
 * - node — div#chat-tabs > div#chat-tabbar[role=tablist] > div.code-tab[role=tab][data-chat][aria-selected]:
 *   название (info(id).title, пустое — «Новый диалог»), ✎ черновика (draftStore(CHAT_DRAFTS)), метка «идёт ответ»
 *   (info(id).busy) и «×»; «+» открывает новый диалог;
 * - show(chat) — диалог встаёт в конец ряда, если его нет (при TABS_MAX уходит самая левая не текущая), становится
 *   выбранным и прокручивается в видимую часть ряда; null (новый диалог) — выбранной нет;
 * - sync(ids) — вкладки диалогов, которых нет в удачно загруженном списке, уходят;
 * - нажатие — open(id); «×» и Delete убирают вкладку, у текущей — open(соседняя ?? null); ← / → / Home / End — фокус
 *   по ряду. Перетаскивания нет.
 * Состав — на устройстве: localStorage['studio-chat-tabs'] = {user: me.id, ids} — только id; чужой `user` или мусор —
 * пусто и ключ удалён. Выход из учётки (`studio-signed-out`, отправка form[action="/logout"]) удаляет ключ — слушатели
 * уровня модуля (app.js подключает модуль сразу), страница входа — тоже (login.js). */
import { tr } from './i18n.js';
import { me } from './api.js';
import { icon } from './icons.js';
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';
import { el } from './ui.js';

export const TABS_MAX = 12;
const KEY = 'studio-chat-tabs';

function forget() {
  try { localStorage.removeItem(KEY); } catch { /* Хранилище закрыто. */ }
}

document.addEventListener('studio-signed-out', forget);
document.addEventListener('submit', (event) => {
  if (event.target.matches('form[action="/logout"]')) forget();
});

function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY));
    if (saved === null) return [];
    if (me && saved?.user === me.id && Array.isArray(saved.ids)
      && saved.ids.every((id) => typeof id === 'string' && /^[\w-]+$/.test(id))) return [...new Set(saved.ids)].slice(-TABS_MAX);
  } catch { /* Мусор в ключе — как чужой. */ }
  forget();
  return [];
}

export function chatTabs({ open, create, info }) {
  const drafts = draftStore(CHAT_DRAFTS);
  const bar = el('div', { id: 'chat-tabbar', class: 'code-tabbar', role: 'tablist', 'aria-label': tr("Открытые диалоги") });
  const add = el('button', { type: 'button', id: 'chat-tab-add', class: 'code-tab-add', 'aria-label': tr("Новый чат"),
    title: tr("Новый чат") }, '+');
  add.addEventListener('click', create);
  bar.append(add);
  const node = el('div', { id: 'chat-tabs', class: 'code-tabs-row chat-tabs' }, bar);
  let ids = restore();
  let active = null;
  let painted = null;         // с чем нарисован ряд: тот же — без перерисовки

  function save() {
    try {
      if (ids.length && me) localStorage.setItem(KEY, JSON.stringify({ user: me.id, ids }));
      else localStorage.removeItem(KEY);
    } catch { /* Ряд живёт и без хранилища. */ }
  }

  function close(id) {
    const index = ids.indexOf(id);
    if (index < 0) return;
    const focused = bar.contains(document.activeElement);
    ids.splice(index, 1);
    save();
    const next = ids[index] ?? ids[index - 1] ?? null;
    if (id === active) {
      active = next;
      paint();
      open(next);
    } else paint();
    if (focused && ids.length) bar.querySelector(`.code-tab[data-chat="${ids[Math.min(index, ids.length - 1)]}"]`)?.focus();
  }

  function tabNode(id) {
    const known = info(id) || {};
    const title = known.title || tr("Новый диалог");
    const draft = drafts.has(id) ? el('span', { class: 'code-tab-draft', title: tr("есть черновик") },
      el('span', { 'aria-hidden': 'true' }, '✎'), el('span', { class: 'sr' }, tr("есть черновик"))) : null;
    const mark = known.busy ? el('span', { class: 'code-tab-mark running', title: tr("идёт ответ") },
      el('span', { class: 'sr' }, tr("идёт ответ"))) : null;
    const shut = el('button', { type: 'button', tabindex: '-1', class: 'code-tab-close', 'aria-label': tr("Закрыть вкладку"),
      title: tr("Закрыть вкладку") }, icon('x'));
    const tab = el('div', { class: 'code-tab', role: 'tab', 'data-chat': id, 'aria-selected': String(id === active),
      tabindex: id === (active && ids.includes(active) ? active : ids[0]) ? '0' : '-1' },
      el('span', { class: 'code-tab-title', title }, title), draft, mark, shut);
    tab.addEventListener('click', (event) => {
      if (!event.target.closest('.code-tab-close') && id !== active) open(id);
    });
    shut.addEventListener('click', () => close(id));
    tab.addEventListener('keydown', (event) => {
      if (event.target !== tab) return;
      const i = ids.indexOf(id);
      const keys = { ArrowLeft: (i + ids.length - 1) % ids.length, ArrowRight: (i + 1) % ids.length, Home: 0, End: ids.length - 1 };
      if (Object.hasOwn(keys, event.key)) {
        event.preventDefault();
        bar.querySelectorAll('.code-tab')[keys[event.key]]?.focus();
      } else if (event.key === 'Delete') {
        event.preventDefault();
        close(id);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (id !== active) open(id);
      }
    });
    return tab;
  }

  function paint() {
    const known = ids.map((id) => {
      const it = info(id) || {};
      return [id, it.title || '', !!it.busy, drafts.has(id)];
    });
    const signature = JSON.stringify([active, known]);
    if (signature === painted) return false;
    painted = signature;
    const focused = document.activeElement?.closest?.('.code-tab');
    const keep = focused && bar.contains(focused) ? focused.dataset.chat : null;
    for (const tab of bar.querySelectorAll('.code-tab')) tab.remove();
    add.before(...ids.map(tabNode));
    if (keep) bar.querySelector(`.code-tab[data-chat="${keep}"]`)?.focus({ preventScroll: true });
    return true;
  }

  /** Выбранная — в видимой части ряда (телефон листает ряд вбок). */
  function reveal() {
    const tab = bar.querySelector('.code-tab[aria-selected="true"]');
    if (!tab) return;
    if (tab.offsetLeft < bar.scrollLeft) bar.scrollLeft = tab.offsetLeft;
    else if (tab.offsetLeft + tab.offsetWidth > bar.scrollLeft + bar.clientWidth - add.offsetWidth) {
      bar.scrollLeft = tab.offsetLeft + tab.offsetWidth + add.offsetWidth - bar.clientWidth;
    }
  }

  const onDraft = (event) => {
    if (event.detail?.store === CHAT_DRAFTS && ids.includes(event.detail.id)) paint();
  };
  document.addEventListener('studio-dialog-draft', onDraft);

  return {
    node,
    show(chat) {
      const id = chat ? chat.id : null;
      if (id !== null && !ids.includes(id)) {
        if (ids.length >= TABS_MAX) ids.shift();       // новая ещё не в ряду: самая левая — не текущая
        ids.push(id);
        save();
      }
      const moved = active !== id;
      active = id;
      paint();
      if (moved) reveal();
    },
    sync(listed) {
      const known = new Set(listed);
      const kept = ids.filter((id) => known.has(id));
      if (kept.length !== ids.length) {
        ids = kept;
        save();
      }
      if (paint()) reveal();                         // названия пришли — ширина вкладок сменилась
    },
    unmount() {
      node.remove();
      document.removeEventListener('studio-dialog-draft', onDraft);
    },
  };
}
