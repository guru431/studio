/* Рабочее место раздела «Claude Code» в учётке (GET/PATCH /api/code/workspace; спека 2026-10-29-studio-code-workspace-design,
 * 6.8): кэш документа { panel, place, projects } на время показа раздела и действия записи.
 * - load() — документ с сервера (вход в раздел, возвращение на экран); get() — кэш (до загрузки — пустой);
 * - act(op, fields) — действие в фоне (open, close, place, panel): ответ сервера заменяет кэш и зовёт подписчиков; отказ
 *   (сеть, 5xx) проглатывается — на экране всё уже сделано, следующее действие запишет своё. Ответы, пришедшие не по
 *   порядку, старый документ поверх нового не кладут;
 * - tabs(aid, pid), count(aid, pid) — вкладки проекта из кэша; subscribe(fn) → отписка; reset() — уход из раздела.
 * Только id — имён и названий нет и в документе; в хранилище браузера ничего. */
import { api } from './api.js';

const EMPTY = { panel: null, place: null, projects: [] };
let doc = EMPTY;
let epoch = 0;          // reset(): ответ прежнего показа раздела кэш не трогает
let sent = 0;           // номер последнего запроса
let applied = 0;        // номер запроса, чей ответ лежит в кэше
const listeners = new Set();

function take(mine, seq, next) {
  if (mine !== epoch || seq < applied) return;
  applied = seq;
  doc = next;
  for (const fn of listeners) fn(doc);
}

export async function load() {
  const mine = epoch;
  const seq = ++sent;
  take(mine, seq, await api('GET', '/api/code/workspace'));
  return doc;
}

export function get() {
  return doc;
}

export function act(op, fields) {
  const mine = epoch;
  const seq = ++sent;
  api('PATCH', '/api/code/workspace', { op, ...fields }).then((next) => take(mine, seq, next), () => {});
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
  listeners.clear();
}
