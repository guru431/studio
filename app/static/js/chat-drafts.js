/* Черновик на каждый диалог «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 21): текст поля — в
 * хранилище сессии вкладки по id диалога (dialog-drafts.js, ключ `studio-chat-drafts`), у нового диалога — `'new'`.
 * Вложения — только в памяти страницы (поле общее для диалогов). Блокировка и выход стирают черновики сами
 * (dialog-drafts.js). Квитанция `receipt` (id сообщения очереди, ушло ли которое — неизвестно, chat-queue.js) живёт,
 * пока текст черновика тот же: такой текст уходит тем же `request_id`. До отпирания (с <html> снят `shrouded`,
 * lock.js) ввод и уход из диалога черновик не пишут — как очередь (chat-queue.js); поле на экране остаётся. */
import { CHAT_DRAFTS, draftStore } from './dialog-drafts.js';

const store = draftStore(CHAT_DRAFTS);
const keyOf = (id) => id ?? 'new';
let locked = false;
document.addEventListener('studio-locked', () => { locked = true; });
new MutationObserver(() => {
  if (!document.documentElement.classList.contains('shrouded')) locked = false;
}).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });

export function chatDrafts(input, { autosize }) {
  const save = (id) => {
    if (locked) return;
    const text = input.value.trim() ? input.value : '';
    const old = store.saved()[keyOf(id)];
    store.save(keyOf(id), text, old?.receipt && old.text === text ? old.receipt : undefined);
  };
  return {
    /** Черновик диалога `id` (или ''). */
    text(id) {
      return store.saved()[keyOf(id)]?.text || '';
    },
    /** Открыт диалог `id`: в поле — его черновик (или пусто). */
    enter(id) {
      input.value = store.saved()[keyOf(id)]?.text || '';
      autosize();
    },
    /** Уходим из диалога `id`: текст поля — его черновик; пустое поле черновик удаляет. */
    leave: save,
    /** Человек набрал текст в поле диалога `id`. */
    typed: save,
    /** `request_id` для текста `text` диалога `id`: квитанция черновика, если текст — тот же, иначе null. */
    receipt(id, text) {
      const saved = store.saved()[keyOf(id)];
      return saved?.receipt && saved.text === text ? saved.receipt : null;
    },
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
