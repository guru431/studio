/* Сведения компьютера живут только в открытом окне; PIN проверяет обычный маршрут моста. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { el, words } from './ui.js';

const bytes = (value) => Number.isFinite(value)
  ? `${(value / 1073741824).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} ГиБ` : 'Нет данных';
const date = (value) => Number.isFinite(value) ? dateTime(value) : 'Нет данных';

export function computerView(host, { locked }) {
  let agent = null;
  let controller = null;
  let generation = 0;
  const title = el('h2', { id: 'code-computer-title' });
  const content = el('div', { class: 'code-computer-content', 'aria-live': 'polite' });
  const refresh = el('button', { type: 'button', class: 'btn', id: 'code-computer-refresh' }, 'Обновить');
  const close = el('button', { type: 'button', class: 'btn' }, 'Закрыть');
  const dialog = el('dialog', { id: 'code-computer-dialog', class: 'dialog code-computer-dialog',
    'aria-labelledby': 'code-computer-title' }, title, content, el('div', { class: 'actions' }, refresh, close));
  host.append(dialog);
  close.addEventListener('click', () => dialog.close());
  function clear() {
    generation += 1;
    controller?.abort();
    content.replaceChildren();
    content.removeAttribute('aria-busy');
  }
  dialog.addEventListener('close', clear);

  function paint(data) {
    const details = el('dl', { class: 'code-computer-details' });
    const row = (label, ...values) => details.append(el('dt', {}, label),
      el('dd', {}, ...values.map((value) => el('div', {}, value || 'Нет данных'))));
    row('Имя компьютера', data.hostname);
    row('Имя пользователя', data.username);
    row('Внешний IP-адрес', data.external_ip);
    row('Внутренние IP-адреса', ...(data.internal_ips.length ? data.internal_ips : [null]));
    row('Операционная система', data.os);
    const memory = data.memory;
    row('Оперативная память', memory
      ? `Используется ${bytes(memory.used)} из ${bytes(memory.total)} (${Math.round(memory.used / memory.total * 100)}%)`
      : null);
    if (memory) row('Доступно памяти', `${bytes(memory.available)} из ${bytes(memory.total)}`);
    row('Свободное место на дисках', ...(data.disks.length ? data.disks.map((disk) =>
      disk.total === null ? `${disk.path} — нет данных`
        : `${disk.path} — ${disk.free === null ? 'нет данных о свободном месте' : `свободно ${bytes(disk.free)}`} из ${bytes(disk.total)}`) : [null]));
    row('Папки с проектами', ...(data.projects_roots.length ? data.projects_roots : ['Не настроены']));
    content.replaceChildren(details, el('p', { class: 'muted small' }, `Обновлено: ${date(data.checked_at)}`));
  }

  async function load() {
    controller?.abort();
    const mine = ++generation;
    const target = agent;
    refresh.disabled = !target.online || !target.computer_info;
    if (!target.online || !target.computer_info) {
      content.replaceChildren(el('p', { class: 'muted' }, !target.online
        ? 'Компьютер не на связи — включите его и запустите мост для просмотра сведений.'
        : 'Обновите агент на компьютере для просмотра сведений.'));
      return;
    }
    controller = new AbortController();
    const signal = controller.signal;
    refresh.disabled = true;
    content.setAttribute('aria-busy', 'true');
    content.replaceChildren(el('p', { class: 'muted' }, 'Получаю сведения компьютера…'));
    try {
      const data = await api('GET', `/api/code/agents/${target.id}/info`, undefined, { signal });
      if (mine === generation && dialog.open) paint(data);
    } catch (err) {
      if (mine !== generation || signal.aborted || !dialog.open) return;
      if (err.code === 'closed') { dialog.close(); locked(target.id); }
      else content.replaceChildren(el('p', { class: 'error' }, words(err)));
    } finally {
      if (mine === generation) { refresh.disabled = false; content.removeAttribute('aria-busy'); }
    }
  }
  refresh.addEventListener('click', load);
  return {
    show(value) {
      agent = value;
      title.textContent = `${agent.name} · сведения`;
      if (!dialog.open) dialog.showModal();
      load();
    },
    unmount() { clear(); dialog.remove(); },
  };
}
