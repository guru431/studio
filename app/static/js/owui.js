import { tr } from './i18n.js';
/* «Open WebUI» (#/owui, #/owui/<id> — учётка, #/owui/<id>/<chat> — чат): учётки Open WebUI внутри Studio, сервер
 * ходит в Open WebUI сам (docs/superpowers/specs/2026-11-22-studio-open-webui-design.md, «Страница»).
 *
 * Раскладка как у «Агентов» (code.js): слева панель (owui-panel.js) — учётки сверху, под ними дерево чатов; справа —
 * лента чата. Телефон (≤ 760 px): панель выезжает по ☰ и прячется (подложка, Escape, выбор чата), как у «Агентов»;
 * без открытого чата она выезжает сама, учёток нет — справа подсказка и «Подключить».
 *
 * Адрес: #/owui — последнее место (localStorage `studio-owui-place`: учётка Studio, id учётки и чата), нет его — первая
 * учётка; адрес с несуществующей учёткой — к показанной. Нажатие на учётку меняет только панель (без открытого чата —
 * и адрес #/owui/<id> через replaceState, без перехода).
 *
 * Поток: одна SSE-подписка GET /api/owui/accounts/{id}/events на учётку, показанную в панели (учётке `relogin` — нет).
 * События раздаются панели и подписчикам listen(fn) — ленте чата: fn(type, data, aid). Поток кончился — новый с
 * растущей паузой; после `closed` (сессия кончилась или заперта) и `link relogin` — нет: панель попросит пароль. */
import { api } from './api.js';
import { svg } from './icons.js';
import { panelView } from './owui-panel.js';
import { sectionName } from './sections-meta.js';
import { readSSE } from './sse.js';
import { el } from './ui.js';

const PLACE = 'studio-owui-place';
const RETRY_MS = [1000, 2000, 5000, 10000, 30000];

let ui = null;
let panel = null;
let user = null;
let route = { aid: null, chat: null };
let stream = null;                     // { aid, controller }
let settled = false;                   // первый адрес разобран: дальше показ учётки сам ведёт адрес
const listeners = new Set();

