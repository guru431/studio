/* Раздел «Чат» (#/chat — новый диалог, #/chat/<id> — диалог): список диалогов, чип модели,
 * лента сообщений с markdown, поле ввода с вложениями.
 *
 * Ход: вложения — POST /api/attachments по одному, затем POST /api/chats/<id>/turns. Подключение
 * через сервер отвечает потоком SSE (turn, delta, done, error); прямое — JSON с адресом и
 * сообщениями: браузер зовёт модель сам с токеном из /api/connections/<id>/token (токен — только
 * в памяти вкладки) и кладёт итог PUT .../messages/<id>. «Остановить»: через сервер — POST .../stop,
 * прямое — обрыв запроса и PUT со статусом stopped.
 *
 * Ответы, которые читает эта вкладка (`jobs`), живут дольше экрана: ушли в другой диалог или
 * раздел — ответ дочитывается и сохраняется. Пока вкладка читает ответ из браузера, она раз в
 * BEAT_MS шлёт признак жизни (POST .../alive): сервер отдаёт такой ответ с `live` — другая вкладка
 * показывает его идущим и опрашивает диалог, как ответ сервера, который идёт без нас. Ответ
 * `streaming` из браузера без признака жизни (вкладку закрыли) — «прервано».
 * Неотправленное вложение крестик удаляет и с сервера (DELETE /api/attachments/<id>).
 * Текст сервера и модели — только textContent и renderMarkdown (DOMPurify). */
import { api, me } from './api.js';
import { renderMarkdown } from './markdown.js';
import { CORS, modelsOf } from './settings.js';
import { readSSE } from './sse.js';
import { button, el, field, openDialog, words } from './ui.js';

const ACCEPT = '.pdf,.docx,.txt,.md,image/*';
const RENDER_MS = 100;           // markdown идущего ответа — не чаще
const POLL_MS = 2000;
const BEAT_MS = 15000;           // признак жизни ответа из браузера; сервер ждёт его 90 с (ALIVE_SEC)
const STICK_PX = 48;             // ближе к низу — автопрокрутка не отпускает ленту
const COPIED_MS = 1500;
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  clip: '<path d="M20 11.5l-8.2 8.2a5 5 0 01-7.1-7.1l8.6-8.6a3.4 3.4 0 014.8 4.8l-8.4 8.4a1.8 1.8 0 01-2.6-2.6l7.6-7.6"/>',
  send: '<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  doc: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
};

const jobs = new Map();          // id диалога → ответ, который читает эта вкладка
const tokens = new Map();        // id подключения → { token, url } прямого подключения (память вкладки)
let screen = null;               // смонтированный раздел

function icon(name, cls = 'ico') {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
  return box.firstChild;
}

function iconButton(name, label, attrs = {}) {
  return el('button', { type: 'button', class: 'icon-btn', 'aria-label': label, title: label, ...attrs }, icon(name));
}

