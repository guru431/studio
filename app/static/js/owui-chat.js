import { tr } from './i18n.js';
/* Лента чата Open WebUI и поле ввода (спека 2026-11-22-studio-open-webui-design, «Страница», «Вид чата для страницы»,
 * «Пауза», «Поток на страницу»).
 *
 * chatView(host, { account, chatId, created }) → { account, chat(), event(type, data), retry(), unmount() }:
 * - лента — текущая ветка GET /api/owui/accounts/{id}/chats/{chat}: текст ответа — разметкой (renderMarkdownInto,
 *   DOMPurify), пока ответ идёт — простым текстом; рассуждение — свёрнутым блоком, как в «Чате»; инструменты —
 *   плашками, у плашки с результатом — раскрытие; картинки и файлы — GET …/files/{fid}; неизвестный элемент —
 *   строкой «элемент <тип>»; пауза — карточка owui-pending.js;
 * - поток — события SSE учётки (owui.js зовёт event): `delta` дописывает текст или рассуждение, `item` — плашку
 *   инструмента (куски ответа, которого лента ещё не знает, ждут его), `done` / `pending` / `active` — перечитать чат,
 *   `link up` — после `down`, `relogin` или обрыва потока (`lost` от owui.js): первое `up` чат не читает
 *   (пропущенные куски потока не восстанавливаются: итог — из сохранённого чата), `title` — название,
 *   `queued` — «в очереди»;
 * - поле ввода: модель — picker.js (GET …/models; у открытого чата — модель чата, у нового — `default`), вложения —
 *   POST …/files (до FILE_MAX, не больше FILES_MAX; файл, который Open WebUI не обработал, — с предупреждением;
 *   убранное до отправки вложение удаляется после окончания загрузки),
 *   «Отправить» (новый чат — POST …/chats, затем created(chat); открытый — POST …/messages с parent_id последнего
 *   сообщения), «Остановить» (POST …/stop) — пока в чате идёт ответ; ответ сервера `queued` — «в очереди».
 * Тексты пользователя, ответы модели, названия чатов и слова Open WebUI — только textContent/разметкой, без перевода. */
import { api } from './api.js';
import { shareMenu } from './chat-share.js';
import { enterSends, fieldBox } from './field.js';
import { icon, iconButton } from './icons.js';
import { renderMarkdownInto } from './markdown.js';
import { paintOwuiPending } from './owui-pending.js';
import { picker } from './picker.js';
import { signal } from './sound.js';
import { el, words } from './ui.js';

