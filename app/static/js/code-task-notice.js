/* Служебное уведомление Claude остаётся текстом: разметка из вывода задач не исполняется. */
import { el } from './ui.js';

const LABELS = { completed: 'Завершена', failed: 'Ошибка', stopped: 'Остановлена', running: 'Выполняется' };
const ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'" };

export function taskNotice(text) {
  if (typeof text !== 'string' || text.length > 65536) return null;
  const source = text.trim();
  if (!source.startsWith('<task-notification>') || !source.endsWith('</task-notification>')) return null;
  const fields = new Map();
  for (const match of source.matchAll(/<(task-id|status|summary|event|output)>([\s\S]*?)<\/\1>/g)) {
    const value = match[2].trim().replace(/&(?:amp|lt|gt|quot|apos);/g, entity => ENTITIES[entity]);
    fields.set(match[1], [...(fields.get(match[1]) || []), value]);
  }
  const ids = (fields.get('task-id') || []).filter(id => /^[\w-]{1,64}$/.test(id) && !id.startsWith('__orphan_summary'));
  const summary = fields.get('summary')?.[0];
  if (!ids.length || !summary) return null;
  const status = fields.get('status')?.[0];
  const state = Object.hasOwn(LABELS, status) ? status : 'update';
  const root = el('article', { class: 'code-msg code-task-notice', 'data-state': state },
    el('div', { class: 'code-task-heading' }, el('strong', {}, ids.length > 1 ? 'Фоновые задачи' : 'Фоновая задача'),
      el('span', { class: 'code-task-state' }, LABELS[state] || 'Обновление')),
    el('p', { class: 'code-task-summary' }, summary));
  const output = [...(fields.get('event') || []), ...(fields.get('output') || [])].join('\n\n');
  if (output) root.append(el('details', { open: true, class: 'code-task-details' },
    el('summary', {}, 'Вывод задачи'), el('pre', {}, output)));
  root.append(el('small', { class: 'code-task-id' }, ids.join(' · ')));
  return root;
}
