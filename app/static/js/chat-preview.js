/* Окно HTML и SVG ответа «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 25; сервер — chat_preview.py):
 * у блока кода `html` или `svg` готового ответа — «Открыть» рядом с «Копировать». Нажатие отдаёт текст блока телом
 * POST …/preview (не адресом: адрес пишут журналы) и открывает окно с `iframe sandbox="allow-scripts"` без
 * `allow-same-origin` — ни cookie, ни страниц Студии; документ отдаёт сервер своим CSP без сети. В защищённом диалоге
 * и у блока больше PREVIEW_MAX кнопки нет. */
import { tr } from './i18n.js';
import { api } from './api.js';
import { el, openDialog } from './ui.js';

export const PREVIEW_MAX = 2 * 1024 * 1024;     // как chat_preview.PREVIEW_MAX: байты UTF-8 блока

/** Ссылки фрагмента ответа (`links` — прежние, например resultPathLinks, или null) с кнопками «Открыть».
 * `allowed()` — окно разрешено (ответ перечитан с сервера, диалог без защищённого источника); `chatId()` — диалог;
 * `failed(err)` — отказ сервера. */
export function previewLinks(links, { host, chatId, allowed, failed }) {
  return { ...(links || {}), decorate(fragment) {
    links?.decorate?.(fragment);
    if (!allowed()) return;
    for (const code of fragment.querySelectorAll('pre > code.language-html, pre > code.language-svg')) {
      const text = code.textContent;
      if (new Blob([text]).size > PREVIEW_MAX) continue;
      const kind = code.classList.contains('language-svg') ? 'svg' : 'html';
      const open = el('button', { type: 'button', class: 'preview-open', 'data-kind': kind }, tr("Открыть"));
      open.addEventListener('click', () => show(host, chatId(), kind, text, open, failed));
      const pre = code.parentElement;
      const copy = pre.querySelector(':scope > .copy');
      const tools = el('span', { class: 'pre-tools' }, open);
      if (copy) tools.append(copy);
      pre.append(tools);
    }
  } };
}

/** Кнопки «Открыть» ленты — прочь (сработал защищённый источник). */
export function dropPreviews(root) {
  for (const open of root.querySelectorAll('.preview-open')) open.remove();
}

async function show(host, chatId, kind, text, open, failed) {
  open.disabled = true;
  let token;
  try {
    ({ token } = await api('POST', `/api/chats/${encodeURIComponent(chatId)}/preview`, { kind, text }));
  } catch (err) {
    failed(err);
    return;
  } finally {
    open.disabled = false;
  }
  const frame = el('iframe', { class: 'chat-preview-frame', title: kind === 'svg' ? 'SVG' : 'HTML',
    sandbox: 'allow-scripts', referrerpolicy: 'no-referrer' });
  const dialog = openDialog(host, { id: 'chat-preview-dialog', title: kind === 'svg' ? tr("Окно SVG") : tr("Окно HTML"),
    cancelText: tr("Закрыть"),
    body: [el('p', { class: 'muted small chat-preview-note' }, tr("Окно изолировано: без сети и без доступа к Студии")), frame] });
  dialog.classList.add('chat-preview-dialog');
  dialog.addEventListener('close', () => frame.removeAttribute('src'), { once: true });
  frame.src = `/api/chats/preview/${encodeURIComponent(token)}`;
}
