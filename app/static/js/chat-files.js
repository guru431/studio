import { tr } from './i18n.js';
/* Файлы между чатом и MCP на странице (mcp_files.py, спека 2026-11-21-studio-source-foundation-design §5 и §7a п. 1, 3).
 *
 * Согласие: шаг ответа (или событие SSE `tool`) с `consent: {server_id, files}` — блок `.msg-consent` под ответом:
 * «Передать серверу «X» файлы диалога: …» с кнопками «Передать» / «Не передавать» (POST /api/chats/<id>/mcp-consent).
 * Решение — из `chat.mcp_file_consent` диалога ({sid: "allowed" | "refused"}): блок показывает его строкой. После
 * «Передать» ответ сам не повторяется — файлы уйдут серверу со следующим сообщением (или «Ответить заново»).
 *
 * Звук и видео диалога (`chat_media`): поле ввода принимает их, если в действующем наборе MCP диалога есть сервер с
 * `chat_files` (каталог GET /api/mcp/catalog), чьё согласие не «Не передавать»; размер сверяется с наибольшим его
 * `max_bytes` до загрузки; загрузка — POST /api/chats/<id>/media телом-файлом, имя — заголовком X-Studio-Name.
 *
 * Файлы результата MCP (`files` шага) — блок `.msg-files` под ответом: картинка `<img loading=lazy>` (нажатие —
 * окно с ней и «Скачать»), видео и звук с `preload="none"`, остальное — ссылка «Скачать «имя»»; файл не загрузился —
 * подпись вместо него. Адрес — GET /api/chats/<id>/messages/<id>/files/<номер шага>/<номер файла>. */
import { api } from './api.js';
import { stateOf } from './chat-mcp.js';
import { icon } from './icons.js';
import { button, el, openDialog } from './ui.js';

export const MEDIA_ACCEPT = 'audio/*,video/*';
const UPLOAD = 'artifact_upload';              // как mcp_files.UPLOAD
const MAX_SERVERS = 16;                        // как mcp_client.MAX_SERVERS

/** Звук или видео по типу файла (остальное — обычное вложение чата). */
export function isMedia(file) {
  return /^(audio|video)\//.test(file.type || '');
}

/** Действующий набор MCP нового диалога (как mcp_sources.dialog_servers): явные плюс общие включённые «в диалоге по
 *  умолчанию», кроме выключенных в черновике; `servers` — ответ GET /api/mcp. */
export function draftIds(draft, servers) {
  const { ids, off } = stateOf(draft);
  const shared = servers.filter((s) => s.shared && s.enabled && s.dialog_default !== false && !off.includes(s.id)
    && !ids.includes(s.id)).map((s) => s.id);
  return [...ids, ...shared].slice(0, MAX_SERVERS);
}

/** Серверы набора `ids`, принимающие файлы чата: [{id, name, max}] по каталогу; `artifact_upload`, выключенный в
 *  диалоге, пометки «интернет» и «с подтверждением» — как в mcp_files.media_limit. */
export function mediaTargets(catalog, ids, { toolsOff, toolsOn }, web) {
  return catalog.filter((s) => {
    const policy = s.chat_files_policy;
    return ids.includes(s.id) && s.state === 'ok' && Number.isFinite(s.chat_files) && s.chat_files > 0 && policy
      && !(toolsOff[s.id] || []).includes(UPLOAD) && (!policy.open_world || web)
      && (!policy.confirm || (toolsOn[s.id] || []).includes(UPLOAD));
  }).map((s) => ({ id: s.id, name: s.name, max: s.chat_files }));
}

/** Размер файла словами: КиБ до мебибайта, дальше МиБ. */
export function byteSize(n) {
  if (n < 1024 * 1024) return tr("{0} КиБ", Math.max(1, Math.round(n / 1024)));
  return tr("{0} МиБ", Math.round(n / 1024 / 1024 * 10) / 10);
}

/** Звук или видео в диалог: тело — сам файл. */
export function uploadMedia(chatId, file) {
  return api('POST', `/api/chats/${encodeURIComponent(chatId)}/media`, file, { headers: {
    'content-type': file.type || 'application/octet-stream', 'x-studio-name': encodeURIComponent(file.name) } });
}

