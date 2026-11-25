/* Источники «Знаний» под ответом «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 28; сервер —
 * chat_sources.py): шаг `knowledge` — вызов `knowledge_search` («Знания: «запрос»») или автоматический блок
 * диалога (`auto`, «Знания диалога») — называет найденные куски «документ · кусок n» (`hits`). Нажатие читает текст
 * куска (GET …/sources/<шаг>/<k>) и показывает его в окне. Шаг без `hits` (прежний) — строка имён документов. */
import { tr } from './i18n.js';
import { api } from './api.js';
import { el, openDialog, words } from './ui.js';

const stepOf = new WeakMap();          // кнопка куска → её шаг (номер шага ищется в `m.tools` при нажатии)

/** Строка шага знаний в `li`; `buttons` — куски кнопками (под ответом; в меню строки состояния — именами). */
export function knowledgeStep(li, s, { buttons = false } = {}) {
  li.append(s.auto ? tr("Знания диалога") : s.query ? tr("Знания: «{0}»", s.query) : tr("Знания"));
  const hits = Array.isArray(s.hits) ? s.hits.filter((h) => h && typeof h.doc === 'string') : [];
  if (buttons && hits.length) {
    li.append(el('ul', { class: 'kb-sources' }, ...hits.map((h, k) => {
      const open = el('button', { type: 'button', class: 'btn-link kb-source', 'data-k': String(k) },
        tr("{0} · кусок {1}", h.doc, h.n));
      stepOf.set(open, s);
      return el('li', {}, open);
    })));
  } else if (Array.isArray(s.sources) && s.sources.length) {
    li.append(el('span', { class: 'muted' }, ` · ${s.sources.join(', ')}`));
  }
}

/** Нажатия по кускам списка шагов ответа `message` диалога `chatId`: окно #kb-source-dialog в `host`. */
export function sourceClicks(list, { chatId, message, host }) {
  list?.addEventListener('click', async (event) => {
    const open = event.target.closest('.kb-source');
    const s = open && stepOf.get(open);
    if (!s || !message.id) return;
    const step = (message.tools || []).indexOf(s);
    if (step < 0) return;
    open.disabled = true;
    let body;
    try {
      const got = await api('GET', `/api/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(message.id)}`
        + `/sources/${step}/${open.dataset.k}`);
      body = [el('pre', { class: 'kb-source-text' }, got.text)];
    } catch (err) {
      body = [el('p', { class: 'error' }, words(err))];
    } finally {
      open.disabled = false;
    }
    openDialog(host, { id: 'kb-source-dialog', title: open.textContent, cancelText: tr("Закрыть"), body });
  });
}
