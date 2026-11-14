import { tr } from './i18n.js';
/* Изменять можно только сообщения, которыми ещё владеет очередь моста. */
import { api } from './api.js';
import { el } from './ui.js';

export function queueView(host, { endpoint, failed, request = api }) {
  const box = el('section', { id: 'code-queue', class: 'code-queue', 'aria-label': tr("Очередь заданий"), hidden: true });
  host.append(box);
  let items = [], foreign = false, pending = false, editing = null, alive = true, paused = false, epoch = 0;

  async function resume() {
    const mine = epoch;
    const data = await request('POST', endpoint('/queue/pause'), { paused: false });
    if (!alive || mine !== epoch) return;
    items = data.items || [];
    paused = false;
  }

  async function edit(item) {
    const mine = epoch;
    pending = true;
    paint();
    try {
      const data = await request('POST', endpoint('/queue/pause'), { paused: true });
      if (!alive || mine !== epoch) return;
      items = data.items || [];
      paused = true;
      const current = items.find((row) => row.id === item.id);
      if (current) editing = { id: item.id, version: current.version || 1, value: current.input_text ?? current.text };
    } catch (err) {
      if (alive && mine === epoch) failed(err);
    } finally {
      pending = false;
      if (alive && mine === epoch) { paint(); box.querySelector('textarea')?.focus(); }
    }
  }

  async function change(item, value) {
    const mine = epoch;
    pending = true;
    paint();
    try {
      const data = await request('POST', endpoint('/queue'), { request_id: item.id, version: item.version || 1, text: value });
      if (!alive || mine !== epoch) return;
      items = data.items || [];
      if (paused && items.length) await resume();
      if (!alive || mine !== epoch) return;
      if (!items.length) paused = false;
      editing = null;
    } catch (err) {
      if (!alive || mine !== epoch) return;
      if (err.code === 'gone') items = items.filter((row) => row.id !== item.id);
      failed(err);
    } finally {
      pending = false;
      if (alive && mine === epoch) paint();
    }
  }

  function paint() {
    box.hidden = !items.length;
    host.hidden = box.hidden;
    box.replaceChildren(el('div', { class: 'small muted' }, paused ? tr("Очередь на паузе") : tr("После текущей задачи")));
    if (paused && !editing) {
      const next = el('button', { type: 'button', class: 'btn btn-sm', disabled: pending || foreign }, tr("Продолжить очередь"));
      next.addEventListener('click', async () => {
        try { await resume(); paint(); } catch (err) { failed(err); }
      });
      box.append(next);
    }
    for (const item of items) {
      const row = el('div', { class: 'code-queue-item', 'data-id': item.id });
      if (editing?.id === item.id) {
        const text = el('textarea', { class: 'input code-queue-text', rows: '3', maxlength: '100000',
          'aria-label': tr("Задание из очереди"), disabled: pending || foreign });
        text.value = editing.value;
        text.addEventListener('input', () => { editing.value = text.value; });
        const save = el('button', { type: 'button', class: 'btn btn-sm', disabled: pending || foreign }, tr("Сохранить"));
        save.addEventListener('click', () => change({ ...item, version: editing.version }, editing.value));
        const cancel = el('button', { type: 'button', class: 'btn btn-sm', disabled: pending || foreign }, tr("Отмена"));
        cancel.addEventListener('click', async () => {
          try { await resume(); editing = null; paint(); } catch (err) { failed(err); }
        });
        row.append(text, el('div', { class: 'code-queue-actions' }, save, cancel));
      } else {
        const editButton = el('button', { type: 'button', class: 'btn btn-sm', disabled: pending || foreign || !!editing,
          'aria-label': tr("Изменить ожидающее задание") }, tr("Изменить"));
        editButton.addEventListener('click', () => edit(item));
        const remove = el('button', { type: 'button', class: 'btn btn-sm', disabled: pending || foreign || !!editing,
          'aria-label': tr("Удалить ожидающее задание") }, tr("Удалить"));
        remove.addEventListener('click', () => change(item, null));
        row.append(el('div', { class: 'code-queue-preview' }, item.text),
          el('div', { class: 'code-queue-actions' }, editButton, remove));
      }
      box.append(row);
    }
  }
  return {
    update(next, hold = false) { items = Array.isArray(next) ? next : []; paused = hold; paint(); },
    clear() { epoch++; items = []; paused = false; editing = null; paint(); },
    paused() { return paused; },
    add(item) { if (!items.some((row) => row.id === item.id)) items.push(item); paint(); },
    remove(id) { items = items.filter((row) => row.id !== id); if (editing?.id === id) editing = null; paint(); },
    ownership(value) { if (foreign !== value) { foreign = value; paint(); } },
    unmount() { alive = false; box.remove(); },
  };
}
