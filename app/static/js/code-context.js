import { locale, tr } from './i18n.js';
/* Подробности сессии: числа CLI и параметры чата, только память страницы. */
import { el } from './ui.js';
import { accessLabel, effortLabel, ENGINE_NAMES } from './code-options.js';

const number = (value) => Number.isFinite(value) ? value.toLocaleString(locale()) : tr("Не сообщается");

export function contextView(host, { chat, engine, project, messages }) {
  let context = null;
  let settings = {};
  let cacheTimer = null;
  const hasCache = engine === 'codex' || engine === 'claude';
  const cacheText = el('b', { id: 'code-cache-text' }, '—');
  const cacheButton = el('button', { type: 'button', id: 'code-cache', class: 'code-cache',
    hidden: !hasCache, 'aria-haspopup': 'dialog' }, el('span', {}, tr("Кэш")), cacheText);
  const text = el('span', { id: 'code-context-text' }, '—');
  const meter = el('meter', { id: 'code-context-meter', class: 'sr', min: '0', max: '100', value: '0', hidden: true });
  const button = el('button', { type: 'button', id: 'code-context', class: 'code-context',
    'aria-label': tr("Контекст: нет данных. Подробности сессии"), 'aria-haspopup': 'dialog',
    title: tr("Подробности сессии и контекста") }, meter, text);
  const content = el('dl', { class: 'code-usage-summary code-session-summary', 'aria-live': 'polite' });
  const close = el('button', { type: 'button', class: 'btn' }, tr("Закрыть"));
  const dialog = el('dialog', { id: 'code-context-dialog', class: 'dialog code-usage-dialog',
    'aria-labelledby': 'code-context-title' }, el('h2', { id: 'code-context-title' }, tr("Сессия и контекст")), content,
  el('p', { class: 'muted small' }, engine === 'opencode'
    ? tr('Контекст — текущая заполненность окна. Токены накапливаются по завершённым запросам из Студии; запросы из других приложений в эту сумму не входят. Стоимость всей сессии сообщает OpenCode.')
    : tr("Контекст — текущая заполненность окна. Расход токенов — накопленный за сессию, включая повторное чтение контекста. Данные сообщает помощник.")),
  el('div', { class: 'dialog-actions' }, close));
  host.append(dialog);
  const cacheRead = el('dd');
  const cacheWrite = el('dd');
  const cacheDetail = el('dd', { 'aria-live': 'off' });
  const cacheClose = el('button', { type: 'button', class: 'btn' }, tr("Закрыть"));
  const cacheDialog = hasCache ? el('dialog', { id: 'code-cache-dialog', class: 'dialog code-usage-dialog',
    'aria-labelledby': 'code-cache-title' }, el('h2', { id: 'code-cache-title' }, tr("Кэш")),
    el('dl', { class: 'code-usage-summary' }, el('dt', {}, tr("Из кэша")), cacheRead,
      el('dt', {}, tr("Запись в кэш")), cacheWrite, el('dt', {}, tr("Кэш: оценка оставшегося времени")), cacheDetail),
    el('p', { class: 'muted small' }, engine === 'codex'
      ? tr("Таймер кэша — оценка 30 минут от последней обработки контекста современной моделью Codex. Сервер не сообщает точное время хранения и не гарантирует попадание в кэш. Открытие чата отсчёт не продлевает.")
      : tr("Таймер кэша — оценка по отчёту Claude: 5 минут или час, при смешанных сроках — короткий. Если срок или время обработки неизвестны, показано «—». Открытие чата отсчёт не продлевает.")),
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
      return { state: 'unknown', text: '—', title: tr("Кэш: время не сообщается. Сведения о кэше") };
    }
    const seconds = Math.max(0, Math.ceil(ttl - (now - cache.updated_at)));
    return { state: seconds ? 'estimated' : 'elapsed',
      text: `≈${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`,
      title: seconds ? tr("Кэш: ориентировочно осталось {0} мин {1} с", Math.floor(seconds / 60), seconds % 60)
        : tr("Кэш: расчётные {0} минут прошли; состояние серверного кэша неизвестно", ttl / 60) };
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
    if (value.state === 'estimated' && !document.hidden) {
      cacheTimer ??= setInterval(paintCache, 1000);
    } else {
      clearInterval(cacheTimer);
      cacheTimer = null;
    }
  }
  function paint() {
    const usage = context?.usage || {};
    const entries = [
      [tr("Помощник"), ENGINE_NAMES[engine]], [tr("Проект"), project?.name || '—'], [tr("Сессия"), chat],
      [tr("Модель"), settings.model || tr("Не сообщается")], [tr("Усилие"), effortLabel(settings.effort)],
      [tr("Права"), accessLabel(engine, settings.access)], [tr("Сообщений в ленте"), number(messages())],
      [tr("Общий размер контекста, токенов"), number(context?.limit)],
      [tr("Занято токенов"), number(context?.used)],
      [tr("Свободно токенов"), number(context ? Math.max(0, context.limit - context.used) : null)],
      [tr("Заполнено"), Number.isFinite(context?.percent) ? `${number(context.percent)}%` : tr("Не сообщается")],
      [engine === 'opencode' ? tr('Токены завершённых ходов Студии') : tr("Потрачено токенов за сессию"), number(usage.total)],
      [tr("Входные токены"), number(usage.input)], [tr("Выходные токены"), number(usage.output)],
      [tr("Рассуждения, токенов"), number(usage.reasoning)],
      ...(engine === 'opencode' ? [[tr('Стоимость сессии, USD'), Number.isFinite(context?.cost?.amount)
        ? context.cost.amount.toLocaleString(locale(), { maximumFractionDigits: 6 }) : tr('Не сообщается')],
        [tr('Основной агент'), settings.agent_mode || tr('Не сообщается')]] : []),
    ];
    content.replaceChildren(...entries.flatMap(([label, value]) => [el('dt', {}, label), el('dd', {}, value)]));
  }
  button.addEventListener('click', () => { paint(); dialog.showModal(); });
  cacheButton.addEventListener('click', () => { paintCache(); cacheDialog?.showModal(); });
  close.addEventListener('click', () => dialog.close());
  cacheClose.addEventListener('click', () => cacheDialog?.close());
  paintCache();
  if (hasCache) document.addEventListener('visibilitychange', paintCache);
  return {
    button, cacheButton,
    settings(data) { settings = { ...settings, ...data }; paintCache(); if (dialog.open) paint(); },
    update(data) {
      if (!data) return;
      if (!Number.isFinite(data.used) || !Number.isFinite(data.limit) || data.used < 0 || data.limit <= 0) {
        if (data.usage || data.cost) { context = { ...context, ...data }; if (dialog.open) paint(); }
        return;
      }
      const percent = Math.min(100, Math.round(1000 * data.used / data.limit) / 10);
      context = { ...data, percent };
      paintCache();
      meter.value = percent;
      meter.hidden = false;
      text.textContent = `${Math.round(percent)}%`;
      button.style.setProperty('--context-percent', `${percent}%`);
      button.dataset.level = percent < 50 ? 'green' : percent < 70 ? 'yellow' : percent < 90 ? 'orange' : 'red';
      button.setAttribute('aria-label', tr("Контекст заполнен на {0}%. Подробности сессии", percent));
      button.title = tr("{0} / {1} токенов · Подробности сессии", number(data.used), number(data.limit));
      button.dataset.used = String(data.used);
      button.dataset.limit = String(data.limit);
      if (dialog.open) paint();
    },
    unmount() { clearInterval(cacheTimer); document.removeEventListener('visibilitychange', paintCache); dialog.remove(); cacheDialog?.remove(); },
  };
}
