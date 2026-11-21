import { message as serverMessage, tr } from './i18n.js';
import { el } from './ui.js';

const form = document.getElementById('share-unlock');
const pin = document.getElementById('share-pin');
const error = document.getElementById('share-error');
const open = document.getElementById('share-open');
const chat = document.getElementById('shared-chat');
const feed = document.getElementById('shared-messages');
const status = document.getElementById('shared-status');
const blobs = new Set();
let access = null;
let timer = null;
let generation = 0;

async function request(path, body) {
  const response = await fetch(path, { method: 'POST', credentials: 'omit', cache: 'no-store',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(30000) });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { /* generic HTTP error below */ }
    const err = new Error(serverMessage(detail?.message) || tr("Ошибка сервера ({0})", response.status));
    err.status = response.status;
    throw err;
  }
  return response;
}

function clear(message = '') {
  generation += 1;
  clearTimeout(timer);
  timer = null;
  access = null;
  pin.value = '';
  feed.replaceChildren();
  document.getElementById('shared-title').textContent = '';
  document.title = tr("Студия — просмотр чата");
  for (const blob of blobs) URL.revokeObjectURL(blob);
  blobs.clear();
  chat.hidden = true;
  form.hidden = false;
  error.textContent = message;
  status.textContent = '';
}

function partText(value) {
  return typeof value === 'string' ? value : JSON.stringify(value ?? '', null, 2);
}

function webLink(url) {
  try { return ['http:', 'https:'].includes(new URL(url).protocol); } catch { return false; }
}

function sourceLink(url, title) {
  const name = typeof title === 'string' && title ? title : (typeof url === 'string' ? url : '');
  return webLink(url) ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, name) : el('span', {}, name);
}

/* шаги «Интернета» и MCP обычного чата — как «Источники» под ответом в чате (chat.js stepNode), без текста для модели */
function stepNode(s) {
  const li = el('li', {});
  if (s.kind === 'search') {
    li.append(s.query ? tr("Поиск: «{0}»", partText(s.query)) : tr("Поиск"));
    const results = Array.isArray(s.results) ? s.results.filter((x) => x && typeof x === 'object') : [];
    if (results.length) {
      li.append(el('ul', {}, ...results.map((x) => el('li', {}, sourceLink(x.url, x.title),
        typeof x.published === 'string' && x.published ? el('span', { class: 'muted' }, ` · ${x.published}`) : null))));
    }
  } else if (s.kind === 'page' || s.kind === 'youtube') {
    li.append(s.kind === 'page' ? tr("Страница: ") : tr("Ролик: "),
      s.error ? el('span', {}, partText(s.title || s.url || '')) : sourceLink(s.url, s.title));
  } else if (s.kind === 'mcp') {
    li.append(['MCP', s.server, s.tool].filter((x) => typeof x === 'string' && x).join(' · '));
    // файлы результата MCP — только имена (web.public): скачать их по ссылке нельзя, файлы остаются на сервере MCP
    const names = (Array.isArray(s.files) ? s.files : []).map((f) => (f && typeof f.name === 'string' ? f.name : ''))
      .filter(Boolean);
    if (names.length) li.append(el('ul', { class: 'shared-files' }, ...names.map((name) => el('li', {}, partText(name)))));
  } else if (s.kind === 'ask') li.append(s.question ? tr("Вопрос: «{0}»", partText(s.question)) : tr("Вопрос модели"));
  else if (s.kind === 'knowledge') {
    li.append(s.query ? tr("Знания: «{0}»", partText(s.query)) : tr("Знания"));
    const sources = (Array.isArray(s.sources) ? s.sources : []).filter((x) => typeof x === 'string' && x);
    if (sources.length) li.append(el('span', { class: 'muted' }, ` · ${sources.join(', ')}`));
  } else if (s.kind === 'duplicate') li.append(tr("Повтор пропущен: {0}", partText(s.tool)));
  else if (s.kind === 'todo') li.append(tr("План работы"));
  // шаг памяти — только действие: текста записи в снимке нет
  else if (s.kind === 'memory') li.append(s.action === 'forget' ? tr("Память: запись удалена") : tr("Память: запись добавлена"));
  else li.append(tr("Инструмент"));
  if (s.error) li.append(el('span', { class: 'error' }, ` — ${partText(s.error)}`));
  return li;
}

