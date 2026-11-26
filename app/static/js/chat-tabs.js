/* Вкладки диалогов «Чата» над лентой (спека 2026-11-25-studio-skills-status-local-models, п. 26); вид — ряд вкладок
 * «Агентов» (.code-tabs-row, .code-tabbar, .code-tab).
 *
 * chatTabs({ open, history, info, changed }) → { node, show(project, chat, choosing), sync(items), resume(project),
 * counts(), unmount() }:
 * - node — div#chat-tabs > div#chat-tabbar[role=tablist] > div.code-tab[role=tab][data-chat][aria-selected]:
 *   название (info(id).title, пустое — «Новый диалог»), ✎ черновика (draftStore(CHAT_DRAFTS)), метка «идёт ответ»
 *   (info(id).busy) и «×»; «+» открывает историю с созданием диалога;
 * - show — ряд выбранного проекта, не больше TABS_MAX; последний открытый диалог каждого проекта запоминается;
 * - sync — сверка принадлежности и существования вкладок с загруженными диалогами;
 * - нажатие — open(id); «×» и Delete убирают вкладку, у текущей — open(соседняя ?? null); ← / → / Home / End — фокус
 *   по ряду. Перетаскивания нет.
 * Состав — на устройстве: localStorage['studio-chat-tabs'] = {user: me.id, ids, active}; чужой `user` или мусор —
 * пусто и ключ удалён. Выход из учётки (`studio-signed-out`, отправка form[action="/logout"]) удаляет ключ — слушатели
 * уровня модуля (app.js подключает модуль сразу), страница входа — тоже (login.js). */
import { tr } from './i18n.js';
import { me } from './api.js';
import { icon } from './icons.js';
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';
import { el } from './ui.js';

export const TABS_MAX = 12;
const KEY = 'studio-chat-tabs';
const ID = /^[\w-]+$/;

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
    if (me && saved?.user === me.id && Array.isArray(saved.ids)
      && saved.ids.every((id) => typeof id === 'string' && ID.test(id))) {
      const ids = [...new Set(saved.ids)];
      const active = new Map(Object.entries(saved.active || {}).filter(([pid, id]) => ID.test(pid) && ids.includes(id)));
      return { ids, active };
    }
  } catch { /* Мусор в ключе — как чужой. */ }
  forget();
  return { ids: [], active: new Map() };
}

