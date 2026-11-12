/* История штатных дочерних агентов. Все ответы остаются в памяти открытой страницы. */
import { api } from './api.js';
import { icon } from './icons.js';
import { el, words } from './ui.js';
import { renderMarkdown } from './markdown.js';
import { dateTime } from './time.js';

const POLL_MS = 10000; // События хода обновляют счётчик сразу; опрос сверяет историю и фоновые завершения.
const HISTORY_POLL_MS = 2000;
const ACTIVE = ['running', 'inProgress', 'pending', 'waiting'];

function duration(row) {
  const elapsed = ACTIVE.includes(row.status) && Number.isFinite(row.started_at)
    ? Math.max(row.duration_ms || 0, Date.now() - row.started_at * 1000) : row.duration_ms;
  if (!Number.isFinite(elapsed)) return 'нет данных';
  const seconds = Math.floor(Math.max(0, elapsed) / 1000);
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60;
  return `${hours ? `${hours} ч ` : ''}${hours || minutes ? `${minutes} мин ` : ''}${seconds % 60} с`;
}

export function subagentView(host, { endpoint, supported = true }) {
  const button = el('button', { type: 'button', id: 'code-subagents', class: 'icon-btn',
    'aria-label': 'Подзадачи', title: 'Подзадачи' });
  button.append(icon('subagents'));
  const count = el('span', { class: 'code-subagents-count', 'aria-hidden': 'true' }, '—');
  button.append(count);
  if (!supported) {
    button.disabled = true;
    button.title = 'Подзадачи недоступны для этого помощника или версии моста';
    button.setAttribute('aria-label', button.title);
    return { button, unmount() {} };
  }
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
  let historyVersion = null;
  const known = new Map();
  const statuses = { running: 'работает', inProgress: 'работает', pending: 'ожидает', waiting: 'ждёт ответа', completed: 'завершён',
    failed: 'ошибка', errored: 'ошибка', stopped: 'остановлен', shutdown: 'остановлен', interrupted: 'остановлен', unknown: 'статус не сообщается' };

  async function read(path, body, render, passive = false) {
    if (passive && request) return;
    request?.abort();
    const own = request = new AbortController();
    if (!passive) status.textContent = 'Читаю на компьютере…';
    try {
      const data = await api(body ? 'POST' : 'GET', endpoint(path), body ?? undefined,
        { signal: own.signal, ...(passive ? { headers: { 'x-studio-passive': '1' } } : {}) });
      if (!alive || own.signal.aborted) return;
      status.textContent = '';
      render(data);
    } catch (err) {
      if (alive && !own.signal.aborted) status.textContent = words(err);
    } finally {
      if (request === own) request = null;
    }
  }

  function openChild(row) {
    selected = row.id;
    historyVersion = null;
    history.replaceChildren();
    paint();
    loadChild(row);
    schedule();
  }

  function loadChild(row, passive = false) {
    return read('/subagent', { request_id: row.id }, (data) => {
      if (!dialog.open || selected !== row.id) return;
      const version = JSON.stringify([row.title, data.items, data.complete]);
      if (version === historyVersion) return;
      const selection = window.getSelection();
      if (passive && !selection?.isCollapsed && history.contains(selection?.anchorNode)) return;
      const top = history.scrollTop, outerTop = dialog.scrollTop;
      const atEnd = history.scrollHeight - history.clientHeight - top < 8;
      const expanded = new Set([...history.querySelectorAll('details[open]')].map(node => node.dataset.step));
      const focus = document.activeElement?.closest('details')?.dataset.step;
      const content = [el('h3', {}, row.title)];
      for (const [index, message] of (data.items || []).entries()) {
        const text = el('div', { class: message.role === 'assistant' ? 'md' : 'code-subagent-user' });
        if (message.role === 'assistant') text.append(renderMarkdown(message.text || ''));
        else text.textContent = message.text || '';
        content.push(text);
        for (const [number, step] of (message.steps || []).entries()) {
          const key = `${index}:${number}`;
          const detail = el('details', { 'data-step': key },
            el('summary', {}, String(step.tool || 'Инструмент')), el('pre', {}, String(step.text || '')));
          detail.open = expanded.has(key);
          content.push(detail);
        }
      }
      if (!(data.items || []).length) content.push(el('p', { class: 'muted' }, 'История пока пуста'));
      if (data.complete === false) content.push(el('p', { class: 'muted small' }, 'Показана часть истории'));
      history.replaceChildren(...content);
      historyVersion = version;
      if (focus) history.querySelector(`details[data-step="${focus}"] > summary`)?.focus({ preventScroll: true });
      history.scrollTop = passive && atEnd ? history.scrollHeight : top;
      dialog.scrollTop = outerTop;
    }, passive);
  }

  function paint() {
    const rows = [...known.values()];
    const active = rows.filter(row => ACTIVE.includes(row.status)).length;
    const unknown = rows.filter(row => !row.status || row.status === 'unknown').length;
    count.textContent = String(active);
    const label = `Подзадачи · работают: ${active} · всего: ${rows.length}`
      + (unknown ? ` · статус неизвестен: ${unknown}` : '');
    button.title = label;
    button.setAttribute('aria-label', label);
    if (!dialog.open) return;
    const focused = document.activeElement?.dataset.subagent;
    list.replaceChildren(...rows.map((row) => {
        const entry = el('button', { type: 'button', class: 'btn code-subagent-row',
          'data-status': row.status || 'unknown' },
          el('span', { class: 'code-subagent-title' }, `${row.title} · ${statuses[row.status] || 'статус не сообщается'}`),
          el('span', { class: 'code-subagent-metrics' },
            `Запуск: ${Number.isFinite(row.started_at) ? dateTime(row.started_at) : 'нет данных'}`,
            el('span', { 'data-duration': row.id }, `Длительность: ${duration(row)}`),
            `Токены: ${Number.isFinite(row.total_tokens) ? row.total_tokens.toLocaleString('ru-RU') : 'нет данных'}`));
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
      if (dialog.open && selected && known.has(selected)) await loadChild(known.get(selected), true);
    } catch (err) {
      if (alive && !own.signal.aborted && dialog.open) status.textContent = words(err);
    } finally {
      if (listing === own) listing = null;
      if (alive) {
        timer = setTimeout(load, pending ? 150 : dialog.open && selected ? HISTORY_POLL_MS : POLL_MS);
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
      const terminal = ['completed', 'failed', 'errored', 'stopped', 'shutdown', 'interrupted'];
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
  dialog.addEventListener('close', () => {
    request?.abort(); selected = null; historyVersion = null; history.replaceChildren();
  });
  const visibility = () => {
    if (document.hidden) { clearTimeout(timer); listing?.abort(); request?.abort(); }
    else load();
  };
  document.addEventListener('visibilitychange', visibility);
  const ticker = setInterval(() => {
    if (!alive || !dialog.open || document.hidden) return;
    for (const node of list.querySelectorAll('[data-duration]')) {
      const row = known.get(node.dataset.duration);
      if (row && ACTIVE.includes(row.status)) node.textContent = `Длительность: ${duration(row)}`;
    }
  }, 1000);
  load();
  return { button, refresh: schedule, update, open(row) { dialog.showModal(); paint(); openChild(row); load(); },
    unmount() {
      alive = false; request?.abort(); listing?.abort(); clearTimeout(timer); clearInterval(ticker);
      document.removeEventListener('visibilitychange', visibility);
      known.clear(); dialog.remove();
    } };
}
