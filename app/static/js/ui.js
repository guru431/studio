import { language, message, tr } from './i18n.js';
import { api } from './api.js';
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

/** Поле PIN компьютера: не password и класть вне <form> — исчезнув после удачного запроса, они дали бы менеджеру
 *  паролей сохранить PIN в синхронизируемое хранилище браузера; точки вместо цифр — `.computer-pin` (app.css). */
export function computerPin(attrs = {}) {
  return el('input', { class: 'input computer-pin', type: 'text', inputmode: 'numeric', autocomplete: 'off',
    autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', maxlength: '64', ...attrs });
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
  const command = el('code', { 'aria-live': 'polite' });
  const copy = button(tr("Копировать"), { class: 'btn', hidden: true }, async () => {
    try {
      await navigator.clipboard.writeText(command.textContent);
      copy.textContent = tr("Скопировано");
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
  const getCommand = button(tr("Получить команду для Linux"), { class: 'btn' }, async () => {
    getCommand.disabled = true;
    copy.hidden = true;
    copy.textContent = tr("Копировать");
    command.textContent = '';
    try {
      const result = await api('POST', '/api/agents/linux-install');
      command.textContent = result.command;
      copy.hidden = false;
    } catch (err) {
      command.textContent = words(err);
    } finally {
      getCommand.disabled = false;
    }
  });
  return el('div', { class: 'agent-download' },
    el('p', { class: 'agent-install-note', role: 'note' },
      tr("Установите мост на компьютер, который хотите подключить к Студии.")),
    el('a', { id: 'agent-download', class: 'btn btn-accent', href: agentInstallers.win32.href, download: agentInstallers.win32.file },
      tr("Скачать мост для Windows (.exe)")),
    el('a', { id: 'agent-download-macos', class: 'btn', href: agentInstallers.darwin.href, download: agentInstallers.darwin.file },
      tr("Скачать мост для macOS (.pkg)")),
    el('a', { id: 'agent-download-linux', class: 'btn', href: agentInstallers.linux.href, download: agentInstallers.linux.file },
      tr("Скачать мост для Linux (.sh)")),
    el('p', { class: 'muted small agent-download-needs' }, tr("Нужны Claude Code, Codex или OpenCode. Для Windows и Linux нужен Python 3.11+, для Linux также venv. Установщик macOS 13+ включает Python.")),
    el('p', { class: 'agent-download-steps' }, tr("В Windows и macOS откройте установщик. В Linux получите команду и выполните её в терминале в течение 10 минут. После установки войдите в Студию через меню моста.")),
    getCommand, el('p', { class: 'agent-install-command' }, command), copy);
}

/** Окно поверх раздела: заголовок, содержимое, «Отмена» (`.dialog-close`) и кнопки действий.
 * Закрылось (Escape, «Отмена», close()) — убирается со страницы. `guard()` — false: «Отмена», Escape и «Назад»
 * Android окно не закрывают (guard сам спрашивает); close() кода закрывает всегда. */
export function openDialog(host, { id, title, body = [], actions = [], cancelText = tr("Отмена"), guard = null }) {
  const close = button(cancelText, { class: 'btn dialog-close' });
  const dialog = el('dialog', { class: 'dialog', id, 'aria-labelledby': `${id}-title` },
    el('h2', { id: `${id}-title` }, title), ...body, el('div', { class: 'actions' }, close, ...actions));
  close.addEventListener('click', () => { if (!guard || guard()) dialog.close(); });
  if (guard) dialog.addEventListener('cancel', (event) => { if (!guard()) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove());
  host.append(dialog);
  dialog.showModal();
  return dialog;
}

/** Окно «Переименовать» записи раздела (расшифровка, скан): в поле — имя без расширения, его сохраняет сервер
 * (documents.rename); save(имя) — Promise ответа, отказ — словами в окне. */
export function renameDialog(host, { id, name, save }) {
  const ext = /\.(?=[0-9a-z]*[a-z])[0-9a-z]{1,8}$/i.exec(name);
  const input = el('input', { class: 'input', name: 'name', maxlength: '200', autocomplete: 'off',
    value: ext && ext.index > 0 ? name.slice(0, ext.index) : name });
  const fail = el('p', { class: 'error' });
  const ok = button(tr("Сохранить"), { class: 'btn btn-accent', id: `${id}-save` });
  const dialog = openDialog(host, { id, title: tr("Переименовать"), body: [field(tr("Название"), input, 'name'), fail],
    actions: [ok] });
  const submit = async () => {
    if (ok.disabled) return;
    ok.disabled = true;
    try {
      await save(input.value);
      dialog.close();
    } catch (err) {
      fail.textContent = words(err);
      ok.disabled = false;
    }
  };
  ok.addEventListener('click', submit);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submit();
    }
  });
  input.focus();
  input.select();
  return dialog;
}
