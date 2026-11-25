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
  document.title = tr("Студия — просмотр по ссылке");
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
  else if (typeof s.note === 'string' && s.note) li.append(el('span', { class: 'muted' }, ` — ${partText(s.note)}`));
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

/* запись другого раздела (chat_shares.ITEMS): подпись под заголовком и название без имени записи */
const KINDS = {
  transcripts: [tr("Только чтение · расшифровка"), tr("Расшифровка")],
  translations: [tr("Только чтение · перевод"), tr("Перевод")],
  scans: [tr("Только чтение · скан"), tr("Скан")],
  images: [tr("Только чтение · картинка"), tr("Картинка")],
  videos: [tr("Только чтение · ролик"), tr("Ролик")],
};
const SHOWN = { images: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'], videos: ['video/mp4', 'video/webm'] };

function stamp(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const mm = String(Math.floor(s / 60) % 60).padStart(2, '0'), ss = String(s % 60).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
}

function langName(code) {
  try { return new Intl.DisplayNames([document.documentElement.lang || 'ru'], { type: 'language' }).of(code) || code; }
  catch { return typeof code === 'string' ? code : ''; }
}

/* картинка или ролик снимка — байты по read-токену (`/share/file`), показ — blob с типом из снимка */
async function showFile(file, kind, box) {
  const current = access;
  try {
    const blob = await (await request('/share/file', { access: current, file: file.file })).blob();
    if (access !== current) return;
    const href = URL.createObjectURL(new Blob([blob], { type: file.mime }));
    blobs.add(href);
    box.replaceChildren(kind === 'images' ? el('img', { class: 'shared-image shared-picture', src: href, alt: tr("Картинка") })
      : el('video', { class: 'shared-video', src: href, controls: true, playsinline: true, preload: 'metadata' }));
  } catch (err) {
    if (access !== current) return;
    if (err.status === 404) clear(err.message);
    else box.replaceChildren(el('p', { class: 'error' }, err.message || tr("Не удалось загрузить файл")));
  }
}

function fileButtons(files) {
  const box = el('div', { class: 'shared-files' });
  for (const file of files) {
    if (!file || typeof file.file !== 'string') continue;
    const button = el('button', { type: 'button', class: 'btn btn-sm' }, file.name || tr("Скачать вложение"));
    button.addEventListener('click', () => download(file, button));
    box.append(button);
  }
  return box;
}

function itemNodes(item, markdown) {
  const text = (value, md) => {
    const body = el('div', { class: md && markdown ? 'md shared-body' : 'shared-user', dir: 'auto' });
    if (md && markdown) body.append(markdown.renderMarkdown(typeof value === 'string' ? value : ''));
    else body.textContent = typeof value === 'string' ? value : '';
    return body;
  };
  const nodes = [];
  if (item.kind === 'transcripts') {
    const segments = Array.isArray(item.segments) ? item.segments.filter((s) => s && typeof s.text === 'string') : [];
    nodes.push(el('div', { class: 'shared-transcript' }, ...segments.map((s) => (s.system
      ? el('p', { class: 'muted small' }, s.text)
      : el('p', { class: 'shared-segment' }, el('span', { class: 'shared-when muted' }, stamp(s.start)), ' ',
        typeof s.speaker === 'string' && s.speaker ? el('b', {}, `${s.speaker}: `) : null, s.text)))));
  } else if (item.kind === 'translations') {
    nodes.push(el('section', { class: 'shared-part' }, el('h2', {}, `${tr("Исходный текст")} · ${langName(item.source_lang)}`),
      text(item.source, false)),
    el('section', { class: 'shared-part' }, el('h2', {}, `${tr("Перевод")} · ${langName(item.target_lang)}`),
      text(item.text, !!item.markdown)));
  } else if (item.kind === 'scans') {
    for (const page of Array.isArray(item.pages) ? item.pages : []) {
      if (!page || !Number.isInteger(page.n)) continue;
      nodes.push(el('section', { class: 'shared-part' }, el('h2', {}, tr("Страница {0}", page.n)),
        typeof page.text === 'string' && page.text ? text(page.text, true)
          : el('p', { class: 'muted' }, tr("Страница не распознана"))));
    }
  } else if (item.kind === 'images' || item.kind === 'videos') {
    const file = item.file && typeof item.file.file === 'string' ? item.file : null;
    const box = el('div', { class: 'shared-media' });
    if (file && SHOWN[item.kind].includes(file.mime)) showFile(file, item.kind, box);
    nodes.push(box, el('section', { class: 'shared-part' }, el('h2', {}, tr("Промпт")), text(item.prompt, false),
      typeof item.model === 'string' && item.model ? el('p', { class: 'muted small' }, tr("Модель: {0}", item.model)) : null),
    file ? fileButtons([file]) : null);
  }
  if (Array.isArray(item.attachments) && item.attachments.length) {
    nodes.push(el('section', { class: 'shared-part' }, el('h2', {}, tr("Файлы")), fileButtons(item.attachments)));
  }
  return nodes;
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
    // запись другого раздела — `item`; `messages` у неё — заглушка для прежней версии студии
    const item = snapshot.item && KINDS[snapshot.item.kind] ? snapshot.item : null;
    document.getElementById('shared-title').textContent = snapshot.title || (item ? KINDS[item.kind][1] : tr("Чат"));
    document.getElementById('shared-kind').textContent = item ? KINDS[item.kind][0] : tr("Только чтение · снимок переписки");
    if (item) feed.append(el('article', { class: 'msg shared-message shared-item' }, ...itemNodes(item, markdown)));
    for (const message of item ? [] : snapshot.messages) {
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
      // навык сообщения — только имя (текст навыка в ссылку не входит)
      if (typeof message.skill === 'string' && message.skill) node.append(el('p', { class: 'muted small shared-skill' }, tr("Навык: {0}", message.skill)));
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
    if (!item && !snapshot.messages.length) feed.append(el('p', { class: 'muted' }, tr("В этом чате пока нет сообщений")));
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