function sourcesNode(steps) {
  const links = steps.flatMap((s) => (s.kind === 'search' ? (Array.isArray(s.results) ? s.results : []).map((x) => x && x.url)
    : (s.error ? [] : [s.url]))).filter(webLink).length;
  return el('details', { class: 'shared-sources' },
    el('summary', {}, links ? tr("Источники ({0})", links) : tr("Вызовы инструментов ({0})", steps.length)),
    el('ul', {}, ...steps.map(stepNode)));
}

/* план ответа (`messages.todo`) — как в чате (chat.js paintTodo): «План: N из M» и шаги с отметкой состояния */
const TODO_STATES = { done: tr("сделано"), in_progress: tr("в работе"), pending: tr("впереди") };

function todoNode(items) {
  const list = Array.isArray(items) ? items.filter((i) => i && typeof i.text === 'string' && TODO_STATES[i.status]) : [];
  if (!list.length) return null;
  const done = list.filter((i) => i.status === 'done').length;
  return el('div', { class: 'chat-todo', role: 'group', 'aria-label': tr("План работы") },
    el('p', { class: 'chat-todo-head' }, tr("План: {0} из {1}", done, list.length)),
    el('ol', {}, ...list.map((i) => el('li', { class: `todo-${i.status}` },
      el('span', { class: 'sr' }, `${TODO_STATES[i.status]}: `), i.text))));
}

