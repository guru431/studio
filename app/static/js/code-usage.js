/* Подписка выбранного помощника на выбранном компьютере. Только чтение, кэш в памяти моста. */
import { api } from './api.js';
import { el, words } from './ui.js';
import { ENGINE_NAMES } from './code-options.js';

const number = (n) => Number.isFinite(n) ? n.toLocaleString('ru-RU') : '—';
const date = (n) => Number.isFinite(n) ? new Date(n * 1000).toLocaleString('ru-RU') : 'Не сообщается';

export function usageView(host, { aid, engine, supported }) {
  let alive = true;
  let controller = null;
  let loading = false;
  const button = el('button', { type: 'button', id: 'code-usage', class: 'icon-btn code-usage',
    title: 'Лимиты и статистика подписки', 'aria-label': 'Лимиты и статистика подписки', 'aria-haspopup': 'dialog' });
  button.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h16M6 16v-5m6 5V4m6 12V8"/><path d="M4 4h3"/></svg>';
  const content = el('div', { class: 'code-usage-content', 'aria-live': 'polite' });
  const close = el('button', { type: 'button', class: 'btn' }, 'Закрыть');
  const refresh = el('button', { type: 'button', class: 'btn', id: 'code-usage-refresh' }, 'Обновить');
  const dialog = el('dialog', { id: 'code-usage-dialog', class: 'dialog code-usage-dialog',
    'aria-labelledby': 'code-usage-title' }, el('h2', { id: 'code-usage-title' }, `${ENGINE_NAMES[engine]} · подписка`),
  content, el('div', { class: 'dialog-actions' }, refresh, close));
  host.append(dialog);
  close.addEventListener('click', () => dialog.close());
  function paint(data) {
    const windows = (Array.isArray(data.windows) ? data.windows : []).map((w) => {
      const remaining = w.available && Number.isFinite(w.remaining_percent) ? w.remaining_percent : null;
      const meter = el('meter', { min: '0', max: '100', value: remaining ?? 0, hidden: remaining === null,
        'aria-label': `${w.label}: осталось ${number(remaining)}%` });
      return el('div', { class: 'code-quota', 'data-window': w.id },
        el('div', { class: 'code-quota-head' }, el('b', {}, w.label),
          el('span', {}, remaining === null ? 'Нет данных' : `Осталось ${number(remaining)}%`)), meter,
        el('p', { class: 'muted small' }, remaining === null ? w.reason || 'Провайдер не сообщает этот лимит'
          : `Использовано ${number(w.used_percent)}% · сброс: ${date(w.resets_at)}`));
    });
    const stats = data.statistics || {};
    const summary = stats.summary || {};
    const labels = { lifetime_tokens: 'Токенов за всё время', peak_daily_tokens: 'Максимум токенов за день',
      longest_running_turn_sec: 'Самый долгий ход, секунд', current_streak_days: 'Дней подряд сейчас',
      longest_streak_days: 'Максимум дней подряд', total_sessions: 'Сессий в истории CLI',
      total_messages: 'Сообщений в истории CLI' };
    const entries = Object.entries(labels).filter(([key]) => Number.isFinite(summary[key]));
    const daily = (Array.isArray(stats.daily) ? stats.daily : []).slice(-31);
    const links = (Array.isArray(data.links) ? data.links : []).filter((link) => {
      try { return new URL(link.url).protocol === 'https:'; } catch { return false; }
    });
    content.replaceChildren(...[
      el('p', { class: 'muted' }, [data.plan, data.message].filter(Boolean).join(' · ')), ...windows,
      el('h3', {}, 'Статистика использования'),
      el('p', { class: 'muted small' }, stats.source || data.source || ''),
      entries.length ? el('dl', { class: 'code-usage-summary' }, ...entries.flatMap(([key, label]) =>
        [el('dt', {}, label), el('dd', {}, number(summary[key]))])) : null,
      !stats.available ? el('p', { class: 'muted' }, stats.reason || 'Провайдер не сообщает статистику подписки') : null,
      daily.length ? el('details', {}, el('summary', {}, 'Использование по дням'),
        el('table', { class: 'code-usage-days' }, el('thead', {}, el('tr', {}, el('th', {}, 'Дата'), el('th', {}, 'Токены'))),
          el('tbody', {}, ...daily.map((d) => el('tr', {}, el('td', {}, d.date), el('td', {}, number(d.tokens))))))) : null,
      el('p', { class: 'muted small' }, `Обновлено: ${date(data.checked_at)} · кэш ${data.cache_ttl || 60} с`),
      ...links.map((link) => el('p', {}, el('a', { href: link.url, target: '_blank', rel: 'noopener noreferrer' }, link.label)))
    ].filter((node) => node !== null));
  }
  async function load(fresh = false) {
    if (loading) return;
    if (!supported) {
      content.replaceChildren(el('p', {}, 'Обновите мост на компьютере для просмотра лимитов и статистики.'));
      refresh.disabled = true;
      return;
    }
    loading = true;
    refresh.disabled = true;
    controller = new AbortController();
    content.replaceChildren(el('p', { class: 'muted' }, 'Получаю данные подписки…'));
    try {
      const data = await api('GET', `/api/code/agents/${aid}/usage?engine=${engine}&refresh=${fresh}`, undefined,
        { signal: controller.signal });
      if (alive) paint(data);
    } catch (err) {
      if (alive && !controller.signal.aborted) content.replaceChildren(el('p', { class: 'error' }, words(err)));
    } finally {
      loading = false;
      if (alive) refresh.disabled = false;
    }
  }
  button.addEventListener('click', () => { dialog.showModal(); load(); });
  refresh.addEventListener('click', () => load(true));
  return { button, unmount() { alive = false; controller?.abort(); dialog.remove(); } };
}
