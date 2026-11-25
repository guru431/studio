import { tr } from './i18n.js';
/* Отметка хранимого пересказа в ленте «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 30): ранняя часть
 * диалога ушла модели пересказом (`chats.summary`), а в ленте осталась целиком. Сразу за последним пересказанным
 * сообщением (`summary.upto` из GET /api/chats/<id>) — строка «Ранняя часть свёрнута в пересказ» и «Показать»:
 * GET /api/chats/<id>/summary → { text } в окне. Пересказа нет или его границы нет в ленте — отметки нет. */
import { api } from './api.js';
import { button, el, openDialog, words } from './ui.js';

let mark = null;              // { key: «диалог:upto», node }: перерисовка ленты узел не пересоздаёт

function markNode(chatId) {
  const fail = el('span', { class: 'error' });
  const show = button(tr("Показать"), { class: 'btn btn-sm msg-summary-show' }, async () => {
    show.disabled = true;
    fail.textContent = '';
    try {
      const { text } = await api('GET', `/api/chats/${chatId}/summary`);
      openDialog(document.body, { id: 'chat-summary-dialog', title: tr("Пересказ ранней части"), cancelText: tr("Закрыть"),
        body: [el('pre', { class: 'att-text summary-text' }, text)] });
    } catch (err) {
      fail.textContent = words(err);
    }
    show.disabled = false;
  });
  return el('div', { class: 'msg-summary' }, el('span', {}, tr("Ранняя часть свёрнута в пересказ")), show, fail);
}

/** Узлы ленты `nodes` (по одному на сообщение `msgs`) с отметкой пересказа диалога `data` ({ chat, summary }). */
export function placeSummary(nodes, msgs, data) {
  const upto = data?.summary?.upto;
  const at = upto ? msgs.findIndex((m) => m.id === upto) : -1;
  if (at < 0) return nodes;
  const key = `${data.chat.id}:${upto}`;
  if (mark?.key !== key) mark = { key, node: markNode(data.chat.id) };
  return [...nodes.slice(0, at + 1), mark.node, ...nodes.slice(at + 1)];
}
