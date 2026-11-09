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
    const err = new Error(detail?.message || `Ошибка сервера (${response.status})`);
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
  document.title = 'Студия — просмотр чата';
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
    else status.textContent = err.message || 'Не удалось скачать файл';
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
    status.textContent = 'Нет связи: доступ будет проверен после восстановления сети';
  }
  if (access === current) timer = setTimeout(check, 30000);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (open.disabled || !form.reportValidity()) return;
  const token = location.hash.slice(1);
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) { error.textContent = 'Ссылка недействительна'; return; }
  const version = ++generation;
  open.disabled = true;
  error.textContent = '';
  try {
    const response = await request('/share/unlock', { token, pin: pin.value });
    pin.value = '';
    const snapshot = await response.json();
    if (version !== generation) return;
    let markdown = null;
    try { markdown = await import('./markdown.js'); } catch { /* safely show plain text if libraries are unavailable */ }
    if (version !== generation) return;
    access = snapshot.access;
    document.getElementById('shared-title').textContent = snapshot.title || 'Чат';
    for (const message of snapshot.messages) {
      const body = el('div', { class: message.role === 'user' ? 'shared-user' : 'md shared-body' });
      const text = typeof message.text === 'string' ? message.text : '';
      if (markdown && message.role === 'assistant') body.append(markdown.renderMarkdown(text));
      else body.textContent = text;
      const node = el('article', { class: 'msg shared-message' },
        el('p', { class: 'shared-role' }, message.role === 'user' ? 'Пользователь' : 'Помощник'), body);
      for (const step of message.steps || []) {
        node.append(el('details', {}, el('summary', {}, partText(step.name || step.tool || 'Команда')),
          el('pre', {}, partText(step.input)), el('pre', {}, partText(step.text))));
      }
      if (message.reasoning) node.append(el('details', {}, el('summary', {}, 'Рассуждение'), el('pre', {}, message.reasoning)));
      for (const image of message.images || []) {
        if (['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(image.mime)
            && typeof image.data === 'string' && /^[A-Za-z0-9+/=\s]+$/.test(image.data)) {
          node.append(el('img', { class: 'shared-image', src: `data:${image.mime};base64,${image.data}`, alt: image.name || 'Вложение' }));
        } else if (typeof image.data === 'string' && /^[A-Za-z0-9+/=\s]+$/.test(image.data)) {
          const button = el('button', { type: 'button', class: 'btn btn-sm' }, image.name || 'Скачать вложение');
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
            } catch { status.textContent = 'Не удалось прочитать вложение'; }
          });
          node.append(button);
        } else node.append(el('span', { class: 'muted' }, image.name || 'Вложение'));
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
    if (!snapshot.messages.length) feed.append(el('p', { class: 'muted' }, 'В этом чате пока нет сообщений'));
    form.hidden = true;
    chat.hidden = false;
    check();
  } catch (err) {
    if (version === generation) { clear(err.message || 'Нет связи с сервером'); }
  } finally { open.disabled = false; }
});

document.getElementById('share-close').addEventListener('click', () => clear());
window.addEventListener('hashchange', () => clear());
window.addEventListener('pagehide', () => clear());
window.addEventListener('pageshow', (event) => { if (event.persisted) clear(); });
document.addEventListener('visibilitychange', () => {
  clearTimeout(timer);
  if (!document.hidden && access) check();
});
window.addEventListener('online', () => { clearTimeout(timer); if (access) check(); });
