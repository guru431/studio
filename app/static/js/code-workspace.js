/* Рабочее место раздела «Claude Code» в учётке (GET/PATCH /api/code/workspace; спека 2026-10-29-studio-code-workspace-design,
 * 6.8): кэш документа { panel, place, projects } на время показа раздела и действия записи.
 * - load() — документ с сервера (вход в раздел, возвращение на экран); get() — кэш (до загрузки — пустой);
 * - act(op, fields) — действие (open, close, place, panel, move): кэш меняется сразу так же, как его поменяет сервер (apply —
 *   те же пределы и та же соседняя активная), запрос — в фоне; ответ сервера заменяет кэш, а действия, на которые
 *   ответа ещё нет, ложатся поверх. Отказ (сеть, 5xx) проглатывается — на экране всё уже сделано; ответы не по порядку
 *   старый документ поверх нового не кладут. GET и PATCH — одной очередью, в порядке действий, даже после reset.
 *   Подписчики зовутся на каждую смену кэша;
 * - tabs(aid, pid), count(aid, pid) — вкладки проекта из кэша; subscribe(fn) → отписка; reset() — уход из раздела.
 * Только id — имён и названий нет и в документе; в хранилище браузера ничего. */
import { api } from './api.js';

const TABS_MAX = 20;        // workspace.TABS_MAX
const PROJECTS_MAX = 100;   // workspace.PROJECTS_MAX
const EMPTY = { panel: null, place: null, projects: [] };
let doc = EMPTY;
let epoch = 0;          // reset(): ответ прежнего показа раздела кэш не трогает
let sent = 0;           // номер последнего запроса
let applied = 0;        // номер запроса, чей ответ лежит в кэше
let waiting = [];       // действия без ответа: { seq, op, fields }
let queue = Promise.resolve(); // чтение и записи идут в порядке действий, в том числе при новом входе в раздел
const listeners = new Set();

/** Действие над документом — как его применит сервер (workspace.py). */
function apply(d, { op, fields: f }) {
  const aid = Number(f.aid);
  if (op === 'panel') return { ...d, panel: aid };
  const found = d.projects.find((p) => p.aid === aid && p.pid === f.pid) || null;
  const rest = d.projects.filter((p) => p !== found);
  if (op === 'place') {
    return { ...d, place: { aid, pid: f.pid, view: f.view }, projects: found ? [found, ...rest] : d.projects };
  }
  if (op === 'open') {
    const tabs = found ? [...found.tabs] : [];
    if (!tabs.includes(f.chat)) tabs.push(f.chat);
    const entry = { aid, pid: f.pid, tabs: tabs.slice(-TABS_MAX), active: f.chat };
    return { ...d, place: { aid, pid: f.pid, view: 'chat' }, projects: [entry, ...rest].slice(0, PROJECTS_MAX) };
  }
  if (op === 'close' && found && found.tabs.includes(f.chat)) {
    const i = found.tabs.indexOf(f.chat);
    const tabs = found.tabs.filter((c) => c !== f.chat);
    if (!tabs.length) return { ...d, projects: rest };
    const active = found.active === f.chat ? tabs[Math.min(i, tabs.length - 1)] : found.active;
    return { ...d, projects: d.projects.map((p) => (p === found ? { ...p, tabs, active } : p)) };
  }
  if (op === 'move' && found && found.tabs.includes(f.chat)) {   // место index (0…TABS_MAX-1), дальше конца — в конец
    const tabs = found.tabs.filter((c) => c !== f.chat);
    tabs.splice(Math.min(Math.max(0, f.index), TABS_MAX - 1, tabs.length), 0, f.chat);
    return { ...d, projects: d.projects.map((p) => (p === found ? { ...p, tabs } : p)) };
  }
  return d;
}

function publish(next) {
  doc = next;
  for (const fn of listeners) fn(doc);
}

function take(mine, seq, next) {
  if (mine !== epoch || seq < applied) return;
  applied = seq;
  waiting = waiting.filter((a) => a.seq > seq);
  publish(waiting.reduce(apply, next));
}

export async function load() {
  const mine = epoch;
  const seq = ++sent;
  const request = queue.then(() => api('GET', '/api/code/workspace'));
  queue = request.catch(() => {});
  take(mine, seq, await request);
  return doc;
}

export function get() {
  return doc;
}

export function act(op, fields) {
  const mine = epoch;
  const action = { seq: ++sent, op, fields };
  waiting.push(action);
  publish(apply(doc, action));
  const request = queue.then(() => api('PATCH', '/api/code/workspace', { op, ...fields }));
  queue = request.catch(() => {});
  request.then((next) => take(mine, action.seq, next), () => {
    if (mine === epoch) waiting = waiting.filter((a) => a !== action);
  });
}

function entry(aid, pid) {
  return doc.projects.find((p) => p.aid === Number(aid) && p.pid === pid) || null;
}

export function tabs(aid, pid) {
  const found = entry(aid, pid);
  return { tabs: found ? [...found.tabs] : [], active: found ? found.active : null };
}

export function count(aid, pid) {
  const found = entry(aid, pid);
  return found ? found.tabs.length : 0;
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function reset() {
  epoch += 1;
  doc = EMPTY;
  waiting = [];
  listeners.clear();
}
