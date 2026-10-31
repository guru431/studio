/* Подробности сессии: числа CLI и параметры чата, только память страницы. */
import { el } from './ui.js';
import { accessLabel, effortLabel, ENGINE_NAMES } from './code-options.js';

const number = (value) => Number.isFinite(value) ? value.toLocaleString('ru-RU') : 'Не сообщается';

export function contextView(host, { chat, engine, project, messages }) {
  let context = null;
  let settings = {};
  const text = el('span', { id: 'code-context-text' }, '—');
  const meter = el('meter', { id: 'code-context-meter', class: 'sr', min: '0', max: '100', value: '0', hidden: true });
  const button = el('button', { type: 'button', id: 'code-context', class: 'code-context',
    'aria-label': 'Контекст: нет данных. Подробности сессии', 'aria-haspopup': 'dialog',
    title: 'Подробности сессии и контекста' }, meter, text);
  const content = el('dl', { class: 'code-usage-summary code-session-summary', 'aria-live': 'polite' });
  const close = el('button', { type: 'button', class: 'btn' }, 'Закрыть');
  const dialog = el('dialog', { id: 'code-context-dialog', class: 'dialog code-usage-dialog',
    'aria-labelledby': 'code-context-title' }, el('h2', { id: 'code-context-title' }, 'Сессия и контекст'), content,
  el('p', { class: 'muted small' }, 'Контекст — текущая заполненность окна. Расход токенов — накопленный за сессию, включая повторное чтение контекста. Данные сообщает помощник.'),
  el('div', { class: 'dialog-actions' }, close));
  host.append(dialog);
  function paint() {
    const usage = context?.usage || {};
    const entries = [
      ['Помощник', ENGINE_NAMES[engine]], ['Проект', project?.name || '—'], ['Сессия', chat],
      ['Модель', settings.model || 'Не сообщается'], ['Усилие', effortLabel(settings.effort)],
      ['Права', accessLabel(engine, settings.access)], ['Сообщений в ленте', number(messages())],
      ['Общий размер контекста, токенов', number(context?.limit)],
      ['Занято токенов', number(context?.used)],
      ['Свободно токенов', number(context ? Math.max(0, context.limit - context.used) : null)],
      ['Заполнено', context ? `${number(context.percent)}%` : 'Не сообщается'],
      ['Потрачено токенов за сессию', number(usage.total)],
      ['Входные токены', number(usage.input)], ['Выходные токены', number(usage.output)],
      ['Из кэша', number(usage.cached)], ['Запись в кэш', number(usage.cache_write)],
      ['Рассуждения, токенов', number(usage.reasoning)],
    ];
    content.replaceChildren(...entries.flatMap(([label, value]) => [el('dt', {}, label), el('dd', {}, value)]));
  }
  button.addEventListener('click', () => { paint(); dialog.showModal(); });
  close.addEventListener('click', () => dialog.close());
  return {
    button,
    settings(data) { settings = { ...settings, ...data }; if (dialog.open) paint(); },
    update(data) {
      if (!data || !Number.isFinite(data.used) || !Number.isFinite(data.limit) || data.used < 0 || data.limit <= 0) return;
      const percent = Math.min(100, Math.round(1000 * data.used / data.limit) / 10);
      context = { ...data, percent };
      meter.value = percent;
      meter.hidden = false;
      text.textContent = `${Math.round(percent)}%`;
      button.style.setProperty('--context-percent', `${percent}%`);
      button.dataset.level = percent < 50 ? 'green' : percent < 70 ? 'yellow' : percent < 90 ? 'orange' : 'red';
      button.setAttribute('aria-label', `Контекст заполнен на ${percent}%. Подробности сессии`);
      button.title = `${number(data.used)} / ${number(data.limit)} токенов · Подробности сессии`;
      button.dataset.used = String(data.used);
      button.dataset.limit = String(data.limit);
      if (dialog.open) paint();
    },
    unmount() { dialog.remove(); },
  };
}
