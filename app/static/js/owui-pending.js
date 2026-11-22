import { tr } from './i18n.js';
/* Пауза ответа Open WebUI на странице (спека 2026-11-22-studio-open-webui-design, «Пауза»; образец — chat-pending.js).
 *
 * `pending` сообщения из вида чата (owui_view.branch) — карточка `.msg-pending-card` под ответом:
 * - `question` — вопросы модели (`ask_user`, 1–3): у каждого заголовок, текст, варианты кнопками (подпись и пояснение,
 *   выбранный — aria-pressed) и поле своего текста, если вопрос его разрешает (`allow_other`); «Ответить» — когда у
 *   каждого вопроса есть ответ, «Отклонить» — всегда;
 * - `approval` — подтверждение инструмента: имя, аргументы свёрнуто, «Разрешить» / «Отклонить».
 * Нажатие зовёт `onResolve({call_id, action, answers?})` — лента шлёт POST …/resolve. Пока решение уходит, кнопки
 * выключены; отказ — его слова в карточке и кнопки снова доступны. Срок вопроса Студия не отсчитывает: вопрос ждёт.
 * Решили в другом окне — лента перечитывает чат, пауза пропадает, и карточка уходит (`pending` = null).
 * Вопросы, варианты и аргументы — текст модели: только textContent и без перевода. */
import { button, el, words } from './ui.js';

/** Свой ответ на вопрос — до 4000 знаков (owui.OtherAnswer). */
const OWN_MAX = 4000;
function questionCard(card, pending, choose) {
  const questions = Array.isArray(pending.questions) ? pending.questions : [];
  const answers = new Map();             // id вопроса → ответ в форме Open WebUI
  const needed = new Set(questions.map((q) => q.id)).size;   // одинаковые id — один ответ (ключ формы Open WebUI)
  card.setAttribute('aria-label', tr("Вопрос модели"));
  const reply = button(tr("Ответить"), { class: 'btn btn-sm btn-accent pending-reply' }, () => {
    if (answers.size >= needed) choose('answer', Object.fromEntries(answers))();
  });
  const ready = () => { reply.disabled = answers.size < needed; };
  for (const q of questions) {
    const box = el('div', { class: 'owui-question', role: 'group', 'aria-label': q.header || q.question || '' });
    if (q.header) box.append(el('p', { class: 'msg-pending-title' }, q.header));
    box.append(el('p', { class: 'msg-pending-text' }, q.question || ''));
    const options = el('div', { class: 'owui-options' });
    const own = q.allow_other ? el('input', { class: 'input pending-text', maxlength: String(OWN_MAX), autocomplete: 'off',
      placeholder: tr("Свой ответ"), 'aria-label': tr("Свой ответ") }) : null;
    const mark = () => {
      const chosen = answers.get(q.id);
      for (const node of options.children) {
        node.setAttribute('aria-pressed', String(chosen?.type === 'option' && chosen.option_index === Number(node.dataset.index)));
      }
    };
    (Array.isArray(q.options) ? q.options : []).forEach((option, index) => {
      const node = el('button', { type: 'button', class: 'btn btn-sm pending-option owui-option', 'aria-pressed': 'false',
        'data-index': String(index) }, el('span', { class: 'owui-option-label' }, option.label || ''),
      option.description ? el('span', { class: 'owui-option-text' }, option.description) : null);
      node.addEventListener('click', () => {
        answers.set(q.id, { type: 'option', option_index: index });
        if (own) own.value = '';
        mark();
        ready();
      });
      options.append(node);
    });
    box.append(options);
    if (own) {
      own.addEventListener('input', () => {
        const text = own.value.trim();
        if (text) answers.set(q.id, { type: 'other', text });
        else if (answers.get(q.id)?.type === 'other') answers.delete(q.id);
        mark();
        ready();
      });
      own.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); reply.click(); }
      });
      box.append(el('div', { class: 'msg-pending-own' }, own));
    }
    card.append(box);
  }
  ready();
  card.append(el('div', { class: 'msg-pending-actions' }, reply,
    button(tr("Отклонить"), { class: 'btn btn-sm pending-refuse' }, choose('reject'))));
}

function approvalCard(card, pending, choose) {
  card.setAttribute('aria-label', tr("Разрешить инструмент?"));
  card.append(el('p', { class: 'msg-pending-title' }, tr("Разрешить инструмент?")),
    el('p', { class: 'msg-pending-tool' }, pending.name || ''));
  if (pending.arguments) {
    card.append(el('details', { class: 'owui-args' }, el('summary', {}, tr("Аргументы")),
      el('pre', { class: 'owui-args-text' }, pending.arguments)));
  }
  card.append(el('div', { class: 'msg-pending-actions' },
    button(tr("Разрешить"), { class: 'btn btn-sm btn-accent pending-run' }, choose('approve')),
    button(tr("Отклонить"), { class: 'btn btn-sm pending-refuse' }, choose('reject'))));
}

/** Карточка паузы в узле `box`; нет паузы — узел пуст. Та же пауза (`call_id`) не перерисовывается: выбранные
 *  варианты и набранный свой ответ не теряются при перерисовке ленты. */
export function paintOwuiPending(box, pending, onResolve) {
  const id = pending ? `${pending.kind}:${pending.call_id}` : '';
  if (box.dataset.pending === id) return;
  box.dataset.pending = id;
  box.replaceChildren();
  if (!pending) return;
  const card = el('div', { class: 'msg-pending-card owui-pending', role: 'group', 'data-kind': pending.kind });
  const fail = el('p', { class: 'error owui-pending-error', 'aria-live': 'polite' });
  const choose = (action, answers) => async () => {
    const controls = [...card.querySelectorAll('button, input')];
    const was = controls.map((c) => c.disabled);
    for (const c of controls) c.disabled = true;
    fail.textContent = '';
    try {
      await onResolve({ call_id: pending.call_id, action, ...(answers ? { answers } : {}) });
    } catch (err) {
      fail.textContent = words(err);
      controls.forEach((c, n) => { c.disabled = was[n]; });
    }
  };
  if (pending.kind === 'question') questionCard(card, pending, choose);
  else approvalCard(card, pending, choose);
  card.append(fail);
  box.append(card);
}
