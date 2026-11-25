import { tr } from './i18n.js';
/* Под ответом «Агентов» (спека 2026-11-25, п. 10): «Копировать» — весь текст ответа разметкой в буфер, и модель,
 * когда помощник её сообщает. Запросов модуль не делает; «Прочитать» здесь нет — текст агентского чата в базу
 * (запись «Озвучки») не пишется. */
import { el } from './ui.js';

const COPIED_MS = 1500;

/** Ряд `.code-msg-actions` и `p.msg-model` в конце узла ответа; повторный вызов заменяет прежние. */
export function answerTools(node, { text, model }) {
  for (const old of node.querySelectorAll(':scope > .code-msg-actions, :scope > .msg-model')) old.remove();
  if (typeof model === 'string' && model) node.append(el('p', { class: 'msg-model muted small' }, model));
  if (typeof text !== 'string' || !text.trim()) return;
  const copy = el('button', { type: 'button', class: 'btn btn-sm msg-copy' }, tr("Копировать"));
  let timer = null;
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = tr("Скопировано");
    } catch {
      copy.textContent = tr("Не вышло");
    }
    clearTimeout(timer);
    timer = setTimeout(() => { copy.textContent = tr("Копировать"); }, COPIED_MS);
  });
  node.append(el('div', { class: 'msg-actions code-msg-actions' }, copy));
}
