/* «Входящие» раздела «Агенты» (D2): лист по нажатию на значок-счётчик или точку «Агентов» в рельсе и нижней панели
 * (code-activity.js; само нажатие на вкладку по-прежнему открывает раздел). Три группы по всем компьютерам — ждёт
 * ответа, работает, завершено и не прочитано — из той же сводки GET /api/code/activity: просьбы к мостам фоновые,
 * срок PIN не продлевают; закрытые PIN и не подключённые компьютеры в сводку не входят (лист говорит об этом).
 * Строка — ссылка сразу в чат #/code/<aid>/<pid>/<chat>: он открывается на конце ленты, ожидающий вопрос — ещё и
 * плашкой над полем (code-chat.js). Защищённый раздел «Агенты» сначала отпирается (lock.js ensureSectionOpen), отказ —
 * лист не открывается. Имена компьютеров — GET /api/agents, проектов — GET …/projects с X-Studio-Passive; только память
 * страницы. Текст машины — только textContent.
 * Клавиатура и чтение с экрана: вкладка «Агенты» остаётся ссылкой на раздел (кнопку внутрь ссылки не вложить), те же
 * три группы с переходом в чат есть в «В работе» (кнопка рельса); сам лист — нативный <dialog> с фокусом и Escape. */
import { api } from './api.js';
import { allCodeActivity, codeActivityState, refreshCodeActivity } from './code-activity.js';
import { ENGINE_NAMES } from './code-options.js';
import { ensureSectionOpen } from './lock.js';
import { el } from './ui.js';

const GROUPS = [['asking', 'Ждёт ответа'], ['running', 'Работает'], ['unread', 'Завершено, не прочитано']];
const agentNames = new Map();     // aid → имя компьютера
const projectNames = new Map();   // aid → Map(pid → имя проекта)
let dialog = null;
let opener = null;
document.addEventListener('studio-code-clear', () => projectNames.clear());

function row(item) {
  const kind = codeActivityState(item);
  const title = item.title || 'Сессия';
  const where = [agentNames.get(item.aid) || 'Компьютер', projectNames.get(item.aid)?.get(item.project) || 'Проект',
    ENGINE_NAMES[item.engine]].filter(Boolean).join(' · ');
  return el('li', {}, el('a', { class: 'code-inbox-row', href: `#/code/${item.aid}/${item.project}/${item.chat}`,
    'data-chat': item.chat, 'data-state': kind },
  el('span', { class: 'code-inbox-title', title }, title), el('span', { class: 'code-inbox-where' }, where)));
}

function paint() {
  const focused = document.activeElement?.closest?.('#code-inbox .code-inbox-row')?.dataset.chat;
  const items = allCodeActivity();
  let total = 0;
  for (const section of dialog.querySelectorAll('.code-inbox-group')) {
    const rows = items.filter((item) => codeActivityState(item) === section.dataset.kind)
      .sort((a, b) => (b.finished || 0) - (a.finished || 0));
    total += rows.length;
    section.hidden = !rows.length;
    section.querySelector('ul').replaceChildren(...rows.map(row));
  }
  dialog.querySelector('#code-inbox-empty').hidden = total > 0;
  if (focused) dialog.querySelector(`.code-inbox-row[data-chat="${CSS.escape(focused)}"]`)?.focus();
}

async function names() {
  try {
    const out = await api('GET', '/api/agents');
    for (const agent of out.items || []) agentNames.set(agent.id, agent.name);
  } catch { /* без имён — «Компьютер» */ }
  const aids = [...new Set(allCodeActivity().map((item) => item.aid))].filter((aid) => !projectNames.has(aid));
  await Promise.all(aids.map(async (aid) => {
    try {
      const out = await api('GET', `/api/code/agents/${aid}/projects`, undefined, { headers: { 'x-studio-passive': '1' } });
      projectNames.set(aid, new Map((out.items || []).map((p) => [p.pid, p.name])));
    } catch { /* закрыт или не на связи — «Проект» */ }
  }));
  if (dialog?.open) paint();
}

function build() {
  const close = el('button', { type: 'button', id: 'code-inbox-close', class: 'btn-link' }, 'Закрыть');
  const groups = GROUPS.map(([kind, title]) => el('section', { class: 'code-inbox-group', 'data-kind': kind, hidden: true },
    el('h3', {}, title), el('ul', { class: 'code-inbox-list' })));
  dialog = el('dialog', { id: 'code-inbox', class: 'code-inbox', 'aria-labelledby': 'code-inbox-title' },
    el('div', { class: 'code-inbox-sheet' },
      el('div', { class: 'code-inbox-head' }, el('h2', { id: 'code-inbox-title' }, 'Входящие'), close),
      ...groups,
      el('p', { id: 'code-inbox-empty', class: 'muted', hidden: true }, 'Сейчас ничего не ждёт вашего внимания.'),
      el('p', { class: 'muted small code-inbox-note' }, 'Компьютеры, закрытые PIN или не на связи, здесь не показаны.'),
      el('a', { id: 'code-inbox-all', class: 'btn code-inbox-all', href: '#/code' }, 'Открыть «Агенты»')));
  document.body.append(dialog);
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) {           // мимо листа
      dialog.close();
      return;
    }
    const link = event.target.closest('a[href]');
    if (!link) return;
    const same = location.hash === link.getAttribute('href');
    dialog.close();
    if (same) {                              // уже здесь — показать заново (как «В работе»)
      event.preventDefault();
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    }
  });
  dialog.addEventListener('close', () => {
    if (opener?.isConnected && !location.hash.startsWith('#/code/')) opener.focus({ preventScroll: true });
    opener = null;
  });
  document.addEventListener('studio-code-snapshot', () => { if (dialog.open) paint(); });
}

/** Открыть лист; from — вкладка «Агенты», куда вернуть фокус. */
export async function openInbox(from = null) {
  try {
    await ensureSectionOpen('code');
  } catch {
    return;                                  // раздел не отперли — данных не показываем
  }
  if (!dialog) build();
  if (dialog.open) return;
  opener = from;
  paint();
  dialog.showModal();
  refreshCodeActivity();
  names();
}
