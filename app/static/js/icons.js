/* Один набор значков студии: SVG 24×24 с обводкой currentColor (класс .ico в app.css). Значки разделов — по id
 * раздела (их берёт реестр sections-meta.js), остальные — по смыслу. Разметка значков — только константы этого
 * модуля: в innerHTML не попадает ничего снаружи. Текстовые символы («•••», «⋯», «↻», «ⓘ», «×») кнопками не
 * служат — у кнопки значок отсюда и слова в aria-label. */
import { el } from './ui.js';

export const ICONS = {
  // разделы
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  code: '<path d="M4 6l5 5-5 5M11 18h9"/>',
  photo: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>',
  transcribe: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
  speech: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
  ocr: '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/>'
    + '<path d="M8 9h8M8 12h8M8 15h5"/>',
  translate: '<path d="M4 7h7M7.5 5v2M5 15c2-2 4-5 4.5-8M6 11c1 2 3 3 5 4"/><path d="M13 20l3.5-8 3.5 8M14.2 17.5h4.6"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  jobs: '<path d="M4 7h16M4 12h10M4 17h7"/><path d="M15.5 16.5l2 2 3.5-4"/>',
  // оболочка и общие кнопки
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  power: '<path d="M12 3v9"/><path d="M6.3 7a8 8 0 1 0 11.4 0"/>',
  prev: '<path d="M15 6l-6 6 6 6"/>',
  next: '<path d="M9 6l6 6-6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  star: '<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>',
  backspace: '<path d="M9 5h11v14H9l-6-7z"/><path d="M12 9l5 6M17 9l-5 6"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="M6 12.5l4 4 8-9"/>',
  pick: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
    + 'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  // поле ввода и записи
  clip: '<path d="M20 11.5l-8.2 8.2a5 5 0 01-7.1-7.1l8.6-8.6a3.4 3.4 0 014.8 4.8l-8.4 8.4a1.8 1.8 0 01-2.6-2.6l7.6-7.6"/>',
  send: '<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  save: '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
  share: '<path d="M8 9H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-2M12 15V3m-4 4 4-4 4 4"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/>',
  bulb: '<path d="M9 18h6M10 21h4"/>'
    + '<path d="M12 3a6 6 0 00-3.5 10.9c.6.4 1 1.1 1 1.8V16h5v-.3c0-.7.4-1.4 1-1.8A6 6 0 0012 3z"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  doc: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/>',
  text: '<path d="M5 6h14M5 12h14M5 18h9"/>',
  camera: '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
  swap: '<path d="M5 8h14l-3-3M19 16H5l3 3"/>',
  folder: '<path d="M3 6.5h6l2 2h10V19H3z"/>',
  file: '<path d="M6 3h8l4 4v14H6zM14 3v4h4"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/>'
    + '<path d="M6 7v10M18 7v3a4 4 0 0 1-4 4h-4a4 4 0 0 0-4 4"/>',
  // вид ленты (views.js)
  preview: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 16l5-5 4 4 3-3 6 6"/>',
  tiles: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
    + '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  table: '<path d="M10 6h10M10 12h10M10 18h10"/><rect x="3.5" y="4.5" width="3" height="3" rx=".8"/>'
    + '<rect x="3.5" y="10.5" width="3" height="3" rx=".8"/><rect x="3.5" y="16.5" width="3" height="3" rx=".8"/>',
};

/** Разметка значка строкой (для innerHTML кнопки рядом с текстом); неизвестное имя — пустой значок. */
export function svg(name, cls = 'ico') {
  return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ''}</svg>`;   // константы выше
}

/** Узел значка <svg>. */
export function icon(name, cls = 'ico') {
  const box = document.createElement('template');
  box.innerHTML = svg(name, cls);
  return box.content.firstChild;
}

/** Кнопка-значок: слова — в aria-label и подсказке. */
export function iconButton(name, label, attrs = {}) {
  return el('button', { type: 'button', class: 'icon-btn', 'aria-label': label, title: label, ...attrs }, icon(name));
}
