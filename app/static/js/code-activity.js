import { tr } from './i18n.js';
/* Число работающих сессий на значке «Агенты», в том числе вне раздела.
 * Снимки — только от доступных мостов с project_activity; PIN не открываем.
 * Фоновый fetch не вызывает экран отпирания защищённого раздела. Скрытая страница
 * и простой более 30 минут останавливают опрос, как у панели проектов. */
import { me } from './api.js';
import { NAMES } from './sections-meta.js';

const POLL_MS = 20000;
const activity = new Map();
const revisions = new Map();
let badge;
let dot;
let tab;
let timer;
let requested = false;
let lastAction = Date.now();

function paint() {
  if (!badge) return;
  const items = allCodeActivity();
  const total = items.filter((item) => item.running).length;
  const asking = items.filter((item) => item.asking).length;
  const unread = items.filter((item) => item.unread && !item.running).length;
  badge.textContent = String(total);
  badge.hidden = total === 0;
  badge.dataset.asking = asking ? '1' : '';
  // Точка — завершённые и не прочитанные. Вся вкладка, включая метки, открывает рабочее место.
  dot.hidden = !unread;
  const parts = [total ? tr("Запущенных сессий: {0}", total) : '', asking ? tr("ждут ответа: {0}", asking) : '',
    unread ? tr("не прочитано: {0}", unread) : ''].filter(Boolean);
  const label = parts.length ? `${NAMES.code} · ${parts.join(' · ')}` : NAMES.code;
  tab.setAttribute('aria-label', label);
  tab.title = label;
}

export function setCodeActivity(aid, items) {
  aid = Number(aid);
  revisions.set(aid, (revisions.get(aid) || 0) + 1);
  items = items.filter((item) => item.running || item.asking || item.unread)
    .map((item) => ({ ...item, project: item.project ?? item.pid }));
  activity.set(aid, new Map(items.map((item) => [item.chat, item])));
  paint();
  document.dispatchEvent(new CustomEvent('studio-code-snapshot', { detail: { aid, items } }));
}

export function codeActivity(aid) {
  return [...(activity.get(Number(aid))?.values() || [])].map((item) => ({ ...item, project: item.project ?? item.pid }));
}
export function allCodeActivity() {
  return [...activity].flatMap(([aid, items]) => [...items.values()].map((item) => ({ ...item, aid })));
}
export function codeActivityState(item) {
  return item.asking ? 'asking' : item.running ? 'running' : 'unread';
}
export const CODE_ACTIVITY_LABELS = { running: tr("Работает"), asking: tr("Требует вашего действия"), unread: tr("Завершена · не прочитана") };
export function refreshCodeActivity() { refresh(); }
function clearActivity() {
  for (const aid of activity.keys()) setCodeActivity(aid, []);
}
document.addEventListener('studio-code-clear', () => { clearActivity(); revisions.clear(); });

function active() {
  return document.visibilityState === 'visible' && !document.documentElement.classList.contains('shrouded')
    && Date.now() - lastAction < Math.min(1800000, (me?.computer_pin_timeout || 1800) * 1000);
}

async function read(path) {
  const response = await fetch(path, { credentials: 'same-origin', signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error(tr("Статус недоступен"));
  return response.json();
}

async function refresh() {
  if (requested) return;
  if (!active()) { clearActivity(); return; }
  clearTimeout(timer);
  timer = null;
  requested = true;
  const before = new Map(revisions);
  try {
    const { items } = await read('/api/code/activity');
    for (const aid of activity.keys()) {
      if (!items.some((agent) => agent.aid === aid) && revisions.get(aid) === before.get(aid)) setCodeActivity(aid, []);
    }
    for (const agent of items) {
      if (revisions.get(agent.aid) === before.get(agent.aid)) setCodeActivity(agent.aid, agent.items);
    }
  } catch {
    for (const aid of activity.keys()) {
      if (revisions.get(aid) === before.get(aid)) setCodeActivity(aid, []);
    }
  } finally {
    requested = false;
    paint();
    if (active()) timer = setTimeout(() => { timer = null; refresh(); }, POLL_MS);
  }
}

export function setupCodeActivity() {
  if (badge) return;
  badge = document.getElementById('code-running-count');
  dot = document.getElementById('code-inbox-dot');
  tab = badge.closest('a');
  document.addEventListener('studio-code-activity', ({ detail: item }) => {
    const aid = Number(item.aid);
    const items = new Map(activity.get(aid));
    items.set(item.chat, { ...items.get(item.chat), ...item, project: item.project ?? item.pid });
    setCodeActivity(aid, [...items.values()]);
  });
  document.addEventListener('studio-code-seen', ({ detail: item }) => {
    const aid = Number(item.aid);
    const items = new Map(activity.get(aid));
    const current = items.get(item.chat);
    if (current?.attention !== item.attention) return;
    items.set(item.chat, { ...current, unread: false });
    setCodeActivity(aid, [...items.values()]);
  });
  function resume() {
    lastAction = Date.now();
    refresh();
  }
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    timer = null;
    if (document.visibilityState === 'visible') resume();
  });
  for (const event of ['pointerdown', 'keydown']) document.addEventListener(event, () => {
    lastAction = Date.now();
    if (!timer && !requested) refresh();
  }, { passive: true });
  window.addEventListener('online', resume);
  paint();
  refresh();
}
