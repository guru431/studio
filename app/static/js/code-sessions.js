import { tr } from './i18n.js';
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
import { api, me } from './api.js';
import { dateTime } from './time.js';
import { ENGINE_NAMES, engineOrder } from './code-options.js';
import { el } from './ui.js';
import { worktreeView } from './code-worktrees.js';

const UNTITLED = tr("Новый чат");

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function sessionList(host, { aid, pid, items, refresh = null, exclude = new Set(), picked, created, failed, codex = false, opencode = false, starts = true, contentSearch = false, worktrees = false, worktreeOpened }) {
  const base = `/api/code/agents/${aid}/projects/${pid}/chats`;
  let alive = true;
  let found = Array.isArray(items) ? items : null;
  let loadError = false;
  let remote = null, timer = null, request = null, generation = 0;

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

  function row(chat) {
    const closed = chat.state === 'closed';
    const marks = [el('span', { class: 'tag code-origin' }, chat.origin === 'studio' ? tr("студия") : tr("VS Code или терминал"))];
    marks.unshift(el('span', { class: 'tag code-engine' },
      ENGINE_NAMES[chat.engine || 'claude']));
    if (closed) marks.push(el('span', { class: 'tag code-closed' }, tr("закрыт")));
    if (chat.asking) marks.push(el('span', { class: 'tag code-asking' }, tr("ждёт ответа")));
    else if (chat.running) marks.push(el('span', { class: 'tag code-running' }, tr("идёт ход")));
    const link = el('button', { type: 'button', class: 'code-chat-link' },
      el('span', { class: 'code-chat-title', title: chat.title || UNTITLED }, chat.title || UNTITLED),
      typeof chat.modified === 'number' && chat.modified > 0 ? el('span', { class: 'muted small code-chat-time' }, when(chat.modified)) : null,
      el('span', { class: 'code-marks' }, ...marks));
    link.addEventListener('click', () => busy(link, async () => {
      if (closed) await api('PATCH', `${base}/${chat.chat}${chat.engine && chat.engine !== 'claude' ? `?engine=${chat.engine}` : ''}`, { state: 'open' });
      if (alive) picked(chat.chat, chat);
    }));
    return el('li', { 'data-chat': chat.chat, class: closed ? 'closed' : null }, link);
  }

  const list = el('ul', { id: 'code-chats', class: 'code-chats' });
  const search = el('input', { id: 'code-chat-search', class: 'input code-chat-search', type: 'search',
    placeholder: tr("Поиск по названиям чатов"), 'aria-label': tr("Поиск чатов"), autocomplete: 'off', maxlength: '200' });
  const scopeLabel = el('span', {}, tr("По заголовкам"));
  const scope = el('button', { type: 'button', id: 'code-search-content', class: 'btn btn-sm code-search-scope',
    role: 'switch', 'aria-checked': 'false', 'aria-label': tr("Поиск по содержимому"), hidden: !contentSearch },
    el('span', { class: 'code-search-switch', 'aria-hidden': 'true' }), scopeLabel);
  const searchRow = el('div', { class: 'code-session-search' }, search, scope);
  const note = el('p', { class: 'small muted', role: 'status', id: 'code-search-status' });
  const normalize = (value) => String(value).toLocaleLowerCase('ru-RU').replace(/ё/g, 'е');
  const startersRow = starts ? el('div', { class: 'code-chat-starters', role: 'group', 'aria-label': tr("Новый чат") }) : null;
  if (starts) {
    const starters = engineOrder(me?.code_defaults).filter((engine) => engine === 'claude'
      || (engine === 'codex' ? codex : opencode)).map((engine) => {
      const id = engine === 'claude' ? 'code-new-chat' : `code-new-${engine}`;
      const label = tr("Новый чат {0}", ENGINE_NAMES[engine]);
      const add = el('button', { type: 'button', id, class: 'code-chat-link code-new-chat', title: label,
        'aria-label': label }, engine === 'claude' ? 'Claude' : ENGINE_NAMES[engine]);
      add.addEventListener('click', () => busy(add, async () => {
        const { chat } = await api('POST', engine === 'claude' ? base : `${base}?engine=${engine}`);
        if (alive) created(chat, engine);
      }));
      return add;
    });
    startersRow.append(...starters);
  }
  function paint() {
    const terms = normalize(search.value).trim().split(/\s+/).filter(Boolean);
    const content = scope.getAttribute('aria-checked') === 'true' && search.value.trim();
    const matches = content ? remote || [] : (found || []).filter((c) => !exclude.has(c.chat)
      && terms.every((term) => normalize(c.title || UNTITLED).includes(term)));
    const empty = loadError ? tr("Не удалось загрузить чаты") : found === null ? tr("Загружаю…")
      : terms.length ? tr("Ничего не найдено") : tr("Прошлых чатов пока нет");
    list.replaceChildren(...(matches.length ? matches.map((chat) => {
      const li = row({ ...(found || []).find((item) => item.chat === chat.chat), ...chat });
      for (const snippet of chat.snippets || []) li.querySelector('button').append(
        el('span', { class: 'code-search-snippet' }, `${snippet.role === 'user' ? tr("Вы") : tr("Ответ")}: ${snippet.text}`));
      return li;
    })
      : [el('li', { class: 'code-chats-wait muted', role: 'status' }, empty)]));
  }
  function query() {
    clearTimeout(timer);
    request?.abort();
    const mine = ++generation;
    note.textContent = '';
    remote = null;
    paint();
    if (scope.getAttribute('aria-checked') !== 'true' || !search.value.trim()) return;
    note.textContent = tr("Ищу на компьютере…");
    timer = setTimeout(async () => {
      const own = new AbortController();
      request = own;
      try {
        const data = await api('POST', `/api/code/agents/${aid}/projects/${pid}/search`,
          { query: search.value.trim() }, { signal: own.signal });
        if (!alive || own.signal.aborted || mine !== generation) return;
        remote = data.items || [];
        note.textContent = data.complete === false ? tr("Поиск ограничен: проверена часть истории или не все помощники доступны") : '';
        paint();
      } catch (err) {
        if (!alive || own.signal.aborted || mine !== generation) return;
        note.textContent = tr("Не удалось выполнить поиск");
        failed(err);
      }
    }, 350);
  }
  search.addEventListener('input', query);
  scope.addEventListener('click', () => {
    const active = scope.getAttribute('aria-checked') !== 'true';
    scope.setAttribute('aria-checked', String(active));
    scopeLabel.textContent = active ? tr("По содержимому") : tr("По заголовкам");
    search.placeholder = active ? tr("Слова из переписки на компьютере") : tr("Поиск по названиям чатов");
    query();
  });
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
  if (startersRow) host.append(startersRow);
  host.append(searchRow, note, list);
  const copies = worktrees ? worktreeView(host, { aid, pid, opened: worktreeOpened }) : null;
  return {
    unmount() {
      alive = false;
      clearTimeout(timer);
      request?.abort();
      startersRow?.remove();
      searchRow.remove();
      note.remove();
      copies?.unmount();
      list.remove();
    },
  };
}
