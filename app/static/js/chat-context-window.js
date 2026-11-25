import { locale, tr } from './i18n.js';
/* Окно «Диалог и контекст» из кольца контекста (спека 2026-11-25-studio-skills-status-local-models, п. 13): модель,
 * подключение, роль, число сообщений; занято и предел (оценка `/estimate`); токены последнего ответа и всего диалога
 * (ввод, вывод, из кэша) — из `usage` сообщений; время и скорость последнего ответа. «Сжать сейчас» — POST
 * …/compact (п. 30), «Новый диалог с итогом» — POST …/continue (п. 27) и открытие нового диалога с итогом в поле. */
import { api } from './api.js';
import { button, el, openDialog, words } from './ui.js';
import { duration } from './code-context.js';
import { speedOf } from './chat-usage.js';

const NO_DATA = () => tr("нет данных");
const number = (value) => (Number.isFinite(value) ? value.toLocaleString(locale()) : NO_DATA());

/** `dialog()` — открытый диалог `{chat, messages}` или null; `connection()` — имя подключения; `estimate()` —
 * Promise `{tokens, limit}` или null; `busy()` — ответ идёт; `openWith(id, text)` — открыть диалог с текстом в поле. */
export function contextWindow(host, { dialog, connection, estimate, busy, openWith }) {
  function open() {
    const data = dialog();
    if (!data) return null;
    const id = data.chat.id;
    const answers = data.messages.filter((m) => m.role === 'assistant');
    const last = answers.at(-1) || null;
    const total = (key) => {
      const values = answers.map((m) => m.usage?.[key]).filter(Number.isFinite);
      return values.length ? values.reduce((sum, value) => sum + value, 0) : null;
    };
    const speed = speedOf(last);
    const cells = {};
    const row = (name, label, value) => {
      cells[name] = el('dd', { 'data-row': name }, value);
      return [el('dt', {}, label), cells[name]];
    };
    const summary = el('dl', { class: 'code-usage-summary chat-context-summary' },
      ...row('model', tr("Модель"), data.chat.model || '—'),
      ...row('connection', tr("Подключение"), connection() || '—'),
      ...row('role', tr("Роль"), data.chat.role?.name || '—'),
      ...row('messages', tr("Сообщений"), number(data.messages.length)),
      ...row('used', tr("Занято токенов"), '…'),
      ...row('limit', tr("Предел контекста"), '…'),
      ...row('last_in', tr("Последний ответ: ввод"), number(last?.usage?.in)),
      ...row('last_out', tr("Последний ответ: вывод"), number(last?.usage?.out)),
      ...row('last_cached', tr("Последний ответ: из кэша"), number(last?.usage?.cached)),
      ...row('total_in', tr("Диалог: ввод"), number(total('in'))),
      ...row('total_out', tr("Диалог: вывод"), number(total('out'))),
      ...row('total_cached', tr("Диалог: из кэша"), number(total('cached'))),
      ...row('time', tr("Время последнего ответа"), Number.isFinite(last?.total_ms) ? duration(last.total_ms) : NO_DATA()),
      ...row('speed', tr("Скорость последнего ответа"),
        speed === null ? NO_DATA() : tr("{0} ток/с", speed.toLocaleString(locale()))));
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const compact = button(tr("Сжать сейчас"), { class: 'btn', id: 'chat-context-compact', disabled: busy() || null });
    const fresh = button(tr("Новый диалог с итогом"), { class: 'btn', id: 'chat-context-continue', disabled: busy() || null });
    const box = openDialog(host, { id: 'chat-context-dialog', title: tr("Диалог и контекст"), cancelText: tr("Закрыть"),
      body: [summary, el('p', { class: 'muted small' }, tr("Занято — оценка Студии для следующего сообщения. Токены и время сообщает сервис модели; чего он не сообщил — «нет данных».")),
        fail], actions: [compact, fresh] });
    box.classList.add('code-usage-dialog');
    const paint = (out) => {
      cells.used.textContent = number(out?.tokens);
      cells.limit.textContent = Number.isFinite(out?.limit) && out.limit > 0 ? number(out.limit) : NO_DATA();
    };
    Promise.resolve().then(estimate).then(paint, () => paint(null));
    compact.addEventListener('click', async () => {
      compact.disabled = fresh.disabled = true;
      fail.textContent = '';
      try {
        const out = await api('POST', `/api/chats/${encodeURIComponent(id)}/compact`);
        cells.used.textContent = number(out.tokens);
        if (Number.isFinite(out.limit) && out.limit > 0) cells.limit.textContent = number(out.limit);
      } catch (err) {
        fail.textContent = words(err);
      } finally {
        compact.disabled = fresh.disabled = busy();
      }
    });
    fresh.addEventListener('click', async () => {
      compact.disabled = fresh.disabled = true;
      fail.textContent = '';
      try {
        const out = await api('POST', `/api/chats/${encodeURIComponent(id)}/continue`);
        box.close();
        openWith(out.chat_id, out.text || '');
      } catch (err) {
        fail.textContent = words(err);
        compact.disabled = fresh.disabled = busy();
      }
    });
    return box;
  }
  return { open };
}
