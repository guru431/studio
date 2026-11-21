import { tr } from './i18n.js';
/* Пауза ответа чата на странице (chat_pending.py, спека 2026-11-21-studio-source-capabilities-design, п. 5 и 17).
 *
 * Сообщение с `pending` (выдача диалога или событие SSE `pending`) — карточка `.msg-pending` под ответом:
 * - `confirm` — «Запустить инструмент?», сервер и инструмент, слова сервера (`question`), аргументы вызова (до 8 пар,
 *   значения обрезаны сервером), кнопки «Запустить» и «Отмена»;
 * - `ask` — вопрос модели, кнопки вариантов, поле «Свой ответ» (если `allow_text`) и «Пропустить».
 * Нажатие зовёт `onAnswer(action, answer)` — чат шлёт POST /api/chats/<id>/pending/<id> и читает ответ сервера в тот
 * же пузырь (`readServer`). Пока ответ уходит, кнопки выключены; отказ — кнопки снова доступны. */
import { button, el } from './ui.js';

/** Карточка паузы `pending` в узле `box`; нет паузы — узел пуст. Та же пауза (`id`) не перерисовывается: набранный
 *  свой ответ не теряется при перерисовке ленты. */
export function paintPending(box, pending, onAnswer) {
  const id = pending ? pending.id : '';
  if (box.dataset.pending === id) return;
  box.dataset.pending = id;
  box.replaceChildren();
  if (!pending) return;
  const card = el('div', { class: 'msg-pending-card', role: 'group', 'data-kind': pending.kind });
  const choose = (action, answer) => async () => {
    const controls = card.querySelectorAll('button, input');
    for (const c of controls) c.disabled = true;
    try {
      await onAnswer(action, answer === undefined ? undefined : answer());
    } finally {
      for (const c of controls) c.disabled = false;
    }
  };
  if (pending.kind === 'confirm' && typeof pending.memory_text === 'string') {
    const title = tr("Запомнить для будущих чатов?");
    card.setAttribute('aria-label', title);
    card.append(el('p', { class: 'msg-pending-title' }, title),
      el('p', { class: 'msg-pending-text' }, pending.memory_text),
      el('div', { class: 'msg-pending-actions' },
        button(tr("Запомнить"), { class: 'btn btn-sm btn-accent pending-run' }, choose('run')),
        button(tr("Отмена"), { class: 'btn btn-sm pending-refuse' }, choose('refuse'))));
  } else if (pending.kind === 'confirm') {
    card.setAttribute('aria-label', tr("Запустить инструмент?"));
    card.append(el('p', { class: 'msg-pending-title' }, tr("Запустить инструмент?")),
      el('p', { class: 'msg-pending-tool' }, `${pending.server || ''} · ${pending.tool || ''}`));
    if (pending.question) card.append(el('p', { class: 'msg-pending-text' }, pending.question));
    const args = Array.isArray(pending.arguments) ? pending.arguments : [];
    if (args.length) {
      card.append(el('dl', { class: 'msg-pending-args' },
        ...args.flatMap(([key, value]) => [el('dt', {}, String(key)), el('dd', {}, String(value))])));
    }
    card.append(el('div', { class: 'msg-pending-actions' },
      button(tr("Запустить"), { class: 'btn btn-sm btn-accent pending-run' }, choose('run')),
      button(tr("Отмена"), { class: 'btn btn-sm pending-refuse' }, choose('refuse'))));
  } else {
    card.setAttribute('aria-label', tr("Вопрос модели"));
    card.append(el('p', { class: 'msg-pending-text' }, pending.question || ''));
    const actions = el('div', { class: 'msg-pending-actions' });
    for (const option of Array.isArray(pending.options) ? pending.options : []) {
      actions.append(button(option, { class: 'btn btn-sm pending-option' }, choose('answer', () => option)));
    }
    card.append(actions);
    if (pending.allow_text) {
      const input = el('input', { class: 'input pending-text', maxlength: '4000', autocomplete: 'off',
        placeholder: tr("Свой ответ"), 'aria-label': tr("Свой ответ") });
      const reply = button(tr("Ответить"), { class: 'btn btn-sm btn-accent pending-reply' }, () => {
        if (!input.value.trim()) { input.focus(); return; }
        choose('answer', () => input.value.trim())();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); reply.click(); }
      });
      card.append(el('div', { class: 'msg-pending-own' }, input, reply));
    }
    actions.append(button(tr("Пропустить"), { class: 'btn btn-sm pending-refuse' }, choose('refuse')));
  }
  box.append(card);
}
