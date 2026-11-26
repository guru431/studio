/* Черновик на каждый диалог «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 21): текст поля — в
 * хранилище сессии вкладки по id диалога (dialog-drafts.js, ключ `studio-chat-drafts`), у нового диалога — `'new'`.
 * Вложения — только в памяти страницы (поле общее для диалогов). Блокировка и выход стирают черновики сами
 * (dialog-drafts.js). Квитанция `receipt` (id сообщения очереди, ушло ли которое — неизвестно, chat-queue.js) живёт,
 * пока текст черновика тот же: такой текст уходит тем же `request_id`. До отпирания (с <html> снят `shrouded`,
 * lock.js) ввод и уход из диалога черновик не пишут — как очередь (chat-queue.js); поле на экране остаётся. */
import { CHAT_DRAFTS, draftStore, draftsLocked } from './dialog-drafts.js';

const store = draftStore(CHAT_DRAFTS);
export function chatDrafts(input, { autosize, skills, project = () => null }) {
  const keyOf = (id) => id ?? (project() ? `new-${project()}` : 'new');
  const save = (id) => {
    if (draftsLocked) return;
    const text = input.value.trim() ? input.value : '';
    const old = store.saved()[keyOf(id)];
    const chosen = skills.chosen();
    const skill = chosen?.id ? { id: chosen.id, name: chosen.name, parts: !!chosen.parts } : null;
    store.save(keyOf(id), text, old?.text === text ? old.receipt : undefined, skill);
  };
  return {
    /** Черновик диалога `id` (или ''). */
    text(id) {
      return store.saved()[keyOf(id)]?.text || '';
    },
    /** Открыт диалог `id`: в поле — его черновик (или пусто). */
    enter(id) {
      const draft = store.saved()[keyOf(id)];
      input.value = draft?.text || '';
      const skill = draft?.skill;
      skills.restore(typeof skill?.id === 'string' && typeof skill.name === 'string' ? skill : null);
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
    sent(id, text, skill) {
      const draft = store.saved()[keyOf(id)];
      if (draft?.text === text && draft.skill?.id === skill?.id) store.save(keyOf(id), '');
    },
    /** Диалог сменил id (`null` ↔ созданный): черновик переходит к нему. */
    rename(from, to) {
      const draft = store.saved()[keyOf(from)];
      if (!draft) return;
      store.save(keyOf(from), '');
      store.save(keyOf(to), draft.text, draft.receipt, draft.skill);
    },
  };
}
