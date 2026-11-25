/* Список диалогов «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 23).
 *
 * chatList(list, { row, group, failed, loaded }) → { search, reload(), items(), seen(chat | null), unmount() }:
 * - reload() — GET /api/chats; сначала группа «Закреплённые» (`pinned`, порядок сервера — по `updated`), затем дни
 *   group(updated). Строку строит row(item) (chat.js, listItem); модуль дописывает звезду button.chat-pin[aria-pressed]
 *   («Закрепить» / «Открепить» → PATCH {pinned}) и метки span.chat-mark[data-mark=busy|unread|draft] без текста —
 *   слова в aria-label и title, textContent строки остаётся названием; «идёт ответ» важнее «не прочитано».
 *   Перерисовка — только когда ответ сервера изменился: опрос не сбивает фокус и открытое меню строки. Опрос — раз в
 *   LIST_POLL_MS, пока у диалога идёт ответ и страница видна. Удачный ответ — loaded(items); итог — items или null (отказ).
 * - search — input#chat-search: отбор по названиям на странице без учёта регистра, без запросов.
 * - seen(chat) — страница видна и `updated` открытого диалога ещё не отмечен: POST …/seen (отказ молча), метка
 *   «не прочитано» строки снимается сразу и не возвращается, пока `updated` прежний.
 * - черновик — draftStore(CHAT_DRAFTS).has(id); событие `studio-dialog-draft` перерисовывает только метки строки. */
import { tr } from './i18n.js';
import { api } from './api.js';
import { iconButton } from './icons.js';
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';
import { el, words } from './ui.js';

export const LIST_POLL_MS = 5000;
const MARKS = { busy: tr("идёт ответ"), unread: tr("не прочитано"), draft: tr("есть черновик") };

export function chatList(list, { row, group, failed = () => {}, loaded = () => {} }) {
  const drafts = draftStore(CHAT_DRAFTS);
  const search = el('input', { type: 'search', id: 'chat-search', class: 'input chat-search', autocomplete: 'off',
    placeholder: tr("Поиск по названиям"), 'aria-label': tr("Поиск по названиям") });
  let items = [];
  let answer = null;          // JSON последнего ответа сервера: тот же — без перерисовки
  let reading = 0;            // номер последнего запроса: поздний ответ прежнего не перебьёт новый
  let timer = null;
  let alive = true;
  const looked = new Map();   // id → `updated`, отмеченный POST …/seen
  const rows = new Map();     // id → { item, box } нарисованной строки

  function unread(item) {
    return item.unread && !(looked.get(item.id) >= item.updated);
  }

  function paintMarks(id) {
    const shown = rows.get(id);
    if (!shown) return;
    const { item, box } = shown;
    const kinds = [item.busy ? 'busy' : unread(item) ? 'unread' : null, drafts.has(id) ? 'draft' : null].filter(Boolean);
    box.replaceChildren(...kinds.map((kind) => el('span', { class: 'chat-mark', 'data-mark': kind, role: 'img',
      'aria-label': MARKS[kind], title: MARKS[kind] })));
  }

  function decorate(item) {
    const li = row(item);
    const box = el('span', { class: 'chat-marks' });
    const label = item.pinned ? tr("Открепить") : tr("Закрепить");
    const pin = iconButton('star', label, { class: 'icon-btn chat-pin', 'aria-pressed': String(!!item.pinned) });
    pin.addEventListener('click', async () => {
      pin.disabled = true;
      try {
        await api('PATCH', `/api/chats/${item.id}`, { pinned: !item.pinned });
      } catch (err) {
        pin.disabled = false;
        failed(err);
        return;
      }
      reload();
    });
    const more = li.querySelector('.chat-more');
    li.insertBefore(box, more);
    li.insertBefore(pin, more);
    rows.set(item.id, { item, box });
    paintMarks(item.id);
    return li;
  }

  function paint() {
    rows.clear();
    if (!items.length) {
      list.replaceChildren(el('p', { class: 'muted small chat-list-note' }, tr("Диалогов пока нет")));
      return;
    }
    const query = search.value.trim().toLocaleLowerCase();
    const found = query ? items.filter((item) => (item.title || tr("Новый диалог")).toLocaleLowerCase().includes(query)) : items;
    if (!found.length) {
      list.replaceChildren(el('p', { class: 'muted small chat-list-note' }, tr("Ничего не найдено")));
      return;
    }
    const nodes = [];
    let name = null;
    for (const item of [...found.filter((i) => i.pinned), ...found.filter((i) => !i.pinned)]) {
      const next = item.pinned ? tr("Закреплённые") : group(item.updated);
      if (next !== name) {
        name = next;
        nodes.push(el('p', { class: 'chat-group' }, name));
      }
      nodes.push(decorate(item));
    }
    list.replaceChildren(...nodes);
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (alive && document.visibilityState === 'visible' && items.some((item) => item.busy)) {
      timer = setTimeout(reload, LIST_POLL_MS);
    }
  }

  async function reload() {
    const mine = ++reading;
    let got;
    try {
      got = (await api('GET', '/api/chats')).items;
    } catch (err) {
      if (!alive || mine !== reading) return null;
      answer = null;
      rows.clear();
      list.replaceChildren(el('p', { class: 'error small chat-list-note' }, words(err)));
      schedule();
      return null;
    }
    if (!alive || mine !== reading) return got;
    const text = JSON.stringify(got);
    if (text !== answer) {
      answer = text;
      items = got;
      paint();
    }
    loaded(items);
    schedule();
    return items;
  }

  const onDraft = (event) => {
    if (event.detail?.store === CHAT_DRAFTS) paintMarks(event.detail.id);
  };
  const onVisible = () => {
    if (document.visibilityState === 'visible' && !timer && items.some((item) => item.busy)) reload();
  };
  document.addEventListener('studio-dialog-draft', onDraft);
  document.addEventListener('visibilitychange', onVisible);
  search.addEventListener('input', paint);

  return {
    search,
    reload,
    items: () => items,
    seen(chat) {
      if (!chat || document.visibilityState !== 'visible' || looked.get(chat.id) === chat.updated) return;
      looked.set(chat.id, chat.updated);
      api('POST', `/api/chats/${chat.id}/seen`).catch(() => {});
      paintMarks(chat.id);
    },
    unmount() {
      alive = false;
      clearTimeout(timer);
      document.removeEventListener('studio-dialog-draft', onDraft);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}