/** Чип звука или видео диалога: имя и размер. */
export function mediaChip(item) {
  return el('span', { class: 'att-chip chat-media-chip', 'data-id': item.id || '' },
    icon(item.mime && item.mime.startsWith('video/') ? 'video' : 'speech'), el('span', { class: 'att-name' }, item.name),
    item.size ? el('span', { class: 'muted att-state' }, byteSize(item.size)) : null,
    item.busy ? el('span', { class: 'muted att-state' }, tr("загружаю…")) : null,
    item.error ? el('span', { class: 'error att-state' }, item.error) : null);
}

/** Запросы согласия ответа: последний на каждый сервер. `steps` — `tools` с сервера или вызовы идущего хода. */
export function consentAsks(steps) {
  const asks = new Map();
  for (const s of steps) {
    if (s && s.consent && Number.isInteger(s.consent.server_id)) {
      asks.set(s.consent.server_id, { sid: s.consent.server_id, server: s.server || s.label || '',
        files: Array.isArray(s.consent.files) ? s.consent.files : [] });
    }
  }
  return [...asks.values()];
}

/** Блок согласия: без решения — вопрос с кнопками; `decide(sid, allow)` — Promise. */
export function consentNode(ask, decision, decide) {
  const box = el('div', { class: 'msg-consent', 'data-sid': String(ask.sid) });
  if (decision === 'allowed') {
    box.append(el('p', { class: 'muted small' },
      tr("Серверу «{0}» разрешено получать файлы диалога — они уйдут ему со следующим сообщением", ask.server)));
    return box;
  }
  if (decision === 'refused') {
    box.append(el('p', { class: 'muted small' }, tr("Файлы диалога серверу «{0}» не передаются", ask.server)));
    return box;
  }
  const choose = (allow) => async () => {
    for (const b of box.querySelectorAll('button')) b.disabled = true;
    try {
      await decide(ask.sid, allow);
    } finally {
      for (const b of box.querySelectorAll('button')) b.disabled = false;
    }
  };
  box.append(el('p', { class: 'msg-consent-text' }, tr("Передать серверу «{0}» файлы диалога: {1}?", ask.server, ask.files.join(', '))),
    el('div', { class: 'msg-consent-actions' },
      button(tr("Передать"), { class: 'btn btn-sm btn-accent consent-allow' }, choose(true)),
      button(tr("Не передавать"), { class: 'btn btn-sm consent-refuse' }, choose(false))));
  return box;
}

function failed(node, name) {
  node.replaceWith(el('p', { class: 'error small msg-file-gone' },
    tr("«{0}» не загрузился: файла больше нет на сервере MCP или он недоступен", name)));
}

function preview(host, url, name) {
  openDialog(host, { id: 'msg-file-dialog', title: name, cancelText: tr("Закрыть"),
    actions: [el('a', { class: 'btn btn-accent', href: url, download: name, id: 'msg-file-save' }, tr("Скачать"))],
    body: [el('img', { class: 'att-photo', src: url, alt: name })] });
}

/** Файлы результатов MCP под ответом `message` диалога `chatId` (`files` шагов); нет — null. */
export function resultFiles(chatId, message, host) {
  if (!message.id || !Array.isArray(message.tools)) return null;
  const items = [];
  message.tools.forEach((step, i) => {
    if (!step || !Array.isArray(step.files)) return;
    step.files.forEach((f, n) => {
      const url = `/api/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(message.id)}/files/${i}/${n}`;
      const name = f.name || tr("файл");
      const mime = String(f.mime || '');
      let node;
      if (mime.startsWith('image/')) {
        const img = el('img', { src: url, alt: name, loading: 'lazy', decoding: 'async' });
        node = el('button', { type: 'button', class: 'msg-file-image', 'aria-label': tr("Открыть {0}", name) }, img);
        img.addEventListener('error', () => failed(node, name), { once: true });
        node.addEventListener('click', () => preview(host, url, name));
      } else if (mime.startsWith('video/') || mime.startsWith('audio/')) {
        node = el(mime.startsWith('video/') ? 'video' : 'audio', { class: 'msg-file-media', src: url, controls: true,
          preload: 'none', 'aria-label': name });
        node.addEventListener('error', () => failed(node, name), { once: true });
      } else {
        node = el('a', { class: 'msg-file-link', href: url, download: name }, icon('save'), tr("Скачать «{0}»", name));
      }
      items.push(el('div', { class: 'msg-file' }, node));
    });
  });
  return items.length ? el('div', { class: 'msg-files' }, ...items) : null;
}
