/* «Новый диалог с итогом» у отказа по длине (спека 2026-11-25-studio-skills-status-local-models, п. 27; сервер —
 * chat_continue.py): ход не поместился в контекст модели (413 `context`) или ответ кончился ошибкой длины — рядом
 * кнопка, что пишет итог всего диалога (POST …/continue) и открывает наследника с итогом в поле ввода. Кнопка окна
 * «Диалог и контекст» (#chat-context-continue) — в chat-context-window.js. */
import { tr } from './i18n.js';
import { api } from './api.js';
import { button, el } from './ui.js';

// копия chat.CONTEXT_WORDS (сверяет tests/test_chat_continue.py): начало слов сервера об отказе по длине
const CONTEXT_WORDS = 'Не помещается в контекст модели';

/** Отказ хода по длине (413 `context`). */
export const lengthRefusal = (err) => err?.code === 'context';

/** Ответ кончился ошибкой длины: слова сервера (`m.error`, до перевода) начинаются с CONTEXT_WORDS. */
export const lengthError = (m) => m?.status === 'error' && typeof m.error === 'string' && m.error.startsWith(CONTEXT_WORDS);

/** `chatId()` — открытый диалог; `openWith(id, text)` — открыть наследника с итогом в поле; `busy()` — ответ идёт;
 * `failed(err)` — отказ. Ответ — { node, show(), hide(), button() }: node — строка под #chat-error, button() — кнопка
 * для действий ответа с ошибкой длины. */
export function continueOffer({ chatId, openWith, busy, failed }) {
  const label = () => tr("Новый диалог с итогом");
  const buttons = new Set();

  async function run() {
    const id = chatId();
    if (!id || busy()) return;
    for (const b of [...buttons]) {
      if (b !== main && !b.isConnected) {          // ответ перерисован — его прежней кнопки больше нет
        buttons.delete(b);
        continue;
      }
      b.disabled = true;
      b.textContent = tr("Пишу итог…");
    }
    try {
      const out = await api('POST', `/api/chats/${encodeURIComponent(id)}/continue`);
      hide();
      openWith(out.chat_id, out.text || '');
    } catch (err) {
      failed(err);
    } finally {
      for (const b of buttons) {
        b.disabled = busy();
        b.textContent = label();
      }
    }
  }

  const main = button(label(), { class: 'btn btn-sm', id: 'chat-continue' }, run);
  buttons.add(main);
  const node = el('p', { class: 'chat-continue', id: 'chat-continue-offer', hidden: true }, main);

  function show() {
    main.disabled = busy();
    node.hidden = false;
  }

  function hide() {
    node.hidden = true;
  }

  return { node, show, hide, button() {
    const b = button(label(), { class: 'btn btn-sm msg-continue', disabled: busy() || null }, run);
    buttons.add(b);
    return b;
  } };
}
