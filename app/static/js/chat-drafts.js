/* Черновик на каждый диалог «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 21): текст поля — в
 * хранилище сессии вкладки по id диалога (dialog-drafts.js, ключ `studio-chat-drafts`), у нового диалога — `'new'`.
 * Вложения — только в памяти страницы (поле общее для диалогов). Блокировка и выход стирают черновики сами
 * (dialog-drafts.js). */
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';

const store = draftStore(CHAT_DRAFTS);
const keyOf = (id) => id ?? 'new';

export function chatDrafts(input, { autosize }) {
  const save = (id) => store.save(keyOf(id), input.value.trim() ? input.value : '');
  return {
    /** Открыт диалог `id`: в поле — его черновик (или пусто). */
    enter(id) {
      input.value = store.saved()[keyOf(id)]?.text || '';
      autosize();
    },
    /** Уходим из диалога `id`: текст поля — его черновик; пустое поле черновик удаляет. */
    leave: save,
    /** Человек набрал текст в поле диалога `id`. */
    typed: save,
    /** Текст `text` ушёл в диалог `id`: черновик с тем же текстом больше не нужен (набранное заново — остаётся). */
    sent(id, text) {
      if (store.saved()[keyOf(id)]?.text === text) store.save(keyOf(id), '');
    },
    /** Диалог сменил id (`null` ↔ созданный): черновик переходит к нему. */
    rename(from, to) {
      const text = store.saved()[keyOf(from)]?.text;
      if (!text) return;
      store.save(keyOf(from), '');
      store.save(keyOf(to), text);
    },
  };
}
