import { tr } from './i18n.js';
/* «Файлы диалога (N)» (спека 2026-11-25-studio-skills-status-local-models, п. 15): вложения, медиа диалога и файлы
 * результатов MCP одним списком — GET /api/chats/{id}/files (`chat_info.files`). Открывают и скачивают их прежние
 * маршруты со своими проверками (владелец, защищённый источник): картинка — «Открыть» в окне, прочее — «Скачать». */
import { api } from './api.js';
import { el, openDialog, words } from './ui.js';
import { byteSize } from './chat-files.js';

/** Число файлов открытого диалога по ответу GET /api/chats/{id}: вложения выбранных сообщений, медиа диалога и
 * файлы шагов ответов — то же, что перечислит `chat_info.files`. */
export function fileCount(chat) {
  let n = Array.isArray(chat?.media) ? chat.media.length : 0;
  for (const m of chat?.messages || []) {
    if (Array.isArray(m.attachments)) n += m.attachments.length;
    for (const step of Array.isArray(m.tools) ? m.tools : []) {
      if (step && Array.isArray(step.files)) n += step.files.filter((f) => f && typeof f === 'object').length;
    }
  }
  return n;
}

const KINDS = { attachment: () => tr("вложение"), media: () => tr("медиа диалога"), result: () => tr("результат MCP") };

function viewer(host, item, name) {
  openDialog(host, { id: 'chat-file-view', title: name, cancelText: tr("Закрыть"),
    body: [el('img', { class: 'chat-file-picture', src: item.url, alt: name })] });
}

/** Окно со списком файлов диалога `chatId`. */
export async function filesDialog(host, chatId) {
  const title = el('span', {}, tr("Файлы диалога"));
  const list = el('ul', { class: 'chat-file-list' });
  const note = el('p', { class: 'muted small', role: 'status' }, tr("Загружаю…"));
  const dialog = openDialog(host, { id: 'chat-files-dialog', title, body: [note, list], cancelText: tr("Закрыть") });
  let items;
  try {
    items = (await api('GET', `/api/chats/${encodeURIComponent(chatId)}/files`)).items;
  } catch (err) {
    note.textContent = words(err);
    note.classList.add('error');
    return dialog;
  }
  title.textContent = tr("Файлы диалога ({0})", items.length);
  note.textContent = items.length ? '' : tr("В диалоге нет файлов");
  note.hidden = !!items.length;
  list.replaceChildren(...items.map((item) => {
    const name = item.name || tr("файл");
    const open = String(item.mime || '').startsWith('image/')
      ? el('button', { type: 'button', class: 'btn btn-sm chat-file-open' }, tr("Открыть"))
      : el('a', { class: 'btn btn-sm chat-file-open', href: item.url, download: name, rel: 'noopener' }, tr("Скачать"));
    if (open.tagName === 'BUTTON') open.addEventListener('click', () => viewer(host, item, name));
    return el('li', { class: 'chat-file', 'data-kind': item.kind },
      el('span', { class: 'chat-file-name' }, name),
      el('span', { class: 'muted small chat-file-about' },
        `${(KINDS[item.kind] || KINDS.attachment)()} · ${Number.isFinite(item.size) ? byteSize(item.size) : '—'}`),
      open);
  }));
  return dialog;
}
