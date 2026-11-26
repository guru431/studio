/* Картинки результатов инструментов MCP под ответом «Чата» (спека 2026-11-25-studio-skills-status-local-models,
 * п. 39): сервер кладёт их вложениями ответа (`attachments`, `kind: "image"`, имя `<инструмент>-<n>.png`); здесь —
 * миниатюры, нажатие открывает фото окном раздела. */
import { tr } from './i18n.js';
import { el } from './ui.js';

/** Миниатюра изображения с сохранённым медиа, окно открывает `show(a)`. */
export function imageThumb(a, show) {
  const thumb = el('button', { type: 'button', class: 'att-thumb', 'aria-label': tr("Открыть фото {0}", a.name) },
    el('img', { src: `/api/media/${encodeURIComponent(a.media_id)}?thumb=1`, alt: '', loading: 'lazy', decoding: 'async' }));
  thumb.addEventListener('click', () => show(a));
  return thumb;
}

/** Миниатюры картинок ответа `m` (`div.msg-pictures`) или null; `show(a)` — окно фото вложения. */
export function resultImages(m, show) {
  const pictures = (m.attachments || []).filter((a) => a && a.kind === 'image' && a.media_id);
  if (!pictures.length) return null;
  return el('div', { class: 'msg-pictures', role: 'group', 'aria-label': tr("Картинки результата") },
    ...pictures.map((a) => imageThumb(a, show)));
}