/** Подписка на события SSE показанной учётки (лента чата); ответ — отписка. */
export function listen(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function parseId(id) {
  const [aid, chat] = String(id || '').split('/');
  if (!/^\d+$/.test(aid || '')) return { aid: null, chat: null };
  return { aid: Number(aid), chat: chat || null };
}

const hashOf = ({ aid, chat }) => `#/owui${aid === null ? '' : `/${aid}${chat ? `/${encodeURIComponent(chat)}` : ''}`}`;

function readPlace() {
  try {
    const place = JSON.parse(localStorage.getItem(PLACE));
    return place && place.user === user && Number.isInteger(place.aid) ? place : null;
  } catch {
    return null;
  }
}

function savePlace() {
  if (route.aid === null) return;
  try {
    localStorage.setItem(PLACE, JSON.stringify({ user, aid: route.aid, chat: route.chat }));
  } catch { /* закрытое хранилище не мешает разделу */ }
}

function replaceRoute(next) {
  route = next;
  if (location.hash !== hashOf(route)) history.replaceState(history.state, '', hashOf(route));
  savePlace();
}

/* ---- раскладка ---- */

function panelOpen(open) {
  if (!ui) return;
  ui.root.classList.toggle('panel-open', open);
  ui.backdrop.hidden = !open;
  ui.toggle.setAttribute('aria-expanded', String(open));
}

const phone = () => matchMedia('(max-width: 760px)').matches;

function onKey(event) {
  if (event.key === 'Escape' && !event.defaultPrevented && ui?.root.classList.contains('panel-open')
      && !document.querySelector('dialog[open]')) panelOpen(false);   // Escape окна закрывает только окно
}

function empty(text, action = null) {
  const badge = el('span', { class: 'badge', 'aria-hidden': 'true' });
  badge.innerHTML = svg('owui');   // константа icons.js
  ui.body.replaceChildren(el('div', { id: 'owui-empty', class: 'empty' }, badge, el('p', {}, text), action));
}

/** Правая часть по адресу. Лента чата — следующий шаг плана (owui-chat.js); пока — название открытого чата. */
function paintRight() {
  const items = panel.accounts();
  if (!items.length) {
    const first = el('button', { type: 'button', id: 'owui-connect-first', class: 'btn btn-accent' }, tr("Подключить Open WebUI"));
    first.addEventListener('click', () => panel.connect());
    empty(tr("Подключите свой Open WebUI: адрес сервера, email и пароль. Пароль Studio не сохраняет."), first);
    return;
  }
  if (!route.chat) {
    empty(tr("Выберите чат слева или начните новый"));
    return;
  }
  const title = panel.title(route.chat);
  ui.body.replaceChildren(el('div', { id: 'owui-feed', class: 'owui-feed', 'data-account': route.aid, 'data-chat': route.chat },
    title ? el('h2', { class: 'owui-feed-title' }, title) : null));
}

/* ---- поток ---- */

function dispatch(aid, type, data) {
  panel?.event(type, data);
  for (const fn of listeners) {
    try {
      fn(type, data, aid);
    } catch (err) {
      console.error(err);
    }
  }
}

function pause(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

async function run(current) {
  try {
    await follow(current);
  } finally {
    if (stream === current) stream = null;     // поток кончился сам: следующий показ учётки откроет новый
  }
}

async function follow(current) {
  const { signal } = current.controller;
  let tries = 0;
  while (!signal.aborted) {
    let end = false;
    try {
      const response = await api('GET', `/api/owui/accounts/${current.aid}/events`, undefined, { signal });
      for await (const { event, data } of readSSE(response)) {
        if (signal.aborted) return;
        tries = 0;
        if (event === 'closed' || (event === 'link' && data?.state === 'relogin')) end = true;
        dispatch(current.aid, event, data);
      }
    } catch (err) {
      if (signal.aborted) return;
      if (err?.status === 404 || err?.code === 'relogin') return;     // учётку удалили или ей нужен вход заново
    }
    if (end || signal.aborted) return;
    await pause(RETRY_MS[Math.min(tries, RETRY_MS.length - 1)], signal);
    tries += 1;
  }
}

function subscribe(aid) {
  if (stream && stream.aid === aid) return;
  stream?.controller.abort();
  stream = null;
  if (aid === null) return;
  stream = { aid, controller: new AbortController() };
  run(stream);
}

/** Панель показала учётку (или учёток не осталось). */
function opened(aid, acc) {
  if (!ui) return;
  subscribe(acc && acc.state !== 'relogin' ? aid : null);
  if (aid === null) {
    route = { aid: null, chat: null };
    if (settled && location.hash !== '#/owui') history.replaceState(history.state, '', '#/owui');
    if (settled) paintRight();
    return;
  }
  if (!settled) return;                       // первый показ: адрес и последнее место разбирает apply
  const gone = route.aid === null || !panel.accounts().some((a) => a.id === route.aid);   // учётку чата удалили
  if (gone || (!route.chat && route.aid !== aid)) {
    replaceRoute({ aid, chat: null });
    if (gone) {
      panel.here(aid, null);
      paintRight();
      if (phone()) panelOpen(true);         // чата нет: на телефоне — сразу дерево новой учётки
    }
  }
}

/** Адрес → панель и правая часть. */
async function apply(next) {
  const mine = ui;
  const read = await panel?.ready.then(() => true, () => false);
  if (!ui || ui !== mine) return;
  if (!read) {                                // список учёток не прочитался: слова отказа — в панели
    empty(tr("Open WebUI сейчас недоступен"));
    return;
  }
  const items = panel.accounts();             // нынешний список: учётки подключают и удаляют после первого чтения
  settled = true;
  if (next.aid === null) {
    const place = readPlace();
    const aid = place && items.some((a) => a.id === place.aid) ? place.aid : panel.shown();
    if (aid === null) {
      route = { aid: null, chat: null };
      paintRight();
      return;
    }
    next = { aid, chat: place && place.aid === aid ? place.chat : null };
  } else if (!items.some((a) => a.id === next.aid)) {
    next = { aid: panel.shown(), chat: null };
  }
  replaceRoute(next);
  if (panel.shown() !== route.aid) panel.select(route.aid);
  panel.here(route.aid, route.chat);
  paintRight();
  if (route.chat) panelOpen(false);
  else if (phone()) panelOpen(true);
}

export function mount(view, ctx) {
  user = ctx.me.id;
  route = parseId(ctx.id);
  settled = false;
  const toggle = el('button', { type: 'button', id: 'owui-panel-toggle', class: 'icon-btn owui-panel-toggle',
    'aria-controls': 'owui-panel', 'aria-expanded': 'false', 'aria-label': tr("Учётки и чаты Open WebUI"),
    title: tr("Учётки и чаты Open WebUI") });
  toggle.innerHTML = svg('list');   // константа icons.js
  const side = el('aside', { id: 'owui-panel', class: 'owui-panel', 'aria-label': tr("Учётки и чаты Open WebUI") });
  const body = el('div', { class: 'owui-body' });
  const backdrop = el('div', { class: 'owui-backdrop', hidden: true });
  const main = el('section', { class: 'owui-main' },
    el('header', { class: 'head' }, toggle, el('h1', {}, sectionName('owui'))), body);
  const root = el('div', { class: 'owui-ws' }, side, main, backdrop);
  toggle.addEventListener('click', () => panelOpen(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panelOpen(false));
  document.addEventListener('keydown', onKey);
  view.append(root);
  ui = { root, toggle, body, backdrop };
  const place = route.aid === null ? readPlace() : null;
  panel = panelView(side, { selected: route.aid ?? place?.aid ?? null, opened,
    chose: () => panelOpen(false) });
  apply(route);
}

export function update(ctx) {
  return apply(parseId(ctx.id));
}

export function unmount() {
  stream?.controller.abort();
  stream = null;
  panel?.unmount();
  panel = null;
  document.removeEventListener('keydown', onKey);
  ui = null;
}