export function chatTabs({ open, history, info, changed }) {
  const drafts = draftStore(CHAT_DRAFTS);
  const bar = el('div', { id: 'chat-tabbar', class: 'code-tabbar', role: 'tablist', 'aria-label': tr("Открытые диалоги") });
  const add = el('button', { type: 'button', id: 'chat-tab-add', class: 'code-tab-add', 'aria-label': tr("Диалоги"),
    title: tr("Диалоги"), 'aria-pressed': 'false' }, '+');
  add.addEventListener('click', history);
  bar.append(add);
  const node = el('div', { id: 'chat-tabs', class: 'code-tabs-row chat-tabs' }, bar);
  const workspace = restore();
  let ids = workspace.ids;
  const remembered = workspace.active;
  let members = new Map();
  let project = null;
  let choosing = false;
  let active = null;
  let painted = null;         // с чем нарисован ряд: тот же — без перерисовки

  function save() {
    try {
      if (ids.length && me) localStorage.setItem(KEY, JSON.stringify({ user: me.id, ids, active: Object.fromEntries(remembered) }));
      else localStorage.removeItem(KEY);
    } catch { /* Ряд живёт и без хранилища. */ }
  }

  const tabsOf = (pid) => ids.filter((id) => members.get(id) === pid);

  function trim() {
    const counts = new Map();
    ids = [...ids].reverse().filter((id) => {
      const pid = members.get(id);
      if (!pid || remembered.get(pid) === id) return true;
      const count = (counts.get(pid) || 0) + 1;
      counts.set(pid, count);
      return count <= TABS_MAX - Number(remembered.has(pid));
    }).reverse();
  }

  function close(id) {
    const tabs = tabsOf(project);
    const index = tabs.indexOf(id);
    if (index < 0) return;
    const focused = bar.contains(document.activeElement);
    ids = ids.filter((tab) => tab !== id);
    const next = tabs[index + 1] ?? tabs[index - 1] ?? null;
    if (remembered.get(project) === id) {
      if (next) remembered.set(project, next);
      else remembered.delete(project);
    }
    save();
    changed();
    if (id === active) {
      active = next;
      paint();
      open(next);
    } else paint();
    if (focused && next) bar.querySelector(`.code-tab[data-chat="${next}"]`)?.focus();
  }

  function tabNode(id, tabs) {
    const known = info(id) || {};
    const title = known.title || tr("Новый диалог");
    const draft = drafts.has(id) ? el('span', { class: 'code-tab-draft', title: tr("есть черновик") },
      el('span', { 'aria-hidden': 'true' }, '✎'), el('span', { class: 'sr' }, tr("есть черновик"))) : null;
    const label = known.asking ? tr("ждёт ответа") : tr("идёт ответ");
    const mark = known.asking || known.busy ? el('span', {
      class: `code-tab-mark ${known.asking ? 'asking' : 'running'}`, title: label }, el('span', { class: 'sr' }, label)) : null;
    const shut = el('button', { type: 'button', tabindex: '-1', class: 'code-tab-close', 'aria-label': tr("Закрыть вкладку"),
      title: tr("Закрыть вкладку") }, icon('x'));
    const tab = el('div', { class: 'code-tab', role: 'tab', 'data-chat': id, 'aria-selected': String(id === active),
      tabindex: id === (active && tabs.includes(active) ? active : tabs[0]) ? '0' : '-1' },
      el('span', { class: 'code-tab-title', title }, title), draft, mark, shut);
    tab.addEventListener('click', (event) => {
      if (!event.target.closest('.code-tab-close') && id !== active) open(id);
    });
    shut.addEventListener('click', () => close(id));
    tab.addEventListener('keydown', (event) => {
      if (event.target !== tab) return;
      const visible = tabsOf(project);
      const i = visible.indexOf(id);
      const keys = { ArrowLeft: (i + visible.length - 1) % visible.length, ArrowRight: (i + 1) % visible.length,
        Home: 0, End: visible.length - 1 };
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
    const tabs = tabsOf(project);
    const known = tabs.map((id) => {
      const it = info(id) || {};
      return [id, it.title || '', !!it.busy, !!it.asking, drafts.has(id)];
    });
    const signature = JSON.stringify([project, active, choosing, known]);
    if (signature === painted) return false;
    painted = signature;
    const focused = document.activeElement?.closest?.('.code-tab');
    const keep = focused && bar.contains(focused) ? focused.dataset.chat : null;
    for (const tab of bar.querySelectorAll('.code-tab')) tab.remove();
    add.setAttribute('aria-pressed', String(choosing));
    add.before(...tabs.map((id) => tabNode(id, tabs)));
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
    if (event.detail?.store === CHAT_DRAFTS && members.get(event.detail.id) === project) paint();
  };
  document.addEventListener('studio-dialog-draft', onDraft);

  return {
    node,
    show(pid, chat, historyOpen = false) {
      const id = chat ? chat.id : null;
      const moved = project !== pid || active !== id;
      project = pid;
      choosing = historyOpen;
      if (id !== null && pid && (remembered.get(pid) !== id || members.get(id) !== pid || !ids.includes(id))) {
        const old = members.get(id);
        if (old !== pid && remembered.get(old) === id) remembered.delete(old);
        members.set(id, pid);
        if (!ids.includes(id)) ids.push(id);
        remembered.set(pid, id);
        trim();
        save();
        changed();
      }
      active = id;
      paint();
      if (moved) reveal();
    },
    sync(items) {
      members = new Map(items.map((chat) => [chat.id, chat.project_id]));
      if (active && project && !members.has(active)) members.set(active, project);
      const before = ids.length;
      const selections = remembered.size;
      ids = ids.filter((id) => members.has(id));
      for (const [pid, id] of remembered) if (members.get(id) !== pid) remembered.delete(pid);
      trim();
      if (before !== ids.length || selections !== remembered.size) save();
      if (paint()) reveal();                         // названия пришли — ширина вкладок сменилась
    },
    resume(pid) {
      const tabs = tabsOf(pid);
      const id = remembered.get(pid);
      return tabs.includes(id) ? id : tabs.at(-1);
    },
    counts() {
      const counts = new Map();
      for (const id of ids) {
        const pid = members.get(id);
        if (pid) counts.set(pid, (counts.get(pid) || 0) + 1);
      }
      return counts;
    },
    unmount() {
      node.remove();
      document.removeEventListener('studio-dialog-draft', onDraft);
    },
  };
}
