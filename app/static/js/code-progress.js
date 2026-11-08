/* Сводка только из фактических событий/правок CLI. Данные живут вместе с DOM чата. */
import { el } from './ui.js';
import { changeView, patchFiles } from './code-diff.js';

const STATES = { running: 'работает', completed: 'завершён', errored: 'ошибка', failed: 'ошибка',
  shutdown: 'остановлен', interrupted: 'остановлен', pendingInit: 'запускается', unknown: 'статус не сообщается' };

export function progressView({ openFile, openChild } = {}) {
  const root = el('div', { class: 'code-progress', hidden: true });
  const details = el('details', { class: 'code-progress-details' });
  const summary = el('summary', { class: 'code-progress-summary' });
  const body = el('div', { class: 'code-progress-body' });
  details.append(summary, body);
  root.append(details);
  let explicit = {}, steps = [];
  let serial = '';
  let render = () => {};
  details.addEventListener('toggle', () => { if (details.open) render(); });

  function update(data = {}, currentSteps = steps, replace = false) {
    explicit = { ...(replace ? {} : explicit), ...data };
    steps = currentSteps;
    const edits = steps.filter((step) => !step.error).flatMap((step) => step.changes || []);
    const changes = explicit.changes;
    const files = changes?.files || [...new Map(edits.map((change) => [change.path, { path: change.path }])).values()];
    const agents = explicit.agents || [...new Map(steps.flatMap((step) => step.agents || []).map((row) => [row.id, row])).values()];
    const processes = steps.filter((step) => step.status === 'running' && ['Shell', 'Bash'].includes(step.tool));
    const plan = Array.isArray(explicit.plan) ? explicit.plan : [];
    const value = JSON.stringify([files, agents, processes.map((s) => [s.id, s.process_id]), plan, changes?.diff, edits]);
    if (value === serial) return;
    serial = value;
    const labels = [];
    if (files.length) {
      const counts = changes ? files.reduce((out, file) => [out[0] + (file.added || 0), out[1] + (file.removed || 0)], [0, 0]) : null;
      labels.push(`Файлов: ${files.length}${counts ? ` · +${counts[0]} −${counts[1]}` : ''}${changes?.truncated ? ' · показана часть' : ''}`);
    }
    if (agents.length) {
      const active = agents.filter((row) => ['running', 'pendingInit'].includes(row.status)).length;
      const done = agents.filter((row) => row.status === 'completed').length;
      const failed = agents.filter((row) => ['failed', 'errored'].includes(row.status)).length;
      labels.push(`Субагенты: ${agents.length} · ${active} в работе · ${done} готово${failed ? ` · ошибок: ${failed}` : ''}`);
    }
    if (processes.length) labels.push(`Выполняются команды: ${processes.length}`);
    if (plan.length) labels.push(`План: ${plan.filter((row) => row.status === 'completed').length}/${plan.length}`);
    root.hidden = !labels.length;
    summary.textContent = labels.join(' · ');
    render = () => {
      const blocks = [];
      if (plan.length) blocks.push(el('ol', { class: 'code-progress-plan' }, ...plan.map((row) => el('li', {
        'data-state': row.status }, `${row.status === 'completed' ? '✓' : row.status === 'inProgress' ? '◉' : '○'} ${row.step}`))));
      if (files.length) blocks.push(el('div', { class: 'code-progress-files' }, ...files.slice(0, 200).map((row) => {
        const button = el('button', { type: 'button', class: 'btn btn-sm code-progress-file' }, row.path,
          Number.isInteger(row.added) ? ` · +${row.added} −${row.removed}` : '');
        button.addEventListener('click', () => openFile?.(row.path));
        return button;
      })));
      if (agents.length) blocks.push(el('div', { class: 'code-progress-agents' }, ...agents.map((row) => {
        const button = el('button', { type: 'button', class: 'btn code-progress-agent', disabled: !openChild },
          el('b', {}, row.title || `Агент ${String(row.id).slice(0, 8)}`),
          el('span', {}, STATES[row.status] || STATES.unknown),
          row.task ? el('span', { class: 'muted small' }, row.task) : null);
        button.addEventListener('click', () => openChild?.(row));
        return button;
      })));
      if (processes.length) blocks.push(el('div', { class: 'code-progress-processes' }, ...processes.map((step) =>
        el('div', {}, `${step.process_id ? `Процесс ${step.process_id} · ` : ''}${step.input?.command || step.tool}`))));
      if (changes?.diff) blocks.push(changeView(patchFiles(changes.diff, changes.truncated)));
      else if (edits.length) blocks.push(changeView(edits));
      body.replaceChildren(...blocks.filter(Boolean));
    };
    if (details.open) render();
    else body.replaceChildren();
  }

  return { root, update };
}
