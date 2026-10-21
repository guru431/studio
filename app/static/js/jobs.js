/* «В работе» (спека 2026-10-21-studio-jobs-panel): значок, опрос GET /api/jobs и панель.
 *
 * Значок #jobs (полоса слева, на телефоне — вверху справа у темы): число идущих (`data-running`) и
 * точка, если есть готовые позже отметки просмотра (`data-new`). Отметка — время сервера `now` того
 * опроса, при котором панель открыли (localStorage `studio-jobs-seen`, удобство устройства, как тема:
 * пропала — новыми считаются все готовые за час). Панель — <dialog>: «Идёт» и «Готово за час», у
 * строки — «Открыть» (адрес раздела с ?id=, chat/<id>); отмены и удаления в панели нет.
 *
 * Опрос: раз в JOBS_POLL_MS, пока что-то идёт или открыта панель, иначе раз в IDLE_POLL_MS и при
 * возвращении на экран; первый — без since (готовое за час), дальше since — `now` прошлого ответа.
 * Скрытая страница опрашивает, только пока что-то идёт. Запертая сессия: api() ждёт отпирания —
 * опрос стоит (отметку «на экране» он не ставит: она — только POST /api/lock/alive). Раздел, запустивший
 * задание, зовёт kick() — опрос сразу. Текст сервера — только textContent. */
import { api } from './api.js';
import { el } from './ui.js';

export const JOBS_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;
const RECENT_SEC = 3600;
const SEEN_KEY = 'studio-jobs-seen';
/** Адрес записи из ответа или от service worker: иное не открывается. */
const HREF = /^(?:(?:photo|video|transcribe|speech)\?id=[\w-]+|chat\/[\w-]+)$/;
const ICONS = {
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  photo: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>',
  transcribe: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
  speech: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
};

let badge = null;
let count;
let dot;
let panel;
let runningList;
let finishedList;
let go = null;               // navigate оболочки
let running = [];
let finished = [];           // готовые за час, новые сверху
let since = null;            // now прошлого ответа сервера
let gotAt = 0;               // Date.now() того ответа: идущим время — от часов сервера
let first = true;            // первый ответ: готовое до открытия страницы, не «стало готовым»
let inflight = false;
let again = false;
let timer = null;
let ticker = null;
let shownMark = 0;           // отметка до открытия панели: по ней выделены новые строки

function readSeen() {
  try {
    return Number(localStorage.getItem(SEEN_KEY)) || 0;
  } catch {
    return 0;
  }
}

function writeSeen(value) {
  try {
    localStorage.setItem(SEEN_KEY, String(value));
  } catch {
    // хранилище закрыто — новыми останутся все готовые за час
  }
}

function clock(sec) {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(mm).padStart(2, '0')}:${ss}` : `${mm}:${ss}`;
}

function serverNow() {
  return since === null ? Date.now() / 1000 : since + (Date.now() - gotAt) / 1000;
}

function icon(section) {
  const box = el('span', { class: 'jobs-ico', 'aria-hidden': 'true' });
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${ICONS[section] || ''}</svg>`;   // константы выше
  return box;
}

function detailText(item) {
  const when = item.finished === null
    ? [item.detail, item.progress ? `${item.progress}%` : '', clock(serverNow() - item.started)]
    : [item.detail, new Date(item.finished * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })];
  return when.filter(Boolean).join(' · ');
}

/** Раз в секунду — только время идущих: строки целиком не пересобираются (нажатие не промахнётся). */
function tick() {
  for (const item of running) {
    const node = runningList.querySelector(`.jobs-row[data-id="${CSS.escape(item.id)}"] .jobs-detail`);
    if (node) node.textContent = detailText(item);
  }
}

function row(item, fresh) {
  const open = el('button', { type: 'button', class: 'btn btn-sm jobs-open' }, 'Открыть');
  open.addEventListener('click', () => openHref(item.href));
  return el('li', { class: `jobs-row${fresh ? ' new' : ''}`, 'data-id': item.id, 'data-section': item.section,
    'data-state': item.state }, icon(item.section),
  el('div', { class: 'jobs-text' }, el('b', { class: 'jobs-title' }, item.title),
    el('span', { class: `jobs-detail${item.state === 'error' ? ' error' : ''}` }, detailText(item))),
  open);
}

