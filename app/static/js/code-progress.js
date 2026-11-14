import { tr } from './i18n.js';
/* Сводка только из фактических событий/правок CLI. Данные живут вместе с DOM чата. */
import { el } from './ui.js';
import { patchFiles } from './code-diff.js';

const STATES = { running: tr("работает"), completed: tr("завершён"), errored: tr("ошибка"), failed: tr("ошибка"),
  shutdown: tr("остановлен"), interrupted: tr("остановлен"), pendingInit: tr("запускается"), unknown: tr("статус не сообщается") };

export function progressView({ onChanges, openChild } = {}) {
  const root = el('div', { class: 'code-progress', hidden: true });
  const details = el('details', { class: 'code-progress-details' });
  const summary = el('summary', { class: 'code-progress-summary' });
  const body = el('div', { class: 'code-progress-body' });
  details.append(summary, body);
  root.append(details);
  let explicit = {}, steps = [];
  let fileState = { files: [], changes: [] };
  let serial = '';
  let render = () => {};
  details.addEventListener('toggle', () => { if (details.open) render(); });

  function update(data = {}, currentSteps = steps, replace = false) {
    explicit = { ...(replace ? {} : explicit), ...data };
    steps = currentSteps;
    const edits = steps.filter((step) => !step.error).flatMap((step) => step.changes || []);
    const changes = explicit.changes;
    const files = [...new Map((changes?.files || edits).filter((row) => typeof row?.path === 'string')
      .map((row) => [row.path, { path: row.path, added: row.added, removed: row.removed }])).values()];
    const agents = explicit.agents || [...new Map(steps.flatMap((step) => step.agents || []).map((row) => [row.id, row])).values()];
    const plan = Array.isArray(explicit.plan) ? explicit.plan : [];
    const value = JSON.stringify([files, agents, plan, changes, edits]);
    if (value === serial) return;
    serial = value;
    fileState = { files, changes: changes?.diff ? patchFiles(changes.diff, changes.truncated) : edits,
      truncated: !!changes?.truncated };
    onChanges?.();
    const labels = [];
    if (agents.length) {
      const active = agents.filter((row) => ['running', 'pendingInit'].includes(row.status)).length;
      const done = agents.filter((row) => row.status === 'completed').length;
      const failed = agents.filter((row) => ['failed', 'errored'].includes(row.status)).length;
      labels.push(tr("Субагенты: {0} · {1} в работе · {2} готово{3}", agents.length, active, done, failed ? tr(" · ошибок: {0}", failed) : ''));
    }
    if (plan.length) labels.push(tr("План: {0}/{1}", plan.filter((row) => row.status === 'completed').length, plan.length));
    root.hidden = !labels.length;
    summary.textContent = labels.join(' · ');
    render = () => {
      const blocks = [];
      if (plan.length) blocks.push(el('ol', { class: 'code-progress-plan' }, ...plan.map((row) => el('li', {
        'data-state': row.status }, `${row.status === 'completed' ? '✓' : row.status === 'inProgress' ? '◉' : '○'} ${row.step}`))));
      if (agents.length) blocks.push(el('div', { class: 'code-progress-agents' }, ...agents.map((row) => {
        const button = el('button', { type: 'button', class: 'btn code-progress-agent', disabled: !openChild },
          el('b', {}, row.title || tr("Агент {0}", String(row.id).slice(0, 8))),
          el('span', {}, STATES[row.status] || STATES.unknown),
          row.task ? el('span', { class: 'muted small' }, row.task) : null);
        button.addEventListener('click', () => openChild?.(row));
        return button;
      })));
      body.replaceChildren(...blocks.filter(Boolean));
    };
    if (details.open) render();
    else body.replaceChildren();
  }

  return { root, update, changes: () => fileState };
}
