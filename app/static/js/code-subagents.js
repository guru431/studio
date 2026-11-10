/* История штатных дочерних агентов. Все ответы остаются в памяти открытой страницы. */
import { api } from './api.js';
import { el, words } from './ui.js';
import { renderMarkdown } from './markdown.js';

const POLL_MS = 10000; // События хода обновляют счётчик сразу; опрос сверяет историю и фоновые завершения.

export function subagentView(host, { endpoint }) {
  const button = el('button', { type: 'button', id: 'code-subagents', class: 'icon-btn',
    'aria-label': 'Подзадачи', title: 'Подзадачи' });
  button.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="5" rx="1"/><rect x="2" y="16" width="6" height="5" rx="1"/><rect x="16" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M5 16v-4h14v4"/></svg>';
  const count = el('span', { class: 'code-subagents-count', 'aria-hidden': 'true' }, '—');
  button.append(count);
  const list = el('div', { class: 'code-subagents-list' });
  const history = el('div', { class: 'code-subagent-history' });
  const status = el('p', { class: 'muted small', role: 'status' });
  const refresh = el('button', { type: 'button', class: 'btn btn-sm' }, 'Обновить');
  const close = el('button', { type: 'button', class: 'btn' }, 'Закрыть');
  const dialog = el('dialog', { id: 'code-subagents-dialog', class: 'dialog code-subagents-dialog',
    'aria-labelledby': 'code-subagents-title' }, el('h2', { id: 'code-subagents-title' }, 'Подзадачи этого чата'),
  refresh, status, list, history, el('div', { class: 'dialog-actions' }, close));
  host.append(dialog);
  let alive = true, request = null, listing = null, timer = null;
  let pending = false;
  let selected = null;
  const known = new Map();
  const statuses = { running: 'работает', inProgress: 'работает', pending: 'ожидает', waiting: 'ждёт ответа', completed: 'завершён',
    failed: 'ошибка', errored: 'ошибка', stopped: 'остановлен', shutdown: 'остановлен', unknown: 'статус не сообщается' };

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
    selected = row.id;
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

  function paint() {
    const rows = [...known.values()];
    const active = rows.filter(row => ['running', 'inProgress', 'pending', 'waiting'].includes(row.status)).length;
    const unknown = rows.filter(row => !row.status || row.status === 'unknown').length;
    count.textContent = String(active);
    const label = `Подзадачи · работают: ${active} · всего: ${rows.length}`
      + (unknown ? ` · статус неизвестен: ${unknown}` : '');
    button.title = label;
    button.setAttribute('aria-label', label);
    if (!dialog.open) return;
    const focused = document.activeElement?.dataset.subagent;
    list.replaceChildren(...rows.map((row) => {
        const entry = el('button', { type: 'button', class: 'btn code-subagent-row' },
          `${row.title} · ${statuses[row.status] || 'статус не сообщается'}`);
        entry.dataset.subagent = row.id;
        entry.setAttribute('aria-pressed', String(selected === row.id));
        entry.addEventListener('click', () => openChild(row));
        return entry;
      }));
    if (focused) [...list.children].find(node => node.dataset.subagent === focused)?.focus({ preventScroll: true });
    if (!rows.length) list.append(el('p', { class: 'muted' }, 'Дочерние агенты пока не найдены'));
  }

  async function load() {
    if (!alive || document.hidden) return;
    if (listing) { pending = true; return; }
    clearTimeout(timer);
    const own = listing = new AbortController();
    try {
      const data = await api('GET', endpoint('/subagents'), undefined,
        { signal: own.signal, headers: { 'x-studio-passive': '1' } });
      if (!alive || own.signal.aborted) return;
      update(data.items);
      if (dialog.open) status.textContent = '';
    } catch (err) {
      if (alive && !own.signal.aborted && dialog.open) status.textContent = words(err);
    } finally {
      if (listing === own) listing = null;
      if (alive) {
        timer = setTimeout(load, pending ? 150 : POLL_MS);
        pending = false;
      }
    }
  }
  function schedule() {
    if (!alive || document.hidden) return;
    if (listing) { pending = true; return; }
    clearTimeout(timer);
    timer = setTimeout(load, 150);
  }
  function update(rows, live = false) {
    for (const row of Array.isArray(rows) ? rows : []) {
      if (!row || typeof row.id !== 'string') continue;
      const before = known.get(row.id);
      const terminal = ['completed', 'failed', 'errored', 'stopped', 'shutdown'];
      const status = before && !live && terminal.includes(before.status) && !terminal.includes(row.status)
        ? before.status : row.status;
      known.set(row.id, { ...before, ...row,
        title: row.title || before?.title || 'Подзадача',
        status });
    }
    paint();
  }
  button.addEventListener('click', () => { dialog.showModal(); paint(); load(); });
  refresh.addEventListener('click', load);
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { request?.abort(); selected = null; history.replaceChildren(); });
  const visibility = () => {
    if (document.hidden) { clearTimeout(timer); listing?.abort(); }
    else load();
  };
  document.addEventListener('visibilitychange', visibility);
  load();
  return { button, refresh: schedule, update, open(row) { dialog.showModal(); paint(); openChild(row); load(); },
    unmount() {
      alive = false; request?.abort(); listing?.abort(); clearTimeout(timer);
      document.removeEventListener('visibilitychange', visibility);
      known.clear(); dialog.remove();
    } };
}
