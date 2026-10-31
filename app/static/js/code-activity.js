/* Число работающих сессий на значке «Агенты», в том числе вне раздела.
 * Снимки — только от доступных мостов с project_activity; PIN не открываем.
 * Фоновый fetch не вызывает экран отпирания защищённого раздела. Скрытая страница
 * и простой более 30 минут останавливают опрос, как у панели проектов. */
import { me } from './api.js';

const POLL_MS = 20000;
const activity = new Map();
const revisions = new Map();
let badge;
let tab;
let timer;
let requested = false;
let lastAction = Date.now();

function paint() {
  if (!badge) return;
  const total = [...activity.values()].reduce((sum, items) => sum + items.size, 0);
  badge.textContent = String(total);
  badge.hidden = total === 0;
  const label = total ? `Агенты · Запущенных сессий: ${total}` : 'Агенты';
  tab.setAttribute('aria-label', label);
  tab.title = label;
}

export function setCodeActivity(aid, items) {
  aid = Number(aid);
  revisions.set(aid, (revisions.get(aid) || 0) + 1);
  activity.set(aid, new Map(items.filter((item) => item.running).map((item) => [item.chat, item])));
  paint();
}

function active() {
  return document.visibilityState === 'visible' && !document.documentElement.classList.contains('shrouded')
    && Date.now() - lastAction < Math.min(1800000, (me?.computer_pin_timeout || 1800) * 1000);
}

async function read(path) {
  const response = await fetch(path, { credentials: 'same-origin', signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error('Статус недоступен');
  return response.json();
}

async function refresh() {
  if (requested) return;
  if (!active()) { activity.clear(); paint(); return; }
  clearTimeout(timer);
  timer = null;
  requested = true;
  const before = new Map(revisions);
  try {
    const { items } = await read('/api/code/activity');
    for (const aid of activity.keys()) {
      if (!items.some((agent) => agent.aid === aid) && revisions.get(aid) === before.get(aid)) activity.delete(aid);
    }
    for (const agent of items) {
      if (revisions.get(agent.aid) === before.get(agent.aid)) setCodeActivity(agent.aid, agent.items);
    }
  } catch {
    for (const aid of activity.keys()) {
      if (revisions.get(aid) === before.get(aid)) activity.delete(aid);
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
  tab = badge.closest('a');
  document.addEventListener('studio-code-activity', ({ detail: item }) => {
    revisions.set(item.aid, (revisions.get(item.aid) || 0) + 1);
    if (!activity.has(item.aid)) activity.set(item.aid, new Map());
    if (item.running) activity.get(item.aid).set(item.chat, item);
    else activity.get(item.aid).delete(item.chat);
    paint();
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
