import { locale, tr } from './i18n.js';
/* Подписка выбранного помощника на выбранном компьютере. Только чтение, кэш в памяти моста. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { el, words } from './ui.js';
import { ENGINE_NAMES } from './code-options.js';

const number = (n) => Number.isFinite(n) ? n.toLocaleString(locale()) : '—';
const date = (n) => Number.isFinite(n) ? dateTime(n) : tr("Не сообщается");

export function usageView(host, { aid, engine, supported, model = () => '' }) {
  let alive = true;
  let controller = null;
  let loading = false;
  let queuedLoad = null;
  let paintedModel = null;
  const button = el('button', { type: 'button', id: 'code-usage', class: 'icon-btn code-usage',
    title: tr("Лимиты и статистика подписки"), 'aria-label': tr("Лимиты и статистика подписки"), 'aria-haspopup': 'dialog' });
  const remainingText = el('b', { id: 'code-usage-remaining' }, '—');
  button.append(el('span', {}, tr("Неделя")), remainingText);
  const content = el('div', { class: 'code-usage-content', 'aria-live': 'polite' });
  const close = el('button', { type: 'button', class: 'btn' }, tr("Закрыть"));
  const refresh = el('button', { type: 'button', class: 'btn', id: 'code-usage-refresh' }, tr("Обновить"));
  const dialog = el('dialog', { id: 'code-usage-dialog', class: 'dialog code-usage-dialog',
    'aria-labelledby': 'code-usage-title' }, el('h2', { id: 'code-usage-title' }, tr("{0} · подписка", ENGINE_NAMES[engine])),
  content, el('div', { class: 'dialog-actions' }, refresh, close));
  host.append(dialog);
  close.addEventListener('click', () => dialog.close());
  function paint(data) {
    const week = (Array.isArray(data.windows) ? data.windows : []).find((w) => w.id === 'seven_day' || w.window_minutes === 10080);
    const remaining = week?.available && Number.isFinite(week.remaining_percent) ? week.remaining_percent : null;
    remainingText.textContent = remaining === null ? '—' : `${number(remaining)}%`;
    button.dataset.level = remaining === null ? 'unknown' : remaining <= 10 ? 'red' : remaining <= 30 ? 'yellow' : 'green';
    button.title = remaining === null ? tr("Недельный лимит не сообщается · Подробности подписки")
      : tr("Недельный лимит: осталось {0}% · сброс: {1}", number(remaining), date(week.resets_at));
    button.setAttribute('aria-label', button.title);
    const windows = (Array.isArray(data.windows) ? data.windows : []).map((w) => {
      const remaining = w.available && Number.isFinite(w.remaining_percent) ? w.remaining_percent : null;
      const meter = el('meter', { min: '0', max: '100', value: remaining ?? 0, hidden: remaining === null,
        'aria-label': tr("{0}: осталось {1}%", w.label, number(remaining)) });
      return el('div', { class: 'code-quota', 'data-window': w.id },
        el('div', { class: 'code-quota-head' }, el('b', {}, w.label),
          el('span', {}, remaining === null ? tr("Нет данных") : tr("Осталось {0}%", number(remaining)))), meter,
        el('p', { class: 'muted small' }, remaining === null ? w.reason || tr("Провайдер не сообщает этот лимит")
          : tr("Использовано {0}% · сброс: {1}", number(w.used_percent), date(w.resets_at))));
    });
    const stats = data.statistics || {};
    const summary = stats.summary || {};
    const labels = { ...(engine === 'codex' ? { available_resets: tr("Доступные сбросы лимита") } : {}),
      lifetime_tokens: tr("Токенов за всё время"), peak_daily_tokens: tr("Максимум токенов за день"),
      longest_running_turn_sec: tr("Самый долгий ход, секунд"), current_streak_days: tr("Дней подряд сейчас"),
      longest_streak_days: tr("Максимум дней подряд"), total_sessions: tr("Сессий в истории CLI"),
      total_messages: tr("Сообщений в истории CLI") };
    const entries = Object.entries(labels).filter(([key]) => key === 'available_resets' || Number.isFinite(summary[key]));
    const resetCredits = engine === 'codex' && summary.available_resets !== 0 && Array.isArray(stats.reset_credits)
      ? stats.reset_credits : [];
    const resetDetails = engine === 'codex' && (summary.available_resets > 0 || resetCredits.length)
      ? el('div', { class: 'code-reset-credits' }, el('h4', {}, tr("Срок действия сбросов")),
        resetCredits.length ? el('dl', { class: 'code-usage-summary' }, ...resetCredits.flatMap((credit, index) =>
          [el('dt', {}, tr("Сброс {0}", index + 1)),
            el('dd', {}, Number.isFinite(credit.expires_at) ? tr("До {0}", date(credit.expires_at)) : tr("Не сообщается"))]))
          : el('p', { class: 'muted small' }, tr("Срок действия не сообщается")),
        resetCredits.length && summary.available_resets > resetCredits.length
          ? el('p', { class: 'muted small' }, tr("Сроки указаны для {0} из {1} сбросов", resetCredits.length, number(summary.available_resets)))
          : null) : null;
    const daily = (Array.isArray(stats.daily) ? stats.daily : []).slice(-31);
    const links = (Array.isArray(data.links) ? data.links : []).filter((link) => {
      try { return new URL(link.url).protocol === 'https:'; } catch { return false; }
    });
    content.replaceChildren(...[
      el('p', { class: 'muted' }, [data.plan, data.message].filter(Boolean).join(' · ')), ...windows,
      el('h3', {}, tr("Статистика использования")),
      el('p', { class: 'muted small' }, stats.source || data.source || ''),
      entries.length ? el('dl', { class: 'code-usage-summary' }, ...entries.flatMap(([key, label]) =>
        [el('dt', {}, label), el('dd', {}, Number.isFinite(summary[key]) ? number(summary[key]) : tr("Не сообщается"))])) : null,
      resetDetails,
      !stats.available ? el('p', { class: 'muted' }, stats.reason || tr("Провайдер не сообщает статистику подписки")) : null,
      daily.length ? el('details', {}, el('summary', {}, tr("Использование по дням")),
        el('table', { class: 'code-usage-days' }, el('thead', {}, el('tr', {}, el('th', {}, tr("Дата")), el('th', {}, tr("Токены")))),
          el('tbody', {}, ...daily.map((d) => el('tr', {}, el('td', {}, d.date), el('td', {}, number(d.tokens))))))) : null,
      el('p', { class: 'muted small' }, tr("Обновлено: {0} · кэш {1} с", date(data.checked_at), data.cache_ttl || 60)),
      ...links.map((link) => el('p', {}, el('a', { href: link.url, target: '_blank', rel: 'noopener noreferrer' }, link.label)))
    ].filter((node) => node !== null));
  }
  /** passive — обновление после конца задания, не по действию владельца: срок PIN компьютера не продлевает. */
  async function load(fresh = false, passive = false) {
    const selectedModel = engine === 'opencode' ? model() : '';
    if (engine === 'opencode' && selectedModel !== paintedModel) {
      remainingText.textContent = '—';
      button.dataset.level = 'unknown';
      button.title = tr("Лимиты и статистика подписки");
      button.setAttribute('aria-label', button.title);
    }
    if (loading) {
      queuedLoad = { fresh: fresh || !!queuedLoad?.fresh, passive: passive && (queuedLoad?.passive ?? true) };
      return;
    }
    if (!supported) {
      content.replaceChildren(el('p', {}, tr("Обновите мост на компьютере для просмотра лимитов и статистики.")));
      refresh.disabled = true;
      return;
    }
    loading = true;
    refresh.disabled = true;
    controller = new AbortController();
    if (dialog.open) content.replaceChildren(el('p', { class: 'muted' }, tr("Получаю данные подписки…")));
    try {
      const suffix = selectedModel ? `&model=${encodeURIComponent(selectedModel)}` : '';
      const data = await api('GET', `/api/code/agents/${aid}/usage?engine=${engine}&refresh=${fresh}${suffix}`, undefined,
        { signal: controller.signal, ...(passive ? { headers: { 'x-studio-passive': '1' } } : {}) });
      if (alive && selectedModel === (engine === 'opencode' ? model() : '')) { paint(data); paintedModel = selectedModel; }
      else if (alive) queuedLoad ??= { fresh: false, passive: true };
    } catch (err) {
      if (alive && !controller.signal.aborted && selectedModel === (engine === 'opencode' ? model() : '')) {
        remainingText.textContent = '—';
        button.dataset.level = 'unknown';
        button.title = tr("Лимиты недоступны: {0}", words(err));
        button.setAttribute('aria-label', button.title);
        content.replaceChildren(el('p', { class: 'error' }, words(err)));
      }
    } finally {
      loading = false;
      if (alive) refresh.disabled = false;
      const next = queuedLoad;
      queuedLoad = null;
      if (alive && next) load(next.fresh, next.passive);
    }
  }
  button.addEventListener('click', () => { dialog.showModal(); load(); });
  refresh.addEventListener('click', () => load(true));
  load();
  return { button, refresh: () => load(false, true), unmount() { alive = false; controller?.abort(); dialog.remove(); } };
}
