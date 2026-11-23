/* Самопроверка и журнал моста в сведениях компьютера (мост 0.47.0): по кнопке, только пока открыты сведения. */
import { api } from './api.js';
import { tr } from './i18n.js';
import { el, words } from './ui.js';

const checks = {
  git: 'Git', claude: 'Claude Code', codex: 'Codex', opencode: 'OpenCode', deepseek: 'DeepSeek Harness',
  projects: 'Папки проектов', home: 'Запись в каталог моста', link: 'Канал со студией',
  desktop: 'Удалённый рабочий стол', disk: 'Свободное место',
};
const codes = {
  missing: 'Не найден', timeout: 'Не ответил вовремя', error: 'Ошибка проверки', node: 'Нужен Node.js новее',
  login: 'Нет входа', none: 'Не настроены', unreadable: 'Нет доступа', read_only: 'Нет записи',
  disabled: 'Выключен в окне моста', unsupported: 'Не поддерживается', screen: 'Нет разрешения на запись экрана',
  input: 'Нет разрешения на управление вводом', low: 'Мало места',
};

function checkWords(item) {
  const parts = [codes[item.code] ? tr(codes[item.code])
    : item.state === 'ok' ? tr('В порядке') : tr('Ошибка проверки')];
  if (item.version) parts.push(item.version);
  if (item.login === 'yes' || (item.login === 'unknown' && item.code !== 'login')) {
    parts.push(tr(item.login === 'yes' ? 'вход есть' : 'вход не проверен'));
  }
  if (item.total) parts.push(tr('доступно {0} из {1}', item.count ?? 0, item.total));
  if (Number.isFinite(item.free)) parts.push(tr('свободно {0} ГиБ', (item.free / 1073741824).toFixed(1)));
  return parts.join(' · ');
}

export function diagnosticsView({ locked }) {
  const host = el('section', { class: 'code-diagnostics', 'aria-live': 'polite' });
  let agent;
  const running = new Map();  // путь → AbortController: самопроверка и журнал не отменяют друг друга

  function clear() {
    for (const controller of running.values()) controller.abort();
    running.clear();
    agent = null;
    host.replaceChildren();
  }

  async function request(path, area, button, paint) {
    const target = agent;
    running.get(path)?.abort();
    const controller = new AbortController();
    running.set(path, controller);
    const { signal } = controller;
    const mine = () => running.get(path) === controller && agent === target;
    button.disabled = true;
    area.replaceChildren(el('p', { class: 'muted small' }, tr('Проверяю…')));
    try {
      const data = await api('GET', `/api/code/agents/${target.id}/${path}`, undefined, { signal });
      if (mine()) paint(data);
    } catch (err) {
      if (!mine() || signal.aborted) return;
      if (err.code === 'closed') locked(target.id);
      else area.replaceChildren(el('p', { class: 'error small' }, words(err)));
    } finally {
      if (mine()) { running.delete(path); button.disabled = false; }
    }
  }

  function checkSection(target) {
    const area = el('div', { id: 'code-self-check-result' });
    const button = el('button', { type: 'button', class: 'btn small', id: 'code-self-check' }, tr('Проверить'));
    button.addEventListener('click', () => request('self-check', area, button, (data) => {
      const list = el('dl', { class: 'code-self-check' });
      for (const item of data.items) {
        list.append(el('div', { 'data-state': item.state, 'data-check': item.id },
          el('dt', {}, tr(checks[item.id])), el('dd', {}, checkWords(item))));
      }
      area.replaceChildren(list);
    }));
    return el('div', { class: 'code-diagnostics-part' },
      el('div', { class: 'code-software-heading' }, el('h3', {}, tr('Самопроверка')), target.self_check ? button : null),
      target.self_check ? area : el('p', { class: 'muted small' }, tr('Обновите мост для самопроверки и журнала.')));
  }

  function logSection(target) {
    const area = el('div', { id: 'code-agent-log-result' });
    const button = el('button', { type: 'button', class: 'btn small', id: 'code-agent-log' }, tr('Показать журнал'));
    button.addEventListener('click', () => request('log', area, button, (data) => {
      const text = data.lines.join('\n');
      const save = el('button', { type: 'button', class: 'btn small', id: 'code-agent-log-save' }, tr('Скачать'));
      save.addEventListener('click', () => {
        const href = URL.createObjectURL(new Blob([text + '\n'], { type: 'text/plain' }));
        const link = el('a', { href, download: 'agent.log' });
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(href), 60000);
      });
      area.replaceChildren(data.lines.length
        ? el('pre', { class: 'code-agent-log', tabindex: '0', 'aria-label': tr('Журнал агента') }, text)
        : el('p', { class: 'muted small' }, tr('Журнал пуст')), ...(data.lines.length ? [save] : []));
    }));
    return el('div', { class: 'code-diagnostics-part' },
      el('div', { class: 'code-software-heading' }, el('h3', {}, tr('Журнал агента')), button),
      el('p', { class: 'muted small' }, tr('Последние 200 строк: только коды, числа и id, без путей и текстов.')), area);
  }

  return { host, clear, show(target) {
    clear();
    agent = target;
    host.append(checkSection(target));
    if (target.agent_log) host.append(logSection(target));
  } };
}
