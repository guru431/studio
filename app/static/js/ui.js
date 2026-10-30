/* Мелочи разметки для разделов: элемент, поле с местом под отказ, своё окно <dialog>.
 *
 * Текст сервера — только через textContent (el() добавляет строки текстовыми узлами), innerHTML
 * с данными нигде. Окно кладётся внутрь раздела: раздел сменился — окна ушли вместе с ним. */

/** Элемент: атрибуты строками (true — пустой атрибут, false/null — нет), дети — узлы и строки. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(key, '');
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

let fields = 0;

/** Подпись, поле и строка отказа `p.error[data-for=key]` под ним. */
export function field(label, input, key) {
  if (!input.id) input.id = `f${++fields}`;
  return el('div', { class: 'field' }, el('label', { for: input.id }, label), input,
    el('p', { class: 'error', 'data-for': key, 'aria-live': 'polite' }));
}

/** Слова отказа с заглавной буквы. */
export function words(err) {
  const text = String((err && err.message) || err || 'Не получилось');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Отказ — под поле `data-for` (по карте код → поле), прочее — в `fallback`. Прежние отказы стираются. */
export function showError(box, err, codes, fallback) {
  for (const p of box.querySelectorAll('.error[data-for]')) p.textContent = '';
  const target = box.querySelector(`.error[data-for="${(codes && codes[err && err.code]) || fallback}"]`);
  if (target) target.textContent = words(err);
}

/** Форма слова для числа: forms — [1 картинку, 2 картинки, 5 картинок]; 11–14 — третья. */
export function plural(n, [one, few, many]) {
  const tens = n % 100;
  const units = n % 10;
  if (tens >= 11 && tens <= 14) return many;
  if (units === 1) return one;
  return units >= 2 && units <= 4 ? few : many;
}

/** Кнопка; `onClick` получает событие. */
export function button(text, attrs = {}, onClick) {
  const node = el('button', { type: 'button', class: 'btn', ...attrs }, text);
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

/** «Скачать мост для Windows» (спека 2026-10-28-studio-agent-download-and-window-design, 4.2): ссылка на ZIP моста
 * Claude Code, что нужно и шаги установки. Один источник — «Claude Code» без компьютеров и «Настройки» → «Прочее». */
export function agentDownload() {
  return el('div', { class: 'agent-download' },
    el('a', { id: 'agent-download', class: 'btn btn-accent', href: '/api/agent/download', download: true },
      'Скачать мост для Windows'),
    el('p', { class: 'muted small agent-download-needs' }, 'ZIP, нужны Python 3.11+ и Claude Code, Codex или OpenCode'),
    el('p', { class: 'agent-download-steps' }, 'Распакуйте архив и запустите install.cmd. В окне моста войдите логином'
      + ' и паролем студии — компьютер появится здесь.'));
}

/** Окно поверх раздела: заголовок, содержимое, «Отмена» (`.dialog-close`) и кнопки действий.
 * Закрылось (Escape, «Отмена», close()) — убирается со страницы. */
export function openDialog(host, { id, title, body = [], actions = [], cancelText = 'Отмена' }) {
  const close = button(cancelText, { class: 'btn dialog-close' });
  const dialog = el('dialog', { class: 'dialog', id, 'aria-labelledby': `${id}-title` },
    el('h2', { id: `${id}-title` }, title), ...body, el('div', { class: 'actions' }, close, ...actions));
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  host.append(dialog);
  dialog.showModal();
  return dialog;
}