async function download(file, button) {
  const current = access;
  if (!current) return;
  button.disabled = true;
  try {
    const response = await request('/share/file', { access: current, file: file.file });
    const blob = await response.blob();
    if (access !== current) return;
    const href = URL.createObjectURL(blob);
    blobs.add(href);
    const link = el('a', { href, download: file.name || 'file' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => { URL.revokeObjectURL(href); blobs.delete(href); }, 60000);
  } catch (err) {
    if (access !== current) return;
    if (err.status === 404) clear(err.message);
    else status.textContent = err.message || tr("Не удалось скачать файл");
  } finally { button.disabled = false; }
}

async function check() {
  const current = access;
  if (!current) return;
  try {
    await request('/share/check', { access: current });
    if (access === current) status.textContent = '';
  } catch (err) {
    if (access !== current) return;
    if (err.status === 404) { clear(err.message); return; }
    status.textContent = tr("Нет связи: доступ будет проверен после восстановления сети");
  }
  if (access === current) timer = setTimeout(check, 30000);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (open.disabled || !form.reportValidity()) return;
  const token = location.hash.slice(1);
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) { error.textContent = tr("Ссылка недействительна"); return; }
  const version = ++generation;
  open.disabled = true;
  error.textContent = '';
  try {
    const response = await request('/share/unlock', { token, pin: pin.value });
    pin.value = '';
    const snapshot = await response.json();
    if (version !== generation) return;
    let markdown = null, changeView = null;
    try { markdown = await import('./markdown.js'); } catch { /* safely show plain text if libraries are unavailable */ }
    try { ({ changeView } = await import('./code-diff.js')); } catch { /* без диффа — сырой ввод шага */ }
    if (version !== generation) return;
    access = snapshot.access;
    document.getElementById('shared-title').textContent = snapshot.title || tr("Чат");
    for (const message of snapshot.messages) {
      const textNode = (text) => {
        const body = el('div', { class: message.role === 'user' ? 'shared-user' : 'md shared-body' });
        if (markdown && message.role === 'assistant') body.append(markdown.renderMarkdown(text));
        else body.textContent = text;
        return body;
      };
      // Правка файла — тем же диффом, что в «Агентах»; без неё — сырой ввод инструмента.
      const stepNode = (step) => {
        const diff = changeView?.(step.changes);
        return el('details', {}, el('summary', {}, partText(step.name || step.tool || tr("Команда"))),
          diff || el('pre', {}, partText(step.input)), el('pre', {}, partText(step.text)));
      };
      const text = typeof message.text === 'string' ? message.text : '';
      const steps = Array.isArray(message.steps) ? message.steps : [];
      const node = el('article', { class: 'msg shared-message' },
        el('p', { class: 'shared-role' }, message.role === 'user' ? tr("Пользователь") : tr("Помощник")),
        todoNode(message.todo));
      // `parts` — порядок текста и шагов ответа (сервер собрал его из timeline моста); иначе текст, затем шаги.
      const ordered = Array.isArray(message.parts) && message.parts.every((part) => typeof part?.text === 'string'
        || (Number.isInteger(part?.step) && part.step >= 0 && part.step < steps.length));
      if (ordered) {
        for (const part of message.parts) node.append(typeof part.text === 'string' ? textNode(part.text) : stepNode(steps[part.step]));
      } else node.append(textNode(text), ...steps.map(stepNode));
      if (message.status === 'streaming') node.append(el('p', { class: 'muted' }, tr("Ответ не был дописан, когда создавали ссылку")));
      if (typeof message.error === 'string' && message.error) node.append(el('p', { class: 'error' }, serverMessage(message.error)));
      const tools = Array.isArray(message.tools) ? message.tools.filter((s) => s && typeof s === 'object') : [];
      if (tools.length) node.append(sourcesNode(tools));
      if (message.reasoning) node.append(el('details', {}, el('summary', {}, tr("Рассуждение")), el('pre', {}, message.reasoning)));
      for (const image of message.images || []) {
        if (image.missing) {
          node.append(el('p', { class: 'muted small' }, typeof image.name === 'string' && image.name
            ? tr("Изображение «{0}» в ссылку не вошло", image.name) : tr("Изображение в ссылку не вошло")));
        } else if (['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(image.mime)
            && typeof image.data === 'string' && /^[A-Za-z0-9+/=\s]+$/.test(image.data)) {
          node.append(el('img', { class: 'shared-image', src: `data:${image.mime};base64,${image.data}`, alt: image.name || tr("Вложение") }));
        } else if (typeof image.data === 'string' && /^[A-Za-z0-9+/=\s]+$/.test(image.data)) {
          const button = el('button', { type: 'button', class: 'btn btn-sm' }, image.name || tr("Скачать вложение"));
          button.addEventListener('click', () => {
            try {
              const raw = Uint8Array.from(atob(image.data), (c) => c.charCodeAt(0));
              const href = URL.createObjectURL(new Blob([raw], { type: 'application/octet-stream' }));
              blobs.add(href);
              const link = el('a', { href, download: image.name || 'file' });
              document.body.append(link);
              link.click();
              link.remove();
              setTimeout(() => { URL.revokeObjectURL(href); blobs.delete(href); }, 60000);
            } catch { status.textContent = tr("Не удалось прочитать вложение"); }
          });
          node.append(button);
        } else node.append(el('span', { class: 'muted' }, image.name || tr("Вложение")));
      }
      const files = el('div', { class: 'shared-files' });
      for (const file of message.attachments || []) {
        const button = el('button', { type: 'button', class: 'btn btn-sm' }, file.name);
        button.addEventListener('click', () => download(file, button));
        files.append(button);
      }
      node.append(files);
      feed.append(node);
    }
    if (!snapshot.messages.length) feed.append(el('p', { class: 'muted' }, tr("В этом чате пока нет сообщений")));
    form.hidden = true;
    chat.hidden = false;
    check();
  } catch (err) {
    if (version === generation) { clear(err.message || tr("Нет связи с сервером")); }
  } finally { open.disabled = false; }
});

/* адрес без секрета (обрезан при пересылке) — сразу, а не после ввода PIN */
function checkAddress() {
  const valid = /^[A-Za-z0-9_-]{43}$/.test(location.hash.slice(1));
  open.disabled = !valid;
  if (!valid) error.textContent = tr("Ссылка недействительна");
}

document.getElementById('share-close').addEventListener('click', () => clear());
window.addEventListener('hashchange', () => { clear(); checkAddress(); });
checkAddress();
window.addEventListener('pagehide', () => clear());
window.addEventListener('pageshow', (event) => { if (event.persisted) clear(); });
document.addEventListener('visibilitychange', () => {
  clearTimeout(timer);
  if (!document.hidden && access) check();
});
window.addEventListener('online', () => { clearTimeout(timer); if (access) check(); });