/** Предел вложения, байт (owui.FILE_MAX) и число вложений сообщения (owui.FILES_MAX). */
const FILE_MAX = 20 * 1024 * 1024;
const FILES_MAX = 10;
const RELOAD_MS = 250;
/** Файл Open WebUI по пути его сервера (как owui_view.FILE_URL): относительный адрес в тексте разметки. */
const OWUI_FILE = /(^|[\s("'<])\/api\/v1\/files\/([A-Za-z0-9_.:-]{1,128})\/content(?:\?[^\s)"'<>]*)?/g;

let localIds = 0;

export function chatView(host, { account, chatId = null, readOnly = false, created = () => {} }) {
  const base = `/api/owui/accounts/${account}`;
  let chat = chatId;
  let messages = [];
  let active = false;              // в чате идёт ответ (GET чата, события active/delta, своя отправка)
  let sending = false;
  let picked = false;              // модель выбрана человеком: перечитывание чата её не сбрасывает
  let queued = [];                 // сообщения, которые Open WebUI поставил в очередь: { text }
  let attachments = [];            // { name, kind, id?, request?, uploading, removing?, failed, error }
  let gone = false;
  let failed = false;              // чтение чата или моделей не удалось: retry() повторит
  let loaded = !chatId;            // открытый чат прочитан хотя бы раз (до того — без «пока нет сообщений»)
  let loadGen = 0;
  let link = null;                 // последнее `link` учётки: up | down | relogin
  let lost = false;                // поток учётки прерывался: следующее `link up` перечитывает чат
  let reloadTimer = 0;
  let paintFrame = 0;
  const dirty = new Set();         // id сообщений, ждущих перерисовки в следующем кадре
  const nodes = new Map();         // id сообщения → узел ленты
  const openThink = new Set();     // id ответов с раскрытым рассуждением
  const openTools = new Set();     // `${id}:${call_id}` раскрытых плашек
  const early = new Map();         // id ответа, которого лента ещё не знает (ответ POST или перечитывание впереди) → куски
  const pendingBoxes = new Map();  // id ответа → узел карточки паузы: перерисовка ленты не теряет выбранное в ней

  /* ---- разметка ---- */
  const titleNode = el('h2', { class: 'owui-feed-title' });
  const feed = el('div', { class: 'owui-feed-list' });
  const scroller = el('div', { class: 'owui-messages', id: 'owui-messages' }, feed);
  let follow = true;               // вид у последнего сообщения: перерисовка и дорисованная разметка докручивают вниз
  scroller.addEventListener('scroll', () => { follow = nearBottom(); }, { passive: true });
  const notice = el('p', { class: 'error owui-chat-error', id: 'owui-chat-error', 'aria-live': 'polite' });
  const queuedNote = el('p', { class: 'muted small owui-queued', id: 'owui-queued', 'aria-live': 'polite', hidden: true });
  const chips = el('div', { class: 'att-chips', id: 'owui-atts' });
  const input = el('textarea', { id: 'owui-input', rows: '1', placeholder: tr("Напишите сообщение…"), 'aria-label': tr("Сообщение"),
    maxlength: '200000' });
  const file = el('input', { type: 'file', id: 'owui-attach', class: 'sr', multiple: true });
  const clip = el('label', { class: 'icon-btn attach', title: tr("Прикрепить файл") }, icon('clip'),
    el('span', { class: 'sr' }, tr("Прикрепить файл")), file);
  const select = el('select', { id: 'owui-model' });
  const modelPick = picker(host, tr("Модель"), select);
  const send = el('button', { type: 'submit', id: 'owui-send', class: 'send composer-go', 'aria-label': tr("Отправить"),
    title: tr("Отправить") }, icon('send'));
  const stop = el('button', { type: 'button', id: 'owui-stop', class: 'send composer-go composer-stop', 'aria-label': tr("Остановить"),
    title: tr("Остановить"), hidden: true }, icon('stop'));
  const inputBox = fieldBox(input, { attach: clip });
  const form = el('form', { class: 'composer owui-composer', id: 'owui-composer', hidden: readOnly }, chips, inputBox.box, inputBox.error,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, modelPick.wrap), stop, send));
  const feedHead = el('div', { class: 'owui-feed-head' }, titleNode);
  const root = el('div', { class: 'owui-chat-view', id: 'owui-feed', 'data-account': String(account) },
    feedHead, scroller, queuedNote, notice, form);
  host.append(root);
  // «Поделиться и экспортировать» (owui_share.py): ссылка — снимок ветки, файлы — только именами
  const sharing = shareMenu(root, { id: 'owui-share', content: scroller, failed: (err) => { notice.textContent = words(err); } });
  feedHead.append(sharing.button);
  function paintShare() {
    const path = chat ? `${base}/chats/${encodeURIComponent(chat)}` : null;
    sharing.set(path ? { share: `${path}/share`, export: `${path}/export`, title: titleNode.textContent, kind: 'owui' } : null);
  }
  if (readOnly) root.append(stop);

  /* ---- лента ---- */

  const fileUrl = (id) => `${base}/files/${encodeURIComponent(id)}`;

  function filesNode(files) {
    if (!Array.isArray(files) || !files.length) return null;
    return el('div', { class: 'msg-files' }, ...files.map((f) => {
      if (f.kind === 'image') {
        return el('a', { class: 'msg-file msg-file-image', href: fileUrl(f.id), target: '_blank', rel: 'noopener noreferrer',
          title: f.name || '' }, el('img', { src: fileUrl(f.id), alt: f.name || tr("Картинка"), loading: 'lazy' }));
      }
      return el('a', { class: 'msg-file msg-file-link', href: fileUrl(f.id), download: f.name || '' }, icon('file'),
        el('span', { class: 'att-name' }, f.name || tr("Файл")));
    }));
  }

  function thinkNode(m) {
    const r = m.reasoning;
    if (!r || !r.text) return null;
    const running = !m.done && !m.pending && !m.text;
    const summary = running ? tr("Думает…")
      : Number.isFinite(r.seconds) ? tr("💭 Рассуждала {0} с", Math.max(1, Math.round(r.seconds))) : tr("💭 Рассуждение");
    const details = el('details', { class: 'think owui-think' }, el('summary', {}, summary),
      el('div', { class: 'think-text' }, r.text));
    details.open = openThink.has(m.id);
    details.addEventListener('toggle', () => (details.open ? openThink.add(m.id) : openThink.delete(m.id)));
    return details;
  }

  const TOOL_WORDS = { completed: tr("готово"), failed: tr("ошибка"), in_progress: tr("выполняется") };

  function toolsNode(m) {
    if (!Array.isArray(m.tools) || !m.tools.length) return null;
    return el('div', { class: 'owui-tools' }, ...m.tools.map((t) => {
      const status = t.status === 'failed' ? 'failed' : t.status === 'completed' || typeof t.result === 'string' ? 'completed' : 'in_progress';
      const head = [el('span', { class: 'owui-tool-name' }, t.name || tr("инструмент")),
        el('span', { class: 'owui-tool-state' }, TOOL_WORDS[status])];
      if (typeof t.result !== 'string') {
        return el('span', { class: 'owui-tool', 'data-status': status },
          status === 'in_progress' ? el('span', { class: 'spin', 'aria-hidden': 'true' }) : null, ...head);
      }
      const key = `${m.id}:${t.call_id}`;
      const details = el('details', { class: 'owui-tool', 'data-status': status }, el('summary', {}, ...head),
        el('pre', { class: 'owui-tool-result' }, t.result || tr("Пустой результат")));
      details.open = openTools.has(key);
      details.addEventListener('toggle', () => (details.open ? openTools.add(key) : openTools.delete(key)));
      return details;
    }));
  }

  function metaText(m) {
    if (m.error !== null && m.error !== undefined) return { text: m.error || tr("Ответ не получен"), error: true };
    if (m.done || m.pending) return null;
    return { text: active ? tr("ответ идёт…") : tr("ответ не закончен"), error: false };
  }

  function build(m) {
    if (m.role === 'user') {
      return el('div', { class: 'msg user owui-msg', 'data-id': m.id }, filesNode(m.files),
        m.text ? el('div', { class: 'bubble' }, m.text) : null);
    }
    const body = el('div', { class: 'msg-body' });
    const md = el('div', { class: 'md' });
    if (m.done && m.text) renderMarkdownInto(md, ownFiles(m.text), { valid: () => md.isConnected, update: (change) => {
      const stick = follow;            // разметка дорисовывается после render(): вид у конца — докрутить
      change();
      if (stick) scroller.scrollTop = scroller.scrollHeight;
    } });
    else if (m.text) md.append(el('div', { class: 'owui-stream' }, m.text));
    let pending = pendingBoxes.get(m.id);
    if (!pending) {
      pending = el('div', { class: 'msg-pending' });
      pendingBoxes.set(m.id, pending);
    }
    paintOwuiPending(pending, m.pending || null, (decision) => resolve(m.id, decision));
    const meta = metaText(m);
    body.append(...[thinkNode(m), toolsNode(m),
      ...(Array.isArray(m.other) ? m.other : []).map((type) => el('p', { class: 'muted small owui-other' }, tr("элемент {0}", type))),
      md, filesNode(m.files), pending,
      meta ? el('p', { class: `msg-meta${meta.error ? ' error' : ''}` }, meta.text) : null,
      m.model ? el('p', { class: 'msg-model muted small' }, m.model) : null].filter(Boolean));
    return el('div', { class: 'msg assistant owui-msg', 'data-id': m.id }, el('span', { class: 'mark', 'aria-hidden': 'true' }), body);
  }

  function nearBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }

  /** Файлы Open WebUI в тексте ответа (`/api/v1/files/<id>/content` — путь его сервера) — через сервер Студии, полным
   *  адресом своего сайта: относительный путь markdown.js счёл бы путём файла компьютера и убрал бы ссылку. */
  const ownFiles = (text) => text.replace(OWUI_FILE, (_, before, id) => `${before}${location.origin}${fileUrl(id)}`);

  function render() {
    const stick = follow;
    nodes.clear();
    for (const id of pendingBoxes.keys()) if (!messages.some((m) => m.id === id)) pendingBoxes.delete(id);
    feed.replaceChildren(...messages.map((m) => {
      const node = build(m);
      nodes.set(m.id, node);
      return node;
    }), ...queued.map((q) => el('div', { class: 'msg user owui-msg owui-msg-queued' },
      el('div', { class: 'bubble' }, q.text), el('span', { class: 'muted small owui-queued-mark' }, tr("в очереди")))));
    if (loaded && !messages.length && !queued.length) {
      feed.append(el('p', { class: 'muted owui-feed-empty' }, chat ? tr("В чате пока нет сообщений") : tr("Новый чат: напишите первое сообщение")));
    }
    queuedNote.hidden = !queued.length;
    queuedNote.textContent = queued.length ? tr("Сообщение в очереди: Open WebUI отправит его после текущего ответа") : '';
    if (stick) scroller.scrollTop = scroller.scrollHeight;
    sync();
  }

  /** Перерисовать одно сообщение в следующем кадре (поток: кусок за куском). */
  function repaint(m) {
    dirty.add(m.id);
    if (paintFrame) return;
    paintFrame = requestAnimationFrame(() => {
      paintFrame = 0;
      const stick = nearBottom();
      for (const id of dirty) {
        const m2 = messages.find((x) => x.id === id);
        const old = nodes.get(id);
        if (!m2 || !old) continue;
        const fresh = build(m2);
        old.replaceWith(fresh);
        nodes.set(id, fresh);
      }
      dirty.clear();
      if (stick) scroller.scrollTop = scroller.scrollHeight;
    });
  }

  /* ---- чат с сервера ---- */

  /** Сервер ещё не сохранил куски, которые лента уже показала: идущий ответ сохраняет показанное. */
  function merge(fresh) {
    const known = new Map(messages.map((m) => [m.id, m]));
    return fresh.map((m) => {
      const old = known.get(m.id);
      if (!old || m.done || old.role !== 'assistant') return m;
      const text = (old.text || '').length > (m.text || '').length ? old.text : m.text;
      const reasoning = (old.reasoning?.text || '').length > (m.reasoning?.text || '').length ? old.reasoning : m.reasoning;
      const tools = (old.tools || []).length > (m.tools || []).length ? old.tools : m.tools;
      return { ...m, text, reasoning, tools };
    });
  }

  async function load() {
    if (!chat) return;
    const mine = ++loadGen;
    try {
      const data = await api('GET', `${base}/chats/${encodeURIComponent(chat)}`);
      if (gone || mine !== loadGen) return;
      loaded = true;
      const before = new Set(messages.filter((m) => m.role === 'user').map((m) => m.text));
      messages = merge(Array.isArray(data.messages) ? data.messages : []);
      active = data.active === true;
      // очередь: сообщение, появившееся в ветке, больше не ждёт
      const arrived = messages.filter((m) => m.role === 'user' && !before.has(m.text)).map((m) => m.text);
      queued = queued.filter((q) => {
        const at = arrived.indexOf(q.text);
        if (at < 0) return true;
        arrived.splice(at, 1);
        return false;
      });
      titleNode.textContent = data.title || tr("Без названия");
      paintShare();
      chatModel = Array.isArray(data.models) && typeof data.models[0] === 'string' ? data.models[0] : null;
      chooseModel();
      notice.textContent = '';
      render();
      drainEarly(true);
    } catch (err) {
      if (gone || mine !== loadGen) return;
      failed = true;
      notice.textContent = words(err);
    }
  }

  function scheduleReload() {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(load, RELOAD_MS);
  }

  /* ---- модели ---- */

  let chatModel = null;            // модель открытого чата (GET чата): умолчание поля, если она есть в списке

  /** Модель поля, пока человек не выбрал свою: модель чата, иначе `default` списка. */
  function chooseModel() {
    if (picked || !select.options.length) return;
    const ids = [...select.options].map((o) => o.value);
    select.value = chatModel && ids.includes(chatModel) ? chatModel : select.dataset.fallback || ids[0];
    modelPick.sync();
    sync();
  }

  async function loadModels() {
    try {
      const data = await api('GET', `${base}/models`);
      if (gone) return;
      const list = Array.isArray(data.models) ? data.models : [];
      select.replaceChildren(...list.map((m) => el('option', { value: m.id }, m.name || m.id)));
      select.dataset.fallback = data.default || list[0]?.id || '';
      chooseModel();
      modelPick.sync();
      sync();
    } catch (err) {
      if (gone) return;
      failed = true;
      notice.textContent = words(err);
    }
  }

  /** Чат или модели не прочитались (нет связи, нужен вход заново) — прочитать снова. */
  function retry() {
    if (!failed || gone) return;
    failed = false;
    notice.textContent = '';
    if (!select.options.length) loadModels();
    load();
  }

  select.addEventListener('change', () => { picked = true; });

  /* ---- вложения ---- */

  async function discard(item, unlock = false) {
    await item.request;             // id известен только после завершения загрузки
    if (!item.id) return;
    try {
      await api('DELETE', fileUrl(item.id), undefined, { keepalive: true, unlock });
    } catch (err) {
      if (err?.status !== 404) throw err;     // мог уйти в сообщение после обрыва запроса отправки
    }
  }

  async function removeAttachment(item) {
    if (sending || item.removing) return;
    item.removing = true;
    paintChips();
    sync();
    try {
      await discard(item, true);
      attachments = attachments.filter((x) => x !== item);
    } catch (err) {
      if (!gone) notice.textContent = words(err);
    } finally {
      item.removing = false;
      if (!gone) { paintChips(); sync(); }
    }
  }

  function paintChips() {
    chips.replaceChildren(...attachments.map((a) => {
      const state = a.uploading ? tr("загружается…") : a.error ? a.error
        : a.failed ? tr("Open WebUI не обработал файл: модель может его не увидеть") : '';
      const remove = iconButton('x', tr("Убрать вложение"), { class: 'icon-btn owui-att-remove',
        disabled: !!a.removing || sending });
      remove.addEventListener('click', () => removeAttachment(a));
      return el('span', { class: `att-chip owui-att${a.error || a.failed ? ' bad' : ''}`, 'data-state':
        a.uploading ? 'uploading' : a.error ? 'error' : a.failed ? 'failed' : 'ready' },
      icon(a.kind === 'image' ? 'photo' : 'file'), el('span', { class: 'att-name' }, a.name),
      state ? el('span', { class: `att-state${a.error || a.failed ? ' error' : ' muted'}` }, state) : null, remove);
    }));
  }

  function attach(list) {
    if (readOnly) return;
    if (sending) return;
    for (const f of list) {
      if (attachments.length >= FILES_MAX) {
        notice.textContent = tr("Не больше {0} вложений в сообщении", FILES_MAX);
        break;
      }
      const item = { name: f.name, kind: (f.type || '').startsWith('image/') ? 'image' : 'file', uploading: false,
        failed: false, error: '' };
      if (f.size > FILE_MAX) item.error = tr("вложение больше 20 МиБ");
      else if (!f.size) item.error = tr("файл пустой");
      else item.uploading = true;
      attachments.push(item);
      if (!item.uploading) continue;
      item.request = api('POST', `${base}/files`, f, { headers: {
        'x-studio-name': encodeURIComponent(f.name), 'content-type': f.type || 'application/octet-stream' } })
        .then((out) => {
          item.id = out.id;
          item.kind = out.kind === 'image' ? 'image' : 'file';
          item.failed = out.failed === true;
        })
        .catch((err) => { item.error = words(err); })
        .finally(() => {
          item.uploading = false;
          if (!gone) { paintChips(); sync(); }
        });
    }
    paintChips();
    sync();
  }

  file.addEventListener('change', () => {
    attach([...file.files]);
    file.value = '';
  });

  /* ---- отправка, стоп, пауза ---- */

  function sync() {
    const pending = attachments.some((a) => a.uploading || a.removing);
    send.disabled = sending || pending || !select.value;
    stop.hidden = !chat || !active;
    input.readOnly = sending;
    file.disabled = sending;
  }

  const localUser = (text, files) => ({ id: `local-${++localIds}`, role: 'user', text, files, tools: [], other: [],
    done: true, error: null, pending: null, reasoning: null });
  const placeholder = (id, model) => ({ id, role: 'assistant', model, text: '', reasoning: null, tools: [], files: [],
    other: [], done: false, error: null, pending: null });

  async function submit() {
    if (readOnly || sending) return;
    const text = input.value;
    const ready = attachments.filter((a) => a.id);
    if (attachments.some((a) => a.uploading || a.removing)) return;
    if (!text.trim() && !ready.length) { input.focus(); return; }
    const model = select.value;
    if (!model) return;
    sending = true;
    notice.textContent = '';
    paintChips();
    sync();
    const files = ready.map((a) => ({ id: a.id, name: a.name, kind: a.kind }));
    try {
      if (!chat) {
        const out = await api('POST', `${base}/chats`, { model, text, files: files.map((f) => f.id) });
        if (gone) return;
        chat = out.chat_id;
        messages = [localUser(text, files), placeholder(out.message_id, model)];
        active = true;
        titleNode.textContent = tr("Новый чат");
        paintShare();
        created(chat);
      } else {
        const parent = messages[messages.length - 1]?.id;
        const out = await api('POST', `${base}/chats/${encodeURIComponent(chat)}/messages`,
          { model, text, files: files.map((f) => f.id), parent_id: parent });
        if (gone) return;
        if (out.queued) queued.push({ text });
        else {
          messages.push(localUser(text, files), placeholder(out.message_id, model));
          active = true;
        }
      }
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      attachments = [];
      paintChips();
      render();
      drainEarly();
      scroller.scrollTop = scroller.scrollHeight;
      scheduleReload();
    } catch (err) {
      if (!gone) notice.textContent = words(err);
    } finally {
      sending = false;
      if (!gone) { paintChips(); sync(); }
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit();
  });
  enterSends(input, () => submit());

  stop.addEventListener('click', async () => {
    if (!chat) return;
    stop.disabled = true;
    try {
      await api('POST', `${base}/chats/${encodeURIComponent(chat)}/stop`);
      if (gone) return;
      active = false;
      render();
      scheduleReload();
    } catch (err) {
      if (!gone) notice.textContent = words(err);
    } finally {
      stop.disabled = false;
    }
  });

  async function resolve(id, decision) {
    if (readOnly) return;
    try {
      await api('POST', `${base}/chats/${encodeURIComponent(chat)}/resolve`, { message_id: id, ...decision });
    } catch (err) {
      // решили в другом окне: перечитанный чат уберёт карточку; та же пауза ещё в ответе — кнопки снова доступны
      if (err?.code === 'resolved') scheduleReload();
      throw err;
    }
    const m = messages.find((x) => x.id === id);
    if (gone || !m) return;
    m.pending = null;
    active = true;
    repaint(m);
    sync();
  }

  /* ---- события SSE учётки ---- */

  /** Кусок потока (`delta`, `item`) — в ответ `m`. */
  function apply(m, type, data) {
    if (type === 'delta') {
      if (typeof data.text !== 'string') return;
      if (data.kind === 'reasoning') m.reasoning = { text: (m.reasoning?.text || '') + data.text, seconds: null };
      else m.text = (m.text || '') + data.text;
      m.done = false;
    } else if (data.tool && typeof data.tool === 'object') {
      const tools = m.tools || (m.tools = []);
      const found = tools.find((t) => t.call_id === data.tool.call_id);
      if (found) found.status = data.tool.status || found.status;
      else tools.push({ call_id: data.tool.call_id, name: data.tool.name, status: data.tool.status, result: null });
    }
    if (!active) { active = true; sync(); }
    repaint(m);
  }

  function hold(id, type, data) {
    const list = early.get(id) || [];
    if (list.length < 2000) list.push([type, data]);
    early.set(id, list);
  }

  /** Куски, пришедшие раньше ответа POST или перечитывания, — в свои ответы; `drop` — забыть оставшиеся. */
  function drainEarly(drop = false) {
    for (const [id, list] of early) {
      const m = messages.find((x) => x.id === id && x.role === 'assistant');
      if (m) for (const [type, data] of list) apply(m, type, data);
      if (m || drop) early.delete(id);
    }
  }

  function event(type, data) {
    if (gone || !data || typeof data !== 'object') return;
    if (type === 'lost') {             // поток учётки прервался (owui.js): куски и события могли пропасть
      lost = true;
      return;
    }
    if (type === 'link') {
      const before = link;
      link = data.state;
      if (data.state === 'up' && chat && (lost || before === 'down' || before === 'relogin')) {
        lost = false;
        scheduleReload();
      }
      return;
    }
    const id = typeof data.message_id === 'string' ? data.message_id : null;
    const piece = type === 'delta' || type === 'item';
    if (piece && !chat && sending && id) { hold(id, type, data); return; }     // новый чат: id чата ещё не пришёл
    if (!chat || data.chat_id !== chat) return;
    const m = id ? messages.find((x) => x.id === id) : null;
    if (piece) {
      if (m && m.role === 'assistant') apply(m, type, data);
      else if (id) {
        hold(id, type, data);
        scheduleReload();
      }
    } else if (type === 'done') {
      if (m) {
        m.done = true;
        m.error = data.error ?? null;
      }
      signal(data.error ? 'error' : 'done', id);
      active = false;
      render();
      scheduleReload();
    } else if (type === 'pending') {
      signal('ask', id);
      scheduleReload();
    } else if (type === 'active') {
      const was = active;
      active = data.active === true;
      if (!active || messages.some((x) => x.pending) || (active && !was && !messages.some((x) => !x.done))) scheduleReload();
      sync();
    } else if (type === 'title' && typeof data.title === 'string') {
      titleNode.textContent = data.title || tr("Без названия");
      paintShare();
    } else if (type === 'queued') {
      queuedNote.hidden = false;
      queuedNote.textContent = tr("Сообщение в очереди: Open WebUI отправит его после текущего ответа");
    }
  }

  /* ---- начало ---- */

  titleNode.textContent = chat ? '' : tr("Новый чат");
  render();
  load();
  loadModels();

  return {
    account,
    chat: () => chat,
    event,
    retry,
    unmount() {
      gone = true;
      if (!sending) {
        for (const a of attachments) if (!a.removing) discard(a).catch(() => {});
      }
      clearTimeout(reloadTimer);
      cancelAnimationFrame(paintFrame);
      sharing.unmount();
      modelPick.unmount();
      root.remove();
    },
  };
}
