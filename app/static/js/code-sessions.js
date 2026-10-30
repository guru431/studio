/* Сессии проекта Claude Code (спека 2026-10-29-studio-code-workspace-design, 6.5): «Новый чат» первой строкой, затем
 * сессии в порядке сервера (открытые, затем закрытые, новые первыми) — название (без него «Новый чат»), время, откуда
 * («студия» / «VS Code или терминал»), «закрыт», «идёт ход», «ждёт ответа».
 *
 * sessionList(host, { aid, pid, items, exclude, picked, created, failed }) → { unmount() }:
 * - items — ответ GET …/chats, exclude — чаты, которых в списке не надо (открытые вкладки);
 * - нажатие на сессию — picked(chat); закрытая сначала открывается снова (PATCH {state: "open"});
 * - «Новый чат» (#code-new-chat) — POST …/chats, затем created(chat);
 * - отказ запроса — failed(err) (code.js: closed — к PIN, прочее — словами).
 * Кнопок «Закрыть» / «Открыть снова» нет: закрывают из самого чата. Текст машины — только textContent. */
import { api } from './api.js';
import { el } from './ui.js';

const UNTITLED = 'Новый чат';

function when(ts) {
  return new Date(ts * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function sessionList(host, { aid, pid, items, exclude = new Set(), picked, created, failed }) {
  const base = `/api/code/agents/${aid}/projects/${pid}/chats`;
  let alive = true;

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

  const add = el('button', { type: 'button', id: 'code-new-chat', class: 'code-chat-link code-new-chat' }, 'Новый чат');
  add.addEventListener('click', () => busy(add, async () => {
    const { chat } = await api('POST', base);
    if (alive) created(chat);
  }));

  function row(chat) {
    const closed = chat.state === 'closed';
    const marks = [el('span', { class: 'tag code-origin' }, chat.origin === 'studio' ? 'студия' : 'VS Code или терминал')];
    if (closed) marks.push(el('span', { class: 'tag code-closed' }, 'закрыт'));
    if (chat.asking) marks.push(el('span', { class: 'tag code-asking' }, 'ждёт ответа'));
    else if (chat.running) marks.push(el('span', { class: 'tag code-running' }, 'идёт ход'));
    const link = el('button', { type: 'button', class: 'code-chat-link' },
      el('span', { class: 'code-chat-title' }, chat.title || UNTITLED),
      typeof chat.modified === 'number' && chat.modified > 0 ? el('span', { class: 'muted small code-chat-time' }, when(chat.modified)) : null,
      el('span', { class: 'code-marks' }, ...marks));
    link.addEventListener('click', () => busy(link, async () => {
      if (closed) await api('PATCH', `${base}/${chat.chat}`, { state: 'open' });
      if (alive) picked(chat.chat);
    }));
    return el('li', { 'data-chat': chat.chat, class: closed ? 'closed' : null }, link);
  }

  const list = el('ul', { id: 'code-chats', class: 'code-chats' }, el('li', { class: 'code-chats-new' }, add),
    ...items.filter((c) => !exclude.has(c.chat)).map(row));
  host.append(list);
  return {
    unmount() {
      alive = false;
      list.remove();
    },
  };
}
