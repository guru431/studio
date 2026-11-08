/* Подробности сессии: числа CLI и параметры чата, только память страницы. */
import { el } from './ui.js';
import { accessLabel, effortLabel, ENGINE_NAMES } from './code-options.js';

const number = (value) => Number.isFinite(value) ? value.toLocaleString('ru-RU') : 'Не сообщается';

export function contextView(host, { chat, engine, project, messages }) {
  let context = null;
  let settings = {};
  const hasCache = engine === 'codex' || engine === 'claude';
  const cacheText = el('b', { id: 'code-cache-text' }, '—');
  const cacheButton = el('button', { type: 'button', id: 'code-cache', class: 'code-cache',
    hidden: !hasCache, 'aria-haspopup': 'dialog' }, el('span', {}, 'Кэш'), cacheText);
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
  const cacheRead = el('dd');
  const cacheWrite = el('dd');
  const cacheDetail = el('dd', { 'aria-live': 'off' });
  const cacheClose = el('button', { type: 'button', class: 'btn' }, 'Закрыть');
  const cacheDialog = hasCache ? el('dialog', { id: 'code-cache-dialog', class: 'dialog code-usage-dialog',
    'aria-labelledby': 'code-cache-title' }, el('h2', { id: 'code-cache-title' }, 'Кэш'),
    el('dl', { class: 'code-usage-summary' }, el('dt', {}, 'Из кэша'), cacheRead,
      el('dt', {}, 'Запись в кэш'), cacheWrite, el('dt', {}, 'Кэш: оценка оставшегося времени'), cacheDetail),
    el('p', { class: 'muted small' }, engine === 'codex'
      ? 'Таймер кэша — оценка 30 минут от последней обработки контекста современной моделью Codex. Сервер не сообщает точное время хранения и не гарантирует попадание в кэш. Открытие чата отсчёт не продлевает.'
      : 'Таймер кэша — оценка по отчёту Claude: 5 минут или час, при смешанных сроках — короткий. Если срок или время обработки неизвестны, показано «—». Открытие чата отсчёт не продлевает.'),
    el('div', { class: 'dialog-actions' }, cacheClose)) : null;
  if (cacheDialog) host.append(cacheDialog);
  function cacheStatus() {
    const cache = context?.cache;
    const model = /^gpt-(\d+)(?:\.(\d+))?(?:-|$)/.exec(settings.model || '');
    const modern = model && (+model[1] >= 6 || (+model[1] === 5 && +model[2] >= 6));
    const ttl = engine === 'codex' && modern ? 1800
      : engine === 'claude' && [300, 3600].includes(cache?.ttl_seconds) ? cache.ttl_seconds : null;
    const now = Date.now() / 1000;
    if (!ttl || cache?.model !== settings.model || !Number.isFinite(cache?.updated_at)
        || cache.updated_at <= 0 || cache.updated_at > now) {
      return { state: 'unknown', text: '—', title: 'Кэш: время не сообщается. Сведения о кэше' };
    }
    const seconds = Math.max(0, Math.ceil(ttl - (now - cache.updated_at)));
    return { state: seconds ? 'estimated' : 'elapsed',
      text: `≈${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`,
      title: seconds ? `Кэш: ориентировочно осталось ${Math.floor(seconds / 60)} мин ${seconds % 60} с`
        : `Кэш: расчётные ${ttl / 60} минут прошли; состояние серверного кэша неизвестно` };
  }
  function paintCache() {
    if (!hasCache) return;
    const value = cacheStatus();
    cacheText.textContent = value.text;
    cacheButton.dataset.state = value.state;
    cacheButton.title = value.title;
    cacheButton.setAttribute('aria-label', value.title);
    cacheDetail.textContent = value.text;
    cacheRead.textContent = number(context?.usage?.cached);
    cacheWrite.textContent = number(context?.usage?.cache_write);
  }
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
      ['Рассуждения, токенов', number(usage.reasoning)],
    ];
    content.replaceChildren(...entries.flatMap(([label, value]) => [el('dt', {}, label), el('dd', {}, value)]));
  }
  button.addEventListener('click', () => { paint(); dialog.showModal(); });
  cacheButton.addEventListener('click', () => { paintCache(); cacheDialog?.showModal(); });
  close.addEventListener('click', () => dialog.close());
  cacheClose.addEventListener('click', () => cacheDialog?.close());
  paintCache();
  const cacheTimer = hasCache ? setInterval(paintCache, 1000) : null;
  if (hasCache) document.addEventListener('visibilitychange', paintCache);
  return {
    button, cacheButton,
    settings(data) { settings = { ...settings, ...data }; paintCache(); if (dialog.open) paint(); },
    update(data) {
      if (!data || !Number.isFinite(data.used) || !Number.isFinite(data.limit) || data.used < 0 || data.limit <= 0) return;
      const percent = Math.min(100, Math.round(1000 * data.used / data.limit) / 10);
      context = { ...data, percent };
      paintCache();
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
    unmount() { clearInterval(cacheTimer); document.removeEventListener('visibilitychange', paintCache); dialog.remove(); cacheDialog?.remove(); },
  };
}
