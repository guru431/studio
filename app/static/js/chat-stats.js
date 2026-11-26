import { locale, tr } from './i18n.js';
/* «Статистика» учётки (спека 2026-11-25-studio-skills-status-local-models, п. 14): ответы, токены ввода и вывода и
 * время генерации по дням (пояс учётки) и моделям за chat_info.STATS_DAYS дней — GET /api/chats/stats. Окно — из
 * строки состояния «Чата» и из «Настроек → Чат». Чисел нет ни у одного ответа группы — «—». */
import { api } from './api.js';
import { button, el, openDialog, plural, words } from './ui.js';
import { duration } from './time.js';

const DAYS = 31;                 // как chat_info.STATS_DAYS

const number = (value) => (Number.isFinite(value) ? value.toLocaleString(locale()) : '—');
// окно словами с верной формой: «31 день», «31 days»
const period = () => `${DAYS} ${plural(DAYS, [tr("день"), tr("дня"), tr("дней")])}`;

/** Окно статистики за DAYS дней (`period`). */
export async function statsDialog(host) {
  const note = el('p', { class: 'muted small', role: 'status' }, tr("Загружаю…"));
  const body = el('tbody');
  const table = el('table', { class: 'code-usage-days chat-stats-table', hidden: true },
    el('thead', {}, el('tr', {}, ...[tr("День"), tr("Модель"), tr("Ответы"), tr("Ввод"), tr("Вывод"), tr("Время генерации")]
      .map((name) => el('th', { scope: 'col' }, name)))), body);
  const dialog = openDialog(host, { id: 'chat-stats-dialog', title: tr("Статистика"), cancelText: tr("Закрыть"),
    body: [el('p', { class: 'muted small' }, tr("Ответы ваших диалогов за {0} по дням и моделям", period())),
      note, el('div', { class: 'chat-stats-scroll' }, table)] });
  let days;
  try {
    days = (await api('GET', `/api/chats/stats?days=${DAYS}`)).days;
  } catch (err) {
    note.textContent = words(err);
    note.classList.add('error');
    return dialog;
  }
  note.textContent = days.length ? '' : tr("За {0} ответов нет", period());
  note.hidden = !!days.length;
  table.hidden = !days.length;
  body.replaceChildren(...days.map((row) => el('tr', {},
    el('td', {}, new Date(`${row.date}T12:00:00`).toLocaleDateString(locale(), { day: 'numeric', month: 'short' })),
    el('td', { class: 'chat-stats-model' }, row.model || '—'), el('td', {}, number(row.answers)),
    el('td', {}, number(row.in)), el('td', {}, number(row.out)),
    el('td', {}, Number.isFinite(row.gen_ms) ? duration(row.gen_ms) : '—'))));
  return dialog;
}

/** Блок «Статистика» в «Настройках → Чат»: кнопка того же окна. */
export function statsBlock(host) {
  return el('section', { class: 'block', id: 'chat-stats-settings' },
    el('h2', {}, tr("Статистика")),
    el('p', { class: 'muted small' }, tr("Ответы, токены и время генерации ваших диалогов за {0}", period())),
    button(tr("Показать статистику"), { class: 'btn' }, () => statsDialog(host)));
}
