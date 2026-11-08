import { api } from './api.js';
import { button, el, openDialog } from './ui.js';

export function visualizationsView(host, { aid, pid, supported }) {
  const url = `/api/code/agents/${aid}/projects/${pid}/visualization`;
  let dialog = null;
  let pending = null;

  function close() {
    pending?.abort();
    pending = null;
    dialog?.close();
    dialog?.remove();
    dialog = null;
  }

  async function open(reference) {
    close();
    const status = el('p', { class: 'muted', role: 'status' }, 'Загружаю визуализацию…');
    const frame = el('iframe', { class: 'code-visualization-frame', title: reference.title,
      sandbox: 'allow-scripts', referrerpolicy: 'no-referrer', hidden: true });
    const done = button('Закрыть', { class: 'btn' }, close);
    const current = openDialog(host, { id: 'code-visualization', title: reference.title,
      body: [done, status, frame], cancelText: 'Закрыть' });
    current.querySelector('.actions').remove();
    current.classList.add('code-visualization-dialog');
    current.classList.toggle('code-visualization-wide', reference.mode === 'wide');
    dialog = current;
    const controller = new AbortController();
    pending = controller;
    current.addEventListener('close', () => {
      controller.abort();
      frame.removeAttribute('src');
      if (dialog === current) dialog = null;
    }, { once: true });
    if (!supported) {
      status.textContent = 'Обновите агент на компьютере, чтобы открывать визуализации';
      return;
    }
    try {
      const { token } = await api('POST', url, { source: reference.path }, { signal: controller.signal });
      if (controller.signal.aborted || dialog !== current) return;
      if (!/^[a-f0-9]{32}$/.test(token)) throw new Error('Не удалось открыть визуализацию');
      frame.addEventListener('load', () => { status.hidden = true; }, { once: true });
      frame.src = `${url}/${token}`;
      frame.hidden = false;
    } catch (error) {
      if (controller.signal.aborted || dialog !== current) return;
      status.className = 'error';
      status.textContent = error.message;
    }
  }

  document.addEventListener('studio-code-clear', close);
  return { open, unmount() { close(); document.removeEventListener('studio-code-clear', close); } };
}
