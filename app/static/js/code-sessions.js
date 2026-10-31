/* Сессии проекта Claude Code (спека 2026-10-29-studio-code-workspace-design, 6.5): «Новый чат» первой строкой, затем
 * сессии в порядке сервера (открытые, затем закрытые, новые первыми) — название (без него «Новый чат»), время, откуда
 * («студия» / «VS Code или терминал»), «закрыт», «идёт ход», «ждёт ответа».
 *
 * sessionList(host, { aid, pid, items, exclude, picked, created, failed }) → { unmount() }:
 * - items — сохранённый список или обещание ответа, refresh — его фоновая сверка; поиск сохраняется при обновлении;
 *   exclude — чаты, которых в списке не надо;
 * - нажатие на сессию — picked(chat); закрытая сначала открывается снова (PATCH {state: "open"});
 * - «Новый чат» (#code-new-chat) — POST …/chats, затем created(chat);
 * - отказ запроса — failed(err) (code.js: closed — к PIN, прочее — словами).
 * Кнопок «Закрыть» / «Открыть снова» нет: закрывают из самого чата. Текст машины — только textContent. */
import { api } from './api.js';
import { ENGINE_NAMES } from './code-options.js';
import { el } from './ui.js';

const UNTITLED = 'Новый чат';

function when(ts) {
  return new Date(ts * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function sessionList(host, { aid, pid, items, refresh = null, exclude = new Set(), picked, created, failed, codex = false, opencode = false, starts = true }) {
  const base = `/api/code/agents/${aid}/projects/${pid}/chats`;
  let alive = true;
  let found = Array.isArray(items) ? items : null;
  let loadError = false;

  async function busy(button, work) {
    button.disabled = true;
    try {
      await work();
    } catch (err) {
      if (!alive) return;
      button.disabled = false;
      failed(err);
    }
  }

  const add = el('button', { type: 'button', id: 'code-new-chat', class: 'code-chat-link code-new-chat' }, 'Новый чат Claude Code');
  add.addEventListener('click', () => busy(add, async () => {
    const { chat } = await api('POST', base);
    if (alive) created(chat, 'claude');
  }));
  const addCodex = codex ? el('button', { type: 'button', id: 'code-new-codex', class: 'code-chat-link code-new-chat' },
    'Новый чат Codex') : null;
  if (addCodex) addCodex.addEventListener('click', () => busy(addCodex, async () => {
    const { chat } = await api('POST', `${base}?engine=codex`);
    if (alive) created(chat, 'codex');
  }));
  const addOpenCode = opencode ? el('button', { type: 'button', id: 'code-new-opencode', class: 'code-chat-link code-new-chat' },
    'Новый чат OpenCode') : null;
  if (addOpenCode) addOpenCode.addEventListener('click', () => busy(addOpenCode, async () => {
    const { chat } = await api('POST', `${base}?engine=opencode`);
    if (alive) created(chat, 'opencode');
  }));

  function row(chat) {
    const closed = chat.state === 'closed';
    const marks = [el('span', { class: 'tag code-origin' }, chat.origin === 'studio' ? 'студия' : 'VS Code или терминал')];
    marks.unshift(el('span', { class: 'tag code-engine' },
      ENGINE_NAMES[chat.engine || 'claude']));
    if (closed) marks.push(el('span', { class: 'tag code-closed' }, 'закрыт'));
    if (chat.asking) marks.push(el('span', { class: 'tag code-asking' }, 'ждёт ответа'));
    else if (chat.running) marks.push(el('span', { class: 'tag code-running' }, 'идёт ход'));
    const link = el('button', { type: 'button', class: 'code-chat-link' },
      el('span', { class: 'code-chat-title', title: chat.title || UNTITLED }, chat.title || UNTITLED),
      typeof chat.modified === 'number' && chat.modified > 0 ? el('span', { class: 'muted small code-chat-time' }, when(chat.modified)) : null,
      el('span', { class: 'code-marks' }, ...marks));
    link.addEventListener('click', () => busy(link, async () => {
      if (closed) await api('PATCH', `${base}/${chat.chat}${chat.engine && chat.engine !== 'claude' ? `?engine=${chat.engine}` : ''}`, { state: 'open' });
      if (alive) picked(chat.chat);
    }));
    return el('li', { 'data-chat': chat.chat, class: closed ? 'closed' : null }, link);
  }

  const list = el('ul', { id: 'code-chats', class: 'code-chats' });
  const search = el('input', { id: 'code-chat-search', class: 'input code-chat-search', type: 'search',
    placeholder: 'Поиск по названиям чатов', 'aria-label': 'Поиск по названиям чатов', autocomplete: 'off' });
  const normalize = (value) => String(value).toLocaleLowerCase('ru-RU').replace(/ё/g, 'е');
  const startsRows = [];
  if (starts) {
    startsRows.push(el('li', { class: 'code-chats-new' }, add));
    if (addCodex) startsRows.push(el('li', { class: 'code-chats-new' }, addCodex));
    if (addOpenCode) startsRows.push(el('li', { class: 'code-chats-new' }, addOpenCode));
  }
  function paint() {
    const terms = normalize(search.value).trim().split(/\s+/).filter(Boolean);
    const matches = (found || []).filter((c) => !exclude.has(c.chat)
      && terms.every((term) => normalize(c.title || UNTITLED).includes(term)));
    const empty = loadError ? 'Не удалось загрузить чаты' : found === null ? 'Загружаю…'
      : terms.length ? 'Ничего не найдено' : 'Прошлых чатов пока нет';
    list.replaceChildren(...startsRows, ...(matches.length ? matches.map(row)
      : [el('li', { class: 'code-chats-wait muted', role: 'status' }, empty)]));
  }
  search.addEventListener('input', paint);
  paint();
  const pending = refresh || (!Array.isArray(items) ? items : null);
  list.setAttribute('aria-busy', String(!!pending));
  if (pending) {
    pending.then((next) => {
      if (!alive) return;
      found = next;
      paint();
    }, (err) => {
      if (!alive) return;
      if (found === null) { loadError = true; paint(); }
      failed(err);
    }).finally(() => { if (alive) list.setAttribute('aria-busy', 'false'); });
  }
  host.append(search, list);
  return {
    unmount() {
      alive = false;
      search.remove();
      list.remove();
    },
  };
}
