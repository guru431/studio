/* История штатных дочерних агентов. Все ответы остаются в памяти открытой страницы. */
import { api } from './api.js';
import { el, words } from './ui.js';
import { renderMarkdown } from './markdown.js';

export function subagentView(host, { endpoint }) {
  const button = el('button', { type: 'button', id: 'code-subagents', class: 'icon-btn',
    'aria-label': 'Подзадачи', title: 'Подзадачи' });
  button.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="5" rx="1"/><rect x="2" y="16" width="6" height="5" rx="1"/><rect x="16" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M5 16v-4h14v4"/></svg>';
  const list = el('div', { class: 'code-subagents-list' });
  const history = el('div', { class: 'code-subagent-history' });
  const status = el('p', { class: 'muted small', role: 'status' });
  const refresh = el('button', { type: 'button', class: 'btn btn-sm' }, 'Обновить');
  const close = el('button', { type: 'button', class: 'btn' }, 'Закрыть');
  const dialog = el('dialog', { id: 'code-subagents-dialog', class: 'dialog code-subagents-dialog',
    'aria-labelledby': 'code-subagents-title' }, el('h2', { id: 'code-subagents-title' }, 'Подзадачи этого чата'),
  refresh, status, list, history, el('div', { class: 'dialog-actions' }, close));
  host.append(dialog);
  let alive = true, request = null;
  const statuses = { running: 'работает', completed: 'завершён', errored: 'ошибка', shutdown: 'остановлен', unknown: 'статус не сообщается' };

  async function read(path, body, render) {
    request?.abort();
    const own = request = new AbortController();
    status.textContent = 'Читаю на компьютере…';
    try {
      const data = await api(body ? 'POST' : 'GET', endpoint(path), body ?? undefined, { signal: own.signal });
      if (!alive || own.signal.aborted) return;
      status.textContent = '';
      render(data);
    } catch (err) {
      if (alive && !own.signal.aborted) status.textContent = words(err);
    }
  }

  function openChild(row) {
    history.replaceChildren();
    read('/subagent', { request_id: row.id }, (data) => {
      history.append(el('h3', {}, row.title));
      for (const message of data.items || []) {
        const text = el('div', { class: message.role === 'assistant' ? 'md' : 'code-subagent-user' });
        if (message.role === 'assistant') text.append(renderMarkdown(message.text || ''));
        else text.textContent = message.text || '';
        history.append(text);
        for (const step of message.steps || []) history.append(el('details', {},
          el('summary', {}, String(step.tool || 'Инструмент')), el('pre', {}, String(step.text || ''))));
      }
      if (!(data.items || []).length) history.append(el('p', { class: 'muted' }, 'История пока пуста'));
      if (data.complete === false) history.append(el('p', { class: 'muted small' }, 'Показана часть истории'));
    });
  }

  function load() {
    read('/subagents', null, (data) => {
      list.replaceChildren(...(data.items || []).map((row) => {
        const entry = el('button', { type: 'button', class: 'btn code-subagent-row' },
          `${row.title} · ${statuses[row.status] || 'статус не сообщается'}`);
        entry.addEventListener('click', () => openChild(row));
        return entry;
      }));
      if (!(data.items || []).length) list.append(el('p', { class: 'muted' }, 'Дочерние агенты пока не найдены'));
    });
  }
  button.addEventListener('click', () => { dialog.showModal(); load(); });
  refresh.addEventListener('click', load);
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { request?.abort(); history.replaceChildren(); });
  return { button, open(row) { dialog.showModal(); openChild(row); },
    unmount() { alive = false; request?.abort(); dialog.remove(); } };
}
