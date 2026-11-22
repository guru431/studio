/* Версии и обновления программ компьютера; состояние существует только пока открыты сведения. */
import { api } from './api.js';
import { tr } from './i18n.js';
import { el, words } from './ui.js';

const names = { codex: 'Codex', claude: 'Claude Code', opencode: 'OpenCode', deepseek: 'DeepSeek Harness' };
const reasons = {
  not_installed: 'Не установлено', bridge: 'Обновляется вместе с мостом',
  manager: 'Установщик не найден на компьютере', manual: 'Обновите программу на компьютере',
  check: 'Не удалось проверить', node: 'Нужен Node.js новее установленного',
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
      const install = item.id === 'claude' && item.source === 'sdk' && item.can_update;
      const current = item.reason === 'check' ? tr('Нет данных')
        : item.installed ? item.version || tr('Нет данных') : tr('Не установлено');
      const versionState = item.installed && item.version && item.latest && item.reason !== 'check'
        ? item.update_available ? 'outdated' : 'current' : 'unknown';
      const details = el('div', { class: 'code-software-versions' },
        el('div', { class: 'code-software-numbers' },
          el('span', { class: 'code-software-version', 'data-state': versionState },
            item.installed || item.reason === 'check' ? tr('Установлено: {0}', current) : current),
          el('span', { class: 'muted' }, versionState === 'current' ? tr('Установлена актуальная версия')
            : tr('Актуальная версия: {0}', item.latest || tr('Не удалось проверить')))));
      const meta = el('div', { class: 'code-software-meta muted small' });
      if (item.source === 'vscode') meta.append(el('span', {}, tr('Расширение VS Code')));
      if (item.cli_version && item.cli_version !== item.version) {
        meta.append(el('span', {}, tr('Версия CLI: {0}', item.cli_version)));
      }
      if (meta.childElementCount) details.append(meta);
      if (reasons[item.reason] && item.reason !== 'not_installed') {
        details.append(el('span', { class: 'muted small' }, tr(reasons[item.reason])));
      }
      if (install) details.append(el('span', { class: 'muted small' }, tr('Будет установлен отдельный CLI Claude Code.')));
      const update = el('button', { type: 'button', class: 'btn small',
        'data-software-update': item.id, 'aria-label': install ? tr('Установить {0}', label) : tr('Обновить {0}', label) },
      install ? tr('Установить') : tr('Обновить'));
      update.disabled = busy || !item.can_update;
      update.addEventListener('click', () => load('POST', `/${item.id}/update`));
      return el('div', { class: 'code-software-row' },
        el('div', { class: 'code-software-heading' }, el('strong', {}, label), update), details);
    });
    const all = el('button', { type: 'button', class: 'btn small', id: 'code-software-update-all',
      disabled: busy || !data.items.some((item) => item.can_update && item.source !== 'sdk') }, tr('Обновить все агенты'));
    all.addEventListener('click', () => load('POST', '/all/update'));
    host.replaceChildren(el('div', { class: 'code-software-heading' }, el('h3', {}, tr('Программы')),
      agent.software_update_all ? all : null), ...rows);
    if (states[data.update.state]) {
      const title = data.update.tool === 'all' ? tr('Все агенты') : names[data.update.tool];
      host.append(el('p', { class: data.update.state === 'error' ? 'error small' : 'muted small' },
        `${title || ''} · ${tr(states[data.update.state])}`));
      if (data.update.results && !busy) {
        for (const [name, result] of Object.entries(data.update.results)) {
          host.append(el('p', { class: result === 'error' ? 'error small' : 'muted small' },
            `${names[name]} · ${tr(states[result])}`));
        }
      }
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
