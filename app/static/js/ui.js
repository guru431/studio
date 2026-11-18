import { language, message, tr } from './i18n.js';
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

/** Фокус ввода на компьютере после снятия шторки раздела, без прокрутки и клавиатуры телефона. */
export function focusDesktop(input) {
  if (!matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  const active = document.activeElement;
  requestAnimationFrame(() => {
    if (!input.isConnected || input.closest('[inert], [hidden]') || !input.getClientRects().length
        || getComputedStyle(input).visibility !== 'visible' || document.activeElement !== active
        || document.querySelector('dialog[open]')) return;
    input.focus({ preventScroll: true });
  });
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
  const text = message((err && err.message) || err || tr("Не получилось"));
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
  if (language() === 'en') return n === 1 ? one : many;
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

export const agentInstallers = {
  win32: { name: 'Windows', icon: 'windows', href: '/api/agent/download', file: 'studio-agent-setup.exe' },
  darwin: { name: 'macOS', icon: 'apple', href: '/api/agent/download?platform=macos', file: 'studio-agent-setup.pkg' },
  linux: { name: 'Linux', icon: 'linux', href: '/api/agent/download?platform=linux', file: 'studio-agent-setup.sh' },
};

export function agentDownload() {
  return el('div', { class: 'agent-download' },
    el('p', { class: 'agent-install-note', role: 'note' },
      tr("Установите агент на компьютер, который хотите подключить к Студии.")),
    el('a', { id: 'agent-download', class: 'btn btn-accent', href: agentInstallers.win32.href, download: agentInstallers.win32.file },
      tr("Скачать агент для Windows (.exe)")),
    el('a', { id: 'agent-download-macos', class: 'btn', href: agentInstallers.darwin.href, download: agentInstallers.darwin.file },
      tr("Скачать агент для macOS (.pkg)")),
    el('a', { id: 'agent-download-linux', class: 'btn', href: agentInstallers.linux.href, download: agentInstallers.linux.file },
      tr("Скачать агент для Linux (.sh)")),
    el('p', { class: 'muted small agent-download-needs' }, tr("Нужны Claude Code, Codex или OpenCode. Для Windows и Linux нужен Python 3.11+, для Linux также venv. Установщик macOS 13+ включает Python.")),
    el('p', { class: 'agent-download-steps' }, tr("В Windows и macOS откройте установщик. В Linux выполните команду в каталоге загрузки и войдите в Студию через меню агента."),
      el('br'), el('code', {}, 'sh ./studio-agent-setup.sh')));
}

/** Окно поверх раздела: заголовок, содержимое, «Отмена» (`.dialog-close`) и кнопки действий.
 * Закрылось (Escape, «Отмена», close()) — убирается со страницы. */
export function openDialog(host, { id, title, body = [], actions = [], cancelText = tr("Отмена") }) {
  const close = button(cancelText, { class: 'btn dialog-close' });
  const dialog = el('dialog', { class: 'dialog', id, 'aria-labelledby': `${id}-title` },
    el('h2', { id: `${id}-title` }, title), ...body, el('div', { class: 'actions' }, close, ...actions));
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());
  host.append(dialog);
  dialog.showModal();
  return dialog;
}
