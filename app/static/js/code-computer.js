import { locale, tr } from './i18n.js';
/* Сведения компьютера живут только в открытом окне; PIN проверяет обычный маршрут моста. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { el, words } from './ui.js';

const bytes = (value) => Number.isFinite(value)
  ? tr("{0} ГиБ", (value / 1073741824).toLocaleString(locale(), { maximumFractionDigits: 1 })) : tr("Нет данных");
const date = (value) => Number.isFinite(value) ? dateTime(value) : tr("Нет данных");

export function computerView(host, { locked }) {
  let agent = null;
  let controller = null;
  let generation = 0;
  const title = el('h2', { id: 'code-computer-title' });
  const content = el('div', { class: 'code-computer-content', 'aria-live': 'polite' });
  const refresh = el('button', { type: 'button', class: 'btn', id: 'code-computer-refresh' }, tr("Обновить"));
  const close = el('button', { type: 'button', class: 'btn' }, tr("Закрыть"));
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
      el('dd', {}, ...values.map((value) => el('div', {}, value || tr("Нет данных")))));
    row(tr("Имя компьютера"), data.hostname);
    row(tr("Имя пользователя"), data.username);
    row(tr("Внешний IP-адрес"), data.external_ip);
    row(tr("Внутренние IP-адреса"), ...(data.internal_ips.length ? data.internal_ips : [null]));
    row(tr("Операционная система"), data.os);
    const memory = data.memory;
    row(tr("Оперативная память"), memory
      ? tr("Используется {0} из {1} ({2}%)", bytes(memory.used), bytes(memory.total), Math.round(memory.used / memory.total * 100))
      : null);
    if (memory) row(tr("Доступно памяти"), tr("{0} из {1}", bytes(memory.available), bytes(memory.total)));
    row(tr("Свободное место на дисках"), ...(data.disks.length ? data.disks.map((disk) =>
      disk.total === null ? tr("{0} — нет данных", disk.path)
        : tr("{0} — {1} из {2}", disk.path, disk.free === null ? tr("нет данных о свободном месте") : tr("свободно {0}", bytes(disk.free)), bytes(disk.total))) : [null]));
    row(tr("Папки с проектами"), ...(data.projects_roots.length ? data.projects_roots : [tr("Не настроены")]));
    content.replaceChildren(details, el('p', { class: 'muted small' }, tr("Обновлено: {0}", date(data.checked_at))));
  }

  async function load() {
    controller?.abort();
    const mine = ++generation;
    const target = agent;
    refresh.disabled = !target.online || !target.computer_info;
    if (!target.online || !target.computer_info) {
      content.replaceChildren(el('p', { class: 'muted' }, !target.online
        ? tr("Компьютер не на связи — включите его и запустите мост для просмотра сведений.")
        : tr("Обновите агент на компьютере для просмотра сведений.")));
      return;
    }
    controller = new AbortController();
    const signal = controller.signal;
    refresh.disabled = true;
    content.setAttribute('aria-busy', 'true');
    content.replaceChildren(el('p', { class: 'muted' }, tr("Получаю сведения компьютера…")));
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
      title.textContent = tr("{0} · сведения", agent.name);
      if (!dialog.open) dialog.showModal();
      load();
    },
    unmount() { clear(); dialog.remove(); },
  };
}
