/* Список диалогов «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 23).
 *
 * chatList(list, { row, failed, loaded }) → { search, reload(), seen(chat | null), unmount() }:
 * - reload() — GET /api/chats; сначала группа «Закреплённые» (`pinned`), затем остальные по `updated`.
 *   Строку строит row(item) (chat.js, listItem); модуль дописывает звезду button.chat-pin[aria-pressed]
 *   («Закрепить» / «Открепить» → PATCH {pinned}) и метки span.chat-mark[data-mark=busy|unread|draft] без текста —
 *   слова в aria-label и title; «идёт ответ» важнее «не прочитано».
 *   Перерисовка — только когда ответ сервера изменился. Опрос — раз в
 *   LIST_POLL_MS при активных диалогах и реже без них, пока страница видна. Удачный ответ — loaded(items); итог — items или null.
 *   Отказ при нарисованном списке (нет связи, выкладка) список не стирает — повтор через LIST_POLL_MS; без списка —
 *   слова отказа на его месте.
 * - search — input#chat-search: отбор по названиям на странице без учёта регистра, без запросов.
 * - seen(chat, waiting) — страница видна и `updated` открытого диалога ещё не отмечен: POST …/seen (отказ молча), метка
 *   «не прочитано» строки снимается сразу и не возвращается, пока `updated` прежний. waiting — в диалоге ждущая
 *   карточка: пока её видно, POST …/seen повторяется раз в SEEN_REPEAT_MS — напоминание (chat_pending.remind)
 *   отсчитывает интервал от взгляда и не приходит ни одному устройству того, кто смотрит на карточку.
 * - черновик — draftStore(CHAT_DRAFTS).has(id); событие `studio-dialog-draft` перерисовывает только метки строки. */
import { tr } from './i18n.js';
import { api } from './api.js';
import { iconButton } from './icons.js';
import { paintPin } from './chat-title.js';
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';
import { el, words } from './ui.js';

export const LIST_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;
const SEEN_REPEAT_MS = 60000;     // как chat_pending.REMIND_TICK_SEC: проход напоминаний — раз в минуту
const MARKS = { busy: tr("идёт ответ"), asking: tr("ждёт ответа"), unread: tr("не прочитано"), draft: tr("есть черновик") };

export function chatList(list, { row, pinItem, project = () => null, failed = () => {}, loaded = () => {} }) {
  const drafts = draftStore(CHAT_DRAFTS);
  const search = el('input', { type: 'search', id: 'chat-search', class: 'input code-chat-search', autocomplete: 'off',
    placeholder: tr("Поиск по названиям"), 'aria-label': tr("Поиск по названиям") });
  let items = [];
  let answer = null;          // JSON последнего ответа сервера: тот же — без перерисовки
  let reading = 0;            // номер последнего запроса: поздний ответ прежнего не перебьёт новый
  let timer = null;
  let alive = true;
  const looked = new Map();   // id → `updated`, отмеченный POST …/seen
  const rows = new Map();     // id → { item, box } нарисованной строки
  let watching = null;        // { id, timer } открытого диалога с ждущей карточкой

  function watch(id) {
    if (watching?.id === id) return;
    if (watching) clearInterval(watching.timer);
    watching = id === null ? null : { id, timer: setInterval(() => {
      if (document.visibilityState === 'visible') api('POST', `/api/chats/${id}/seen`).catch(() => {});
    }, SEEN_REPEAT_MS) };
  }

  function unread(item) {
    return item.unread && !(looked.get(item.id) >= item.updated);
  }

  function activity(item) {
    return item.asking ? 'asking' : item.busy ? 'running' : unread(item) ? 'unread' : null;
  }

  function paintMarks(id) {
    const shown = rows.get(id);
    if (!shown) return;
    const { item, box } = shown;
    const state = activity(item);
    const kinds = [state === 'running' ? 'busy' : state, drafts.has(id) ? 'draft' : null].filter(Boolean);
    box.replaceChildren(...kinds.map((kind) => el('span', { class: 'chat-mark', 'data-mark': kind, role: 'img',
      'aria-label': MARKS[kind], title: MARKS[kind] })));
  }

  function decorate(item) {
    const li = row(item);
    if (item.readonly) return li;
    const box = el('span', { class: 'chat-marks' });
    const pin = iconButton('star', tr("Закрепить"), { class: 'icon-btn chat-pin' });
    paintPin(pin, item.pinned);
    pin.addEventListener('click', async () => {
      pin.disabled = true;
      try {
        await pinItem(item);
      } catch (err) {
        failed(err);
      } finally {
        pin.disabled = false;
      }
    });
    li.firstElementChild.append(box);
    li.append(pin);
    rows.set(item.id, { item, box });
    paintMarks(item.id);
    return li;
  }

  function paint() {
    rows.clear();
    const pid = project();
    const scoped = pid ? items.filter((item) => item.project_id === pid) : [];
    if (!scoped.length) {
      list.replaceChildren(el('li', { class: 'muted chat-list-note' }, tr("Диалогов пока нет")));
      return;
    }
    const query = search.value.trim().toLocaleLowerCase();
    const found = query ? scoped.filter((item) => (item.title || tr("Новый диалог")).toLocaleLowerCase().includes(query)) : scoped;
    if (!found.length) {
      list.replaceChildren(el('li', { class: 'muted chat-list-note' }, tr("Ничего не найдено")));
      return;
    }
    const pinned = found.filter((i) => i.pinned);
    list.replaceChildren(...(pinned.length ? [el('li', { class: 'chat-group' }, tr("Закреплённые"))] : []),
      ...pinned.map(decorate), ...found.filter((i) => !i.pinned).map(decorate));
  }

  function schedule(retry = false) {
    clearTimeout(timer);
    timer = null;
    if (alive && document.visibilityState === 'visible' && !document.documentElement.classList.contains('shrouded')) {
      const delay = retry || items.some((item) => item.busy || item.asking) ? LIST_POLL_MS : IDLE_POLL_MS;
      timer = setTimeout(() => reload(true), delay);
    }
  }

  async function reload(passive = false) {
    const mine = ++reading;
    let got;
    try {
      got = (await api('GET', '/api/chats', undefined, { unlock: !passive })).items;
    } catch (err) {
      if (!alive || mine !== reading) return null;
      if (answer !== null) {                         // нарисованный список остаётся — повтор
        schedule(true);
        return null;
      }
      rows.clear();
      list.replaceChildren(el('li', { class: 'error small chat-list-note' }, words(err)));
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
    clearTimeout(timer);
    timer = null;
    if (document.visibilityState === 'visible' && !document.documentElement.classList.contains('shrouded')) reload(true);
  };
  document.addEventListener('studio-dialog-draft', onDraft);
  document.addEventListener('visibilitychange', onVisible);
  search.addEventListener('input', paint);

  return {
    search,
    reload,
    paint,
    activity,
    seen(chat, waiting = false) {
      watch(chat && waiting ? chat.id : null);
      if (!chat || document.visibilityState !== 'visible' || looked.get(chat.id) === chat.updated) return;
      looked.set(chat.id, chat.updated);
      api('POST', `/api/chats/${chat.id}/seen`).catch(() => {});
      paintMarks(chat.id);
    },
    unmount() {
      alive = false;
      clearTimeout(timer);
      watch(null);
      document.removeEventListener('studio-dialog-draft', onDraft);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}
