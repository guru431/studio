/* Версии и обновления программ компьютера; состояние существует только пока открыты сведения. */
import { api } from './api.js';
import { tr } from './i18n.js';
import { el, words } from './ui.js';

const names = { codex: 'Codex', claude: 'Claude Code', opencode: 'OpenCode' };
const reasons = {
  not_installed: 'Не установлено', bridge: 'Обновляется вместе с мостом',
  manager: 'Установщик не найден на компьютере', manual: 'Обновите программу на компьютере',
  check: 'Не удалось проверить',
};
const states = {
  waiting: 'Обновление начнётся после завершения активных заданий', updating: 'Устанавливаю обновление…',
  done: 'Обновление установлено, версия проверена', error: 'Обновить не удалось. Повторите проверку версий и попробуйте ещё раз.',
  cancelled: 'Обновление отменено',
};

export function softwareView({ locked }) {
  const host = el('section', { class: 'code-software', 'aria-live': 'polite' });
  let agent, controller, timer, generation = 0;

  function clear() {
    generation += 1;
    clearTimeout(timer);
    controller?.abort();
    agent = null;
    host.replaceChildren();
  }

  function paint(data) {
    const busy = ['waiting', 'updating'].includes(data.update.state);
    const rows = data.items.map((item) => {
      const label = names[item.id];
      const current = item.reason === 'check' ? tr('Нет данных')
        : item.installed ? item.version || tr('Нет данных') : tr('Не установлено');
      const versionState = item.installed && item.version && item.latest && item.reason !== 'check'
        ? item.update_available ? 'outdated' : 'current' : 'unknown';
      const details = el('div', { class: 'code-software-versions' },
        el('strong', {}, label), el('span', { class: 'code-software-version', 'data-state': versionState }, tr('Установлено: {0}', current)),
        el('span', {}, tr('Актуальная версия: {0}', item.latest || tr('Не удалось проверить'))));
      if (item.source === 'vscode') details.append(el('span', { class: 'muted small' }, tr('Расширение VS Code')));
      if (item.cli_version && item.cli_version !== item.version) {
        details.append(el('span', { class: 'muted small' }, tr('Версия CLI: {0}', item.cli_version)));
      }
      if (item.installed && item.latest && item.version && !item.update_available) {
        details.append(el('span', { class: 'muted small' }, tr('Установлена актуальная версия')));
      }
      if (reasons[item.reason]) details.append(el('span', { class: 'muted small' }, tr(reasons[item.reason])));
      const update = el('button', { type: 'button', class: 'btn small',
        'data-software-update': item.id, 'aria-label': tr('Обновить {0}', label) }, tr('Обновить'));
      update.disabled = busy || !item.can_update;
      update.addEventListener('click', () => load('POST', `/${item.id}/update`));
      return el('div', { class: 'code-software-row' }, details, update);
    });
    host.replaceChildren(el('h3', {}, tr('Программы')), ...rows);
    if (states[data.update.state]) {
      host.append(el('p', { class: data.update.state === 'error' ? 'error small' : 'muted small' },
        `${names[data.update.tool] || ''} · ${tr(states[data.update.state])}`));
    }
    if (data.update.state === 'waiting') {
      const cancel = el('button', { type: 'button', class: 'btn small', 'data-software-cancel': '' }, tr('Отменить обновление'));
      cancel.addEventListener('click', () => load('POST', '/cancel'));
      host.append(cancel);
    }
    if (busy) timer = setTimeout(() => load(), 2000);
  }

  async function load(method = 'GET', path = '') {
    const target = agent;
    if (!target) return;
    clearTimeout(timer);
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    const mine = ++generation;
    host.querySelectorAll('button').forEach((button) => { button.disabled = true; });
    try {
      const data = await api(method, `/api/code/agents/${target.id}/software${path}`, undefined, { signal });
      if (mine === generation && agent) paint(data);
    } catch (err) {
      if (mine !== generation || signal.aborted || !agent) return;
      if (err.code === 'closed') locked(target.id);
      else {
        host.replaceChildren(el('h3', {}, tr('Программы')), el('p', { class: 'error small' }, words(err)));
        const retry = el('button', { type: 'button', class: 'btn small' }, tr('Повторить'));
        retry.addEventListener('click', () => load('GET', '?refresh=true'));
        host.append(retry);
      }
    }
  }

  return { host, clear, show(target, refresh = false) {
    clear();
    agent = target;
    if (!target.software_versions) {
      host.append(el('p', { class: 'muted small' }, tr('Обновите мост для просмотра версий и обновления программ.')));
      return;
    }
    host.append(el('p', { class: 'muted small' }, tr('Проверяю версии программ…')));
    load('GET', refresh ? '?refresh=true' : '');
  } };
}