function elapsed(since) {
  const s = Math.max(0, Math.floor((Date.now() - since) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function groupOf(updated) {
  const day = new Date();
  day.setHours(0, 0, 0, 0);
  const today = day.getTime();
  day.setDate(day.getDate() - 1);
  const t = updated * 1000;
  return t >= today ? 'Сегодня' : t >= day.getTime() ? 'Вчера' : 'Ранее';
}

function notify(job, final) {
  if (screen) screen.refresh(job, final);
}

function finish(job) {
  if (jobs.get(job.chatId) === job) jobs.delete(job.chatId);
  notify(job, true);
}

/* ---- чтение ответа ------------------------------------------------------------ */

function append(job, text) {
  job.first = true;
  job.message.content += text;
  notify(job, false);
}

function adoptIds(job, data) {
  if (job.user) job.user.id = data.user_message_id;
  job.message.id = data.assistant_message_id;
}

async function readServer(job, response) {
  job.message.via = 'server';
  let cut = true;
  try {
    for await (const { event, data } of readSSE(response)) {
      if (event === 'turn') adoptIds(job, data);
      else if (event === 'delta' && data && typeof data.text === 'string') append(job, data.text);
      else if (event === 'done' || event === 'error') {
        job.message.status = event === 'done' ? data.status : 'error';
        if (event === 'error') job.message.error = data.message;
        cut = false;
        break;
      }
    }
  } catch {
    // связь оборвалась — ответ на сервере идёт дальше, диалог перечитается
  }
  if (cut) job.lost = true;
  finish(job);
}

/* Токен — только тому адресу, для которого сервер его выдал: подключение сменило адрес (здесь,
 * в другой вкладке или у администратора) — прежний токен новому адресу не уходит. */
async function tokenFor(connId, url) {
  const known = tokens.get(connId);
  if (known && known.url === url) return known.token;
  tokens.delete(connId);
  const fresh = await api('GET', `/api/connections/${connId}/token`);
  if (fresh.chat_url !== url) throw new Error('Адрес подключения сменился — отправьте сообщение ещё раз');
  tokens.set(connId, { token: fresh.token, url });
  return fresh.token;
}

async function serviceRefusal(response) {
  let detail = '';
  try {
    const error = (await response.json()).error;
    detail = String((error && error.message) || error || '').slice(0, 300);
  } catch {
    // не JSON — без подробностей
  }
  return `Сервис ответил ошибкой (${response.status})${detail ? `: ${detail}` : ''}`;
}

async function readBrowser(job, turn) {
  job.message.via = 'browser';
  adoptIds(job, turn);
  // признак жизни; ответ уже не идёт (диалог удалили, историю стёрли) — читать его дальше незачем
  const beat = setInterval(() => {
    api('POST', `/api/chats/${job.chatId}/messages/${job.message.id}/alive`).catch((err) => {
      if (err.code === 'gone' || err.code === 'state') job.controller.abort();
    });
  }, BEAT_MS);
  let status = 'done';
  try {
    const token = await tokenFor(job.connId, turn.url);
    let response;
    try {
      response = await fetch(turn.url, { method: 'POST', signal: job.controller.signal,
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: turn.model, messages: turn.messages, stream: true }),
        credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
    } catch (err) {
      throw err instanceof TypeError ? new Error(CORS) : err;
    }
    if (response.status === 401 || response.status === 403) tokens.delete(job.connId);
    if (!response.ok) throw new Error(await serviceRefusal(response));
    try {
      for await (const { data } of readSSE(response)) {
        if (data === '[DONE]') break;
        if (!data || typeof data !== 'object') continue;
        if (data.error) throw new Error(`Модель прервала ответ: ${String(data.error.message || data.error).slice(0, 300)}`);
        for (const choice of data.choices || []) {
          const text = choice && choice.delta && choice.delta.content;
          if (typeof text === 'string' && text) append(job, text);
        }
      }
    } catch (err) {
      throw err instanceof TypeError ? new Error('Связь с сервисом оборвалась') : err;
    }
  } catch (err) {
    if (err && err.name === 'AbortError') status = 'stopped';
    else {
      status = 'error';
      job.message.error = words(err);
    }
  }
  clearInterval(beat);
  job.message.status = status;
  try {
    await api('PUT', `/api/chats/${job.chatId}/messages/${job.message.id}`, { content: job.message.content, status });
  } catch (err) {
    if (err.code !== 'state' && err.code !== 'gone') job.message.error = `Ответ не сохранился: ${words(err)}`;
  }
  finish(job);
}

/* ---- экран -------------------------------------------------------------------- */

function build(view, ctx) {
  let chatId;                  // показанный диалог; null — новый, ещё не созданный
  let chat = null;             // { chat, messages } с сервера
  let conns = [];              // подключения с разделом «чат»
  let draft = null;            // { connection_id, model } нового диалога
  let atts = [];               // вложения поля ввода: { file, meta, error, busy }
  let sending = false;
  let stick = true;
  let opened = 0;              // медленный ответ прежнего диалога не перебьёт новый
  let poll = null;
  let paintTimer = null;
  let lastPaint = 0;
  let menuFor = null;          // пункт списка с открытым меню
  const parts = new WeakMap(); // сообщение → его узлы в ленте
  const models = new Map();    // id подключения → Promise имён моделей

  /* разметка */
  const list = el('nav', { class: 'chat-list', 'aria-label': 'Диалоги' });
  const fresh = el('button', { type: 'button', class: 'btn chat-new', id: 'chat-new' }, icon('plus'), 'Новый чат');
  const aside = el('aside', { id: 'chats', class: 'chats' }, fresh, list);
  const backdrop = el('div', { class: 'chats-backdrop', hidden: true });
  const toggle = iconButton('list', 'Диалоги', { id: 'chats-toggle', class: 'icon-btn chats-toggle', 'aria-controls': 'chats',
    'aria-expanded': 'false' });
  const chipName = el('span', { class: 'chip-name' });
  const chip = el('button', { type: 'button', id: 'model-chip', class: 'model-chip', 'aria-haspopup': 'true',
    'aria-expanded': 'false' }, el('span', { class: 'dot', 'aria-hidden': 'true' }), chipName, icon('down', 'ico caret'));
  const modelMenu = el('div', { id: 'model-menu', class: 'model-menu', hidden: true });
  const connName = el('span', { class: 'chat-conn' });
  const head = el('header', { class: 'head chat-head' }, toggle,
    el('div', { class: 'model-wrap' }, chip, modelMenu), connName);
  const feed = el('div', { class: 'feed' });
  const scroller = el('div', { id: 'messages', class: 'messages' }, feed);
  const notice = el('p', { class: 'chat-error error', id: 'chat-error', 'aria-live': 'polite' });
  const chipsBox = el('div', { class: 'att-chips' });
  const input = el('textarea', { id: 'chat-input', rows: '1', placeholder: 'Напишите сообщение…', 'aria-label': 'Сообщение',
    maxlength: '200000' });
  const file = el('input', { type: 'file', id: 'attach', class: 'sr', accept: ACCEPT, multiple: true });
  const clip = el('label', { class: 'icon-btn attach', title: 'Прикрепить файл' }, icon('clip'),
    el('span', { class: 'sr' }, 'Прикрепить файл'), file);
  const send = el('button', { type: 'submit', id: 'send', class: 'send', 'aria-label': 'Отправить', title: 'Отправить' },
    icon('send'));
  const stop = el('button', { type: 'button', id: 'stop', class: 'send', 'aria-label': 'Остановить', title: 'Остановить',
    hidden: true }, icon('stop'));
  const form = el('form', { class: 'composer', id: 'composer' }, chipsBox, input,
    el('div', { class: 'row' }, clip, el('span', { class: 'grow' }), send, stop));
  const main = el('section', { class: 'chat-main' }, head, scroller, notice, form);
  const root = el('div', { class: 'chat' }, aside, backdrop, main);
  view.append(root);

  /* выдвижная панель списка (≤ 760px) */
  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  /* список диалогов */
  async function loadList() {
    let items;
    try {
      items = (await api('GET', '/api/chats')).items;
    } catch (err) {
      list.replaceChildren(el('p', { class: 'error small chat-list-note' }, words(err)));
      return;
    }
    if (!items.length) {
      list.replaceChildren(el('p', { class: 'muted small chat-list-note' }, 'Диалогов пока нет'));
      return;
    }
    const nodes = [];
    let group = null;
    for (const item of items) {
      const name = groupOf(item.updated);
      if (name !== group) {
        group = name;
        nodes.push(el('p', { class: 'chat-group' }, name));
      }
      nodes.push(listItem(item));
    }
    list.replaceChildren(...nodes);
  }

  function listItem(item) {
    const link = el('a', { href: `#/chat/${item.id}`, class: 'chat-link', 'aria-current': item.id === chatId ? 'page' : null },
      item.title || 'Новый диалог');
    const more = iconButton('more', 'Действия с диалогом', { class: 'icon-btn chat-more', 'aria-haspopup': 'true' });
    const li = el('div', { class: 'chat-item', 'data-id': item.id }, link, more);
    link.addEventListener('click', () => panel(false));
    more.addEventListener('click', (event) => {
      event.stopPropagation();
      const was = menuFor === li;
      closeItemMenu();
      if (!was) openItemMenu(li, item);
    });
    return li;
  }

  function openItemMenu(li, item) {
    const rename = el('button', { type: 'button', class: 'menu-item chat-rename' }, 'Переименовать');
    const remove = el('button', { type: 'button', class: 'menu-item chat-delete btn-danger' }, 'Удалить');
    rename.addEventListener('click', () => { closeItemMenu(); renameDialog(item); });
    remove.addEventListener('click', () => { closeItemMenu(); deleteDialog(item); });
    li.append(el('div', { class: 'chat-menu', role: 'menu' }, rename, remove));
    menuFor = li;
  }

  function closeItemMenu() {
    if (menuFor) menuFor.querySelector('.chat-menu')?.remove();
    menuFor = null;
  }

  function renameDialog(item) {
    const title = el('input', { class: 'input', name: 'title', maxlength: '200', value: item.title || '' });
    const fail = el('p', { class: 'error' });
    const save = button('Сохранить', { class: 'btn btn-accent', id: 'rename-save' });
    const dialog = openDialog(root, { id: 'rename-dialog', title: 'Переименовать диалог',
      body: [field('Название', title, 'title'), fail], actions: [save] });
    const submit = async () => {
      save.disabled = true;
      try {
        const out = await api('PATCH', `/api/chats/${item.id}`, { title: title.value });
        if (chat && chat.chat.id === out.id) chat.chat = out;
        dialog.close();
        loadList();
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    };
    save.addEventListener('click', submit);
    title.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    });
    title.focus();
    title.select();
  }

  function deleteDialog(item) {
    const fail = el('p', { class: 'error' });
    const confirm = button('Удалить', { class: 'btn btn-danger solid', id: 'chat-delete-confirm' });
    const dialog = openDialog(root, { id: 'chat-delete-dialog', title: 'Удалить диалог?',
      body: [el('p', {}, `«${item.title || 'Новый диалог'}» удалится вместе с вложениями.`), fail], actions: [confirm] });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await api('DELETE', `/api/chats/${item.id}`);
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
        return;
      }
      const job = jobs.get(item.id);
      if (job) job.controller.abort();
      dialog.close();
      if (chatId === item.id) newChat();
      loadList();
    });
  }

  function newChat() {
    panel(false);
    if (location.hash === '#/chat') open(null);
    else ctx.navigate('chat');
  }

  /* чип модели */
  function defaultConnId() {
    return conns.some((c) => c.id === me.default_connection_id) ? me.default_connection_id : (conns[0] || {}).id;
  }

  function chosen() {
    if (chat) return { connection_id: chat.chat.connection_id, model: chat.chat.model };
    if (draft) return draft;
    return { connection_id: defaultConnId(), model: me.default_model || '' };
  }

  function paintChip() {
    const { connection_id: connId, model } = chosen();
    chipName.textContent = model || 'модель по умолчанию';
    connName.textContent = (conns.find((c) => c.id === connId) || {}).name || '';
  }

  function modelsFor(conn) {
    if (!models.has(conn.id)) {
      const ask = modelsOf(conn);
      models.set(conn.id, ask);
      ask.catch(() => models.delete(conn.id));         // отказ — в следующий раз спросить заново
    }
    return models.get(conn.id);
  }

  function openModels(open) {
    modelMenu.hidden = !open;
    chip.setAttribute('aria-expanded', String(open));
    if (!open) return;
    const now = chosen();
    if (!conns.length) {
      modelMenu.replaceChildren(el('p', { class: 'muted small' }, 'Нет подключений с чатом — добавьте в профиле'));
      return;
    }
    modelMenu.replaceChildren(...conns.map((conn) => {
      const note = el('p', { class: 'muted small model-note' }, 'Загружаю список моделей…');
      const group = el('div', { class: 'model-group', 'data-conn': String(conn.id) },
        el('p', { class: 'model-group-name' }, conn.name), note);
      modelsFor(conn).then((ids) => {
        note.remove();
        if (!ids.length) group.append(el('p', { class: 'muted small model-note' }, 'Сервис не назвал ни одной модели'));
        for (const id of ids) {
          const option = el('button', { type: 'button', class: 'model-option', 'data-conn': String(conn.id), 'data-model': id,
            'aria-current': conn.id === now.connection_id && id === now.model ? 'true' : null }, id);
          option.addEventListener('click', () => choose(conn.id, id));
          group.append(option);
        }
      }, (err) => {
        note.textContent = words(err);
        note.className = 'error small model-note';
      });
      return group;
    }));
  }

  async function choose(connId, model) {
    openModels(false);
    if (!chat) {
      draft = { connection_id: connId, model };
      paintChip();
      return;
    }
    try {
      chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, { connection_id: connId, model });
      notice.textContent = '';
    } catch (err) {
      notice.textContent = words(err);
    }
    paintChip();
  }

  /* лента */
  function liveOf(m) {
    const job = jobs.get(chatId);
    return job && m.id && job.message.id === m.id ? job.message : m;
  }

  function reading(m) {
    for (const job of jobs.values()) if (job.message === m) return job;
    return null;
  }

  function attLabel(a) {
    return el('span', { class: 'att' }, icon(a.kind === 'image' ? 'image' : 'doc'), el('b', {}, a.name),
      a.kind === 'document' ? el('span', { class: 'muted' }, `${Number(a.chars || 0).toLocaleString('ru-RU')} зн.`) : null);
  }

  function messageNode(m, last) {
    if (m.role === 'user') {
      const box = el('div', { class: 'msg user' });
      if (m.attachments && m.attachments.length) box.append(el('div', { class: 'atts' }, ...m.attachments.map(attLabel)));
      if (m.content) box.append(el('div', { class: 'bubble' }, m.content));
      return box;
    }
    const md = el('div', { class: 'md' });
    const meta = el('p', { class: 'msg-meta' });
    const actions = el('div', { class: 'msg-actions' });
    const node = el('div', { class: 'msg assistant' }, el('span', { class: 'mark', 'aria-hidden': 'true' }),
      el('div', { class: 'msg-body' }, md, meta, actions));
    parts.set(m, { md, meta, actions, last, shown: null });
    paint(m);
    return node;
  }

  function paint(m) {
    const p = parts.get(m);
    if (!p) return;
    const job = reading(m);
    if (p.shown !== m.content) {
      p.md.replaceChildren(renderMarkdown(m.content));
      p.shown = m.content;
    }
    let text = '';
    let kind = '';
    if (m.status === 'streaming') {
      if (job) text = job.first ? '' : `загружаю модель · ${elapsed(job.started)}`;
      else if (m.via === 'browser') text = m.live ? 'ответ идёт в другой вкладке…' : 'прервано';
      else text = 'ответ ещё идёт…';
    } else if (m.status === 'stopped') text = 'остановлено';
    else if (m.status === 'error') {
      text = m.error || (m.content ? 'ответ оборвался' : 'ответ не получен');
      kind = ' error';
    }
    p.meta.textContent = text;
    p.meta.className = `msg-meta${kind}`;
    p.actions.replaceChildren();
    if (m.status === 'streaming' && (job || m.via !== 'browser' || m.live)) return;   // «прервано» можно переспросить
    if (m.content) {
      const copy = button('Копировать', { class: 'btn btn-sm msg-copy' }, async () => {
        try {
          await navigator.clipboard.writeText(m.content);
          copy.textContent = 'Скопировано';
        } catch {
          copy.textContent = 'Не вышло';
        }
        setTimeout(() => { copy.textContent = 'Копировать'; }, COPIED_MS);
      });
      p.actions.append(copy);
    }
    if (p.last) p.actions.append(button('Ответить заново', { class: 'btn btn-sm msg-retry' }, () => submit(true)));
  }

  function renderFeed() {
    if (!chat) return;
    const msgs = chat.messages.map(liveOf);
    if (!msgs.length) {
      feed.replaceChildren(emptyNote());
      return;
    }
    feed.replaceChildren(...msgs.map((m, i) => messageNode(m, i === msgs.length - 1)));
  }

  function emptyNote() {
    return el('div', { class: 'empty chat-empty' }, el('span', { class: 'badge', 'aria-hidden': 'true' }, icon('chat')),
      el('p', {}, 'Напишите сообщение или прикрепите документ — PDF, DOCX, TXT, MD или фото'));
  }

  function scrollDown(force) {
    if (force) stick = true;
    if (stick) scroller.scrollTop = scroller.scrollHeight;
  }

  function remoteStream() {
    return !!chat && chat.messages.some((m) => m.role === 'assistant' && m.status === 'streaming' && m.via === 'server'
      && liveOf(m) === m);
  }

  /* ответ из браузера, который читает другая вкладка: остановить его отсюда нельзя, только дождаться */
  function otherTab() {
    return !!chat && chat.messages.some((m) => m.role === 'assistant' && m.status === 'streaming' && m.via === 'browser'
      && m.live && liveOf(m) === m);
  }

  function syncBusy() {
    const busy = jobs.has(chatId) || remoteStream();
    send.hidden = busy;
    stop.hidden = !busy;
    send.disabled = sending;
  }

  function schedulePoll() {
    clearTimeout(poll);
    poll = (remoteStream() || otherTab()) && !jobs.has(chatId) ? setTimeout(reload, POLL_MS) : null;
  }

  async function reload() {
    if (!chatId) return;
    const id = chatId;
    const mine = opened;
    try {
      const data = await api('GET', `/api/chats/${id}`);
      if (mine !== opened || chatId !== id) return;
      chat = data;
    } catch (err) {
      if (mine === opened) notice.textContent = words(err);
      return;
    }
    renderFeed();
    paintChip();
    syncBusy();
    schedulePoll();
    scrollDown(false);
  }

  async function open(id) {
    panel(false);
    openModels(false);
    if (id === chatId && (id === null || chat)) return;
    const mine = ++opened;
    chatId = id;
    chat = null;
    clearTimeout(poll);
    notice.textContent = '';
    let listed = false;
    for (const link of list.querySelectorAll('.chat-link')) {
      const here = link.closest('.chat-item').dataset.id === id;
      listed ||= here;
      if (here) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    if (id !== null && !listed) {                    // диалог новее списка (другая вкладка, ссылка)
      firstList.then(() => {                         // первый список раздела ещё мог не прийти
        if (chatId === id && ![...list.querySelectorAll('.chat-item')].some((n) => n.dataset.id === id)) loadList();
      });
    }
    syncBusy();
    paintChip();
    if (id === null) {
      feed.replaceChildren(emptyNote());
      input.focus();
      return;
    }
    feed.replaceChildren(el('p', { class: 'muted feed-note' }, 'Загружаю диалог…'));
    try {
      const data = await api('GET', `/api/chats/${encodeURIComponent(id)}`);
      if (mine !== opened) return;
      chat = data;
    } catch (err) {
      if (mine !== opened) return;
      feed.replaceChildren(el('p', { class: 'error feed-note' }, words(err)));
      return;
    }
    renderFeed();
    paintChip();
    syncBusy();
    schedulePoll();
    scrollDown(true);
  }

  /* поле ввода и вложения */
  function paintAtts() {
    chipsBox.replaceChildren(...atts.map((a) => {
      const remove = iconButton('x', `Убрать ${a.file.name}`, { class: 'icon-btn att-remove' });
      remove.addEventListener('click', () => {
        atts = atts.filter((x) => x !== a);
        paintAtts();
        // уже загружено, но не отправлено — и с сервера; отказ не важен: остаток уберёт уборка
        if (a.meta) api('DELETE', `/api/attachments/${a.meta.id}`).catch(() => {});
      });
      return el('span', { class: `att-chip${a.error ? ' bad' : ''}` },
        icon(a.file.type.startsWith('image/') ? 'image' : 'doc'), el('span', { class: 'att-name' }, a.file.name),
        a.busy ? el('span', { class: 'muted att-state' }, 'загружаю…') : null,
        a.error ? el('span', { class: 'error att-state' }, a.error) : null,
        sending ? null : remove);
    }));
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
  }

  async function upload(a) {
    if (a.meta) return true;
    a.busy = true;
    a.error = '';
    paintAtts();
    try {
      const body = new FormData();
      body.append('file', a.file, a.file.name);
      a.meta = await api('POST', '/api/attachments', body);
      return true;
    } catch (err) {
      a.error = words(err);
      return false;
    } finally {
      a.busy = false;
      paintAtts();
    }
  }

  async function submit(regenerate) {
    const text = input.value;
    if (sending || jobs.has(chatId) || (!regenerate && !text.trim() && !atts.length)) return;
    sending = true;
    notice.textContent = '';
    syncBusy();
    paintAtts();
    try {
      await turn(regenerate, text);
    } catch (err) {
      notice.textContent = words(err);
    } finally {
      sending = false;
      syncBusy();
      paintAtts();
    }
  }

  async function turn(regenerate, text) {
    if (!chatId) {
      const created = await api('POST', '/api/chats', draft || {});
      ++opened;
      chatId = created.id;
      chat = { chat: created, messages: [] };
      draft = null;
      history.replaceState(null, '', `#/chat/${created.id}`);
      loadList();
    }
    const id = chatId;
    const sent = regenerate ? [] : atts.slice();
    for (const a of sent) {
      if (!(await upload(a))) return;              // отказ — словами у чипа
    }
    if (chatId !== id) return;
    const before = chat.messages;
    const user = regenerate ? null : { id: null, role: 'user', content: text, status: 'done',
      attachments: sent.map((a) => a.meta) };
    const message = { id: null, role: 'assistant', content: '', status: 'streaming', via: null, attachments: [] };
    const job = { chatId: id, connId: chat.chat.connection_id, user, message, started: Date.now(), first: false,
      lost: false, controller: new AbortController() };
    const kept = regenerate && before.length && before[before.length - 1].role === 'assistant' ? before.slice(0, -1) : before;
    chat.messages = [...kept, ...(user ? [user] : []), message];
    jobs.set(id, job);
    renderFeed();
    syncBusy();
    scrollDown(true);
    let reply;
    try {
      reply = await api('POST', `/api/chats/${id}/turns`,
        regenerate ? { regenerate: true } : { text, attachment_ids: sent.map((a) => a.meta.id) });
    } catch (err) {
      jobs.delete(id);
      if (chatId === id) {
        chat.messages = before;
        renderFeed();
        if (err.status === 503) reload();          // ход записан, но фото не прочиталось — как на сервере
      }
      throw err;
    }
    if (chatId === id && !regenerate) {
      input.value = '';
      autosize();
      atts = atts.filter((a) => !sent.includes(a));
    }
    loadList();
    if (reply instanceof Response) readServer(job, reply);
    else readBrowser(job, reply);
  }

  /* ответ, который читает вкладка: перерисовка не чаще RENDER_MS */
  function refresh(job, final) {
    if (job.chatId !== chatId || !chat) {
      if (final) loadList();
      return;
    }
    if (!parts.has(job.message)) renderFeed();
    if (!final) {
      if (paintTimer) return;
      paintTimer = setTimeout(() => {
        paintTimer = null;
        lastPaint = Date.now();
        paint(job.message);
        scrollDown(false);
      }, Math.max(0, RENDER_MS - (Date.now() - lastPaint)));
      return;
    }
    clearTimeout(paintTimer);
    paintTimer = null;
    renderFeed();                                    // «Ответить заново» — у последнего
    syncBusy();
    scrollDown(false);
    loadList();
    if (job.lost) reload();
  }

  /* события */
  const onDocClick = (event) => {
    if (!modelMenu.hidden && !modelMenu.contains(event.target) && !chip.contains(event.target)) openModels(false);
    if (menuFor && !menuFor.contains(event.target)) closeItemMenu();
  };
  const onKey = (event) => {
    if (event.key !== 'Escape') return;
    if (!modelMenu.hidden) {
      openModels(false);
      chip.focus();
    }
    closeItemMenu();
    panel(false);
  };
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKey);
  const tick = setInterval(() => {
    const job = jobs.get(chatId);
    if (job && !job.first) paint(job.message);
  }, 1000);

  fresh.addEventListener('click', newChat);
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  chip.addEventListener('click', () => openModels(modelMenu.hidden));
  scroller.addEventListener('scroll', () => {
    stick = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - STICK_PX;
  }, { passive: true });
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit(false);
  });
  file.addEventListener('change', () => {
    for (const f of file.files) atts.push({ file: f, meta: null, error: '', busy: false });
    file.value = '';
    paintAtts();
  });
  stop.addEventListener('click', async () => {
    const job = jobs.get(chatId);
    stop.disabled = true;
    try {
      if (job) job.controller.abort();             // прямое подключение: обрыв запроса и PUT stopped
      if (chatId && (!job || job.message.via !== 'browser')) {
        await api('POST', `/api/chats/${chatId}/stop`);
        if (!job) reload();
      }
    } catch (err) {
      notice.textContent = words(err);
    } finally {
      stop.disabled = false;
    }
  });

  api('GET', '/api/connections').then(({ items }) => {
    conns = items.filter((c) => c.sections.includes('chat'));
    paintChip();
  }, (err) => { notice.textContent = words(err); });
  const firstList = loadList();

  return {
    open,
    refresh,
    destroy() {
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
      clearInterval(tick);
      clearTimeout(poll);
      clearTimeout(paintTimer);
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
  screen.open(ctx.id);
}

export function update(ctx) {
  if (screen) screen.open(ctx.id);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