function renderPanel() {
  runningList.replaceChildren(...(running.length ? running.map((i) => row(i, false))
    : [el('li', { class: 'muted small jobs-empty' }, 'Сейчас ничего не идёт')]));
  finishedList.replaceChildren(...(finished.length ? finished.map((i) => row(i, i.finished > shownMark))
    : [el('li', { class: 'muted small jobs-empty' }, 'За час ничего не закончилось')]));
}

function paint() {
  const fresh = finished.some((f) => f.finished > readSeen());
  badge.dataset.running = String(running.length);
  if (fresh) badge.dataset.new = '1';
  else delete badge.dataset.new;
  count.textContent = running.length ? String(running.length) : '';
  count.hidden = !running.length;
  dot.hidden = !fresh;
  const label = `В работе${running.length ? `: ${running.length}` : ''}${fresh ? ', есть готовое' : ''}`;
  badge.setAttribute('aria-label', label);
  badge.title = label;
  if (panel.open) renderPanel();
}

function take(out) {
  const known = new Set(finished.map((f) => f.id));
  const fresh = out.finished.filter((f) => !known.has(f.id));
  if (!first) for (const item of fresh) notify(item);
  first = false;
  finished = [...fresh, ...finished].filter((f) => f.finished >= out.now - RECENT_SEC)
    .sort((a, b) => b.finished - a.finished);
  running = out.running;
  since = out.now;
  gotAt = Date.now();
  if (panel.open) writeSeen(out.now);       // открытая панель — просмотр
  badge.dataset.polled = '1';               // первый ответ пришёл (по нему ждут браузерные тесты)
  paint();
}

function schedule() {
  clearTimeout(timer);
  if (document.visibilityState !== 'visible' && !(notifyEnabled() && running.length)) return;
  timer = setTimeout(poll, running.length || panel.open ? JOBS_POLL_MS : IDLE_POLL_MS);
}

async function poll() {
  if (!badge) return;
  if (inflight) {
    again = true;
    return;
  }
  clearTimeout(timer);
  inflight = true;
  again = false;
  let out = null;
  try {
    out = await api('GET', `/api/jobs${since === null ? '' : `?since=${since}`}`);
  } catch {
    // нет связи — следующий опрос по расписанию
  }
  inflight = false;
  if (out) take(out);
  if (again) poll();
  else schedule();
}

/** Опрос сразу: раздел только что запустил задание. */
export function kick() {
  poll();
}

function openHref(href) {
  if (typeof href !== 'string' || !HREF.test(href)) return;
  if (panel.open) panel.close();
  if (location.hash === `#/${href}`) window.dispatchEvent(new HashChangeEvent('hashchange'));
  else go(href);
}

function openPanel() {
  shownMark = readSeen();
  if (since !== null) writeSeen(since);
  renderPanel();
  panel.showModal();
  paint();
  clearInterval(ticker);
  ticker = setInterval(tick, 1000);
  poll();
}

/* ---- уведомления ------------------------------------------------------------------ */

export function notifyEnabled() {
  return false;
}

function notify() {}

/** Оболочка зовёт после профиля (app.js, рядом с setupMenu). */
export function setupJobs({ navigate }) {
  go = navigate;
  badge = document.getElementById('jobs');
  count = badge.querySelector('.jobs-count');
  dot = badge.querySelector('.jobs-dot');
  panel = document.getElementById('jobs-panel');
  runningList = document.getElementById('jobs-running');
  finishedList = document.getElementById('jobs-finished');
  badge.addEventListener('click', openPanel);
  document.getElementById('jobs-close').addEventListener('click', () => panel.close());
  panel.addEventListener('click', (event) => { if (event.target === panel) panel.close(); });   // мимо листа
  panel.addEventListener('close', () => {
    clearInterval(ticker);
    paint();
    schedule();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') poll();
    else schedule();
  });
  paint();
  poll();
}
