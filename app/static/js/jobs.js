/* «В работе» (спека 2026-10-21-studio-jobs-panel): значок, опрос GET /api/jobs и панель.
 *
 * Значок #jobs (полоса слева, на телефоне — вверху справа у темы): число идущих (`data-running`) и
 * точка, если есть готовые позже отметки просмотра (`data-new`). Отметка — время сервера `now` того
 * опроса, при котором панель открыли (localStorage `studio-jobs-seen`, удобство устройства, как тема:
 * пропала — новыми считаются все готовые за час). Панель — <dialog>: «Идёт» и «Готово за час», у
 * строки — «Открыть» (адрес раздела с ?id=, chat/<id>), у идущих и ждущих — ещё «Отменить» (адрес
 * отмены раздела из ответа, `cancel`; удаления в панели нет). Открытая
 * панель на опросе строки не пересобирает: сопоставляет по id (fill), фокус и нажатие не теряются.
 *
 * Опрос: раз в JOBS_POLL_MS, пока что-то идёт или открыта панель, иначе раз в IDLE_POLL_MS и при
 * возвращении на экран; первый — без since (готовое за час), дальше since — `now` прошлого ответа.
 * У запроса срок POLL_TIMEOUT_MS: зависший fetch не держит следующие опросы.
 * Скрытая страница опрашивает, только если включены уведомления и что-то идёт. Запертая сессия: api() ждёт отпирания —
 * опрос стоит (отметку «на экране» он не ставит: она — только POST /api/lock/alive). Раздел, запустивший
 * задание, зовёт kick() — опрос сразу. Текст сервера — только textContent.
 *
 * Уведомления (по желанию, «Настройки» → «Уведомления»; флаг устройства localStorage `studio-notify`, как
 * тема): только ServiceWorkerRegistration.showNotification, только пока страница скрыта, только о
 * ставшем готовым за жизнь этой страницы (не о пришедшем первым опросом). Если у сессии есть подписка
 * push, уведомляет сервер — локальный опрос не дублирует его. Нажатие ведёт sw.js: адрес записи попадает
 * в URL окна до возврата фокуса; запасной postMessage({ jobs: href }) проверяет HREF. */
import { api } from './api.js';
import { svg } from './icons.js';
import { timeOnly } from './time.js';
import { el, words } from './ui.js';
import { allCodeActivity, codeActivityState, CODE_ACTIVITY_LABELS, refreshCodeActivity } from './code-activity.js';
import { ENGINE_NAMES } from './code-options.js';
import { sectionName } from './sections-meta.js';

export const JOBS_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;
const POLL_TIMEOUT_MS = 20000;   // срок одного опроса
const RECENT_SEC = 3600;
const SEEN_KEY = 'studio-jobs-seen';
const NOTIFY_KEY = 'studio-notify';
/** Слова уведомления раздела: готово, не получилось. Без промпта, текста и имён — их видно на запертом экране. */
const NOTICES = {
  photo: ['картинка готова', 'картинка не получилась'],
  video: ['ролик готов', 'ролик не получился'],
  transcribe: ['расшифровка готова', 'расшифровка не получилась'],
  ocr: ['распознавание готово', 'распознавание не получилось'],
  translate: ['перевод готов', 'перевод не получился'],
  speech: ['озвучка готова', 'озвучка не получилась'],
};
/** Адрес записи из ответа или от service worker (и строки «Поиска»): иное не открывается. Push Claude Code
 * (bridge.py) ведёт в чат: code/<agent_id>/<pid>/<chat>. */
export const HREF = /^(?:(?:photo|video|transcribe|ocr|translate|speech)\?id=[\w-]+|chat\/[\w-]+|code\/\d+\/[0-9a-f]{16}\/[0-9a-f-]{36})$/;
/** Адрес отмены из ответа (jobs.py CANCEL): иное не отправляется. */
const CANCEL = /^\/api\/(?:(?:images|videos|transcripts|ocr|translate|speeches)\/[\w-]+\/cancel|chats\/[\w-]+\/stop)$/;
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
  box.innerHTML = svg(section);   // константы icons.js: значок раздела — тот же, что на рельсе
  return box;
}

function detailText(item) {
  if (item.section === 'code') return [ENGINE_NAMES[item.engine], CODE_ACTIVITY_LABELS[item.state]].filter(Boolean).join(' · ');
  const when = item.finished === null
    ? [item.detail, item.progress ? `${item.progress}%` : '', clock(serverNow() - item.started)]
    : [item.detail, timeOnly(item.finished)];
  return when.filter(Boolean).join(' · ');
}

/** Раз в секунду — только время идущих: строки целиком не пересобираются (нажатие не промахнётся). */
function tick() {
  for (const item of running) {
    const node = runningList.querySelector(`.jobs-row[data-id="${CSS.escape(item.id)}"] .jobs-detail`);
    if (node) node.textContent = detailText(item);
  }
}

/** «Отменить» идущей или ждущей строки — обычный адрес отмены её раздела (ответ сервера `cancel`):
 * закрытый раздел отвечает 423, как любой его запрос. Отказ — словами в строке до следующего опроса. */
function cancelButton(item) {
  if (typeof item.cancel !== 'string' || !CANCEL.test(item.cancel)) return null;
  const stop = el('button', { type: 'button', class: 'btn btn-sm jobs-cancel', 'aria-label': `Отменить: ${item.title}` },
    'Отменить');
  stop.addEventListener('click', async () => {
    stop.disabled = true;
    try {
      await api('POST', item.cancel);
    } catch (err) {
      const detail = stop.closest('.jobs-row')?.querySelector('.jobs-detail');
      if (detail) {
        detail.textContent = words(err);
        detail.classList.add('error');
      }
      stop.disabled = false;
    }
    poll();
  });
  return stop;
}

function row(item, fresh) {
  const open = el('button', { type: 'button', class: 'btn btn-sm jobs-open' }, 'Открыть');
  open.addEventListener('click', () => openHref(item.href));
  return el('li', { class: `jobs-row${fresh ? ' new' : ''}`, 'data-id': item.id, 'data-section': item.section,
    'data-state': item.state }, icon(item.section),
  el('div', { class: 'jobs-text' }, el('b', { class: 'jobs-title', title: item.title }, item.title),
    el('span', { class: `jobs-detail${item.state === 'error' ? ' error' : ''}` }, detailText(item))),
  el('div', { class: 'jobs-acts' }, item.finished === null ? cancelButton(item) : null, open));
}

/** Строка того же id — тот же узел: меняются только слова и состояние. */
function refresh(node, item) {
  node.dataset.state = item.state;
  node.querySelector('.jobs-title').textContent = item.title;
  node.querySelector('.jobs-title').title = item.title;
  const detail = node.querySelector('.jobs-detail');
  detail.textContent = detailText(item);
  detail.classList.toggle('error', item.state === 'error');
}

/** Список по id: ушедшие строки убираются, новые встают на место, прежние не пересоздаются и без нужды
 * не двигаются — фокус на «Открыть» и нажатие во время опроса не теряются. */
function fill(list, items, fresh, empty) {
  if (!items.length) {
    if (!list.querySelector('.jobs-empty')) list.replaceChildren(el('li', { class: 'muted small jobs-empty' }, empty));
    return;
  }
  const ids = new Set(items.map((i) => i.id));
  const old = new Map();
  for (const node of [...list.children]) {
    if (node.classList.contains('jobs-row') && ids.has(node.dataset.id)) old.set(node.dataset.id, node);
    else node.remove();
  }
  let at = list.firstChild;
  for (const item of items) {
    let node = old.get(item.id);
    if (node) refresh(node, item);
    else node = row(item, fresh(item));
    if (node === at) at = at.nextSibling;
    else list.insertBefore(node, at);
  }
}

function renderPanel() {
  for (const kind of ['running', 'asking', 'unread']) {
    const group = document.getElementById(`jobs-code-${kind}`);
    const items = allCodeActivity().filter((item) => codeActivityState(item) === kind).map((item) => ({
      ...item, id: `code-${item.aid}-${item.chat}`, section: 'code', state: kind, title: item.title || 'Сессия',
      href: `code/${item.aid}/${item.project}/${item.chat}`,
    }));
    group.hidden = !items.length;
    fill(group.querySelector('ul'), items, () => kind === 'unread', '');
  }
  fill(runningList, running, () => false, 'Сейчас ничего не идёт');
  fill(finishedList, finished, (i) => i.finished > shownMark, 'За час ничего не закончилось');
}

function paint() {
  const agents = allCodeActivity();
  const fresh = finished.some((f) => f.finished > readSeen()) || agents.some((item) => item.unread && !item.running);
  const total = running.length + agents.filter((item) => item.running).length;
  badge.dataset.running = String(total);
  if (fresh) badge.dataset.new = '1';
  else delete badge.dataset.new;
  count.textContent = total ? String(total) : '';
  count.hidden = !total;
  dot.hidden = !fresh;
  const label = `В работе${total ? `: ${total}` : ''}${agents.some((item) => item.asking) ? ', требуется ваше действие' : ''}${fresh ? ', есть готовое' : ''}`;
  badge.setAttribute('aria-label', label);
  badge.title = label;
  const bySection = new Map();
  for (const item of running) bySection.set(item.section, (bySection.get(item.section) || 0) + 1);
  bySection.set('code', agents.filter((item) => item.running).length);
  for (const link of document.querySelectorAll('#rail .tab[data-section], #mobile-section-links a[data-section]')) {
    const mark = link.querySelector('.section-running-count');
    if (!mark) continue;                 // рельс «Агентов» обновляет code-activity.js, включая входящие
    const total = bySection.get(link.dataset.section) || 0;
    mark.hidden = !total;
    mark.textContent = total ? String(total) : '';
    link.setAttribute('aria-label', sectionName(link.dataset.section) + (total ? ` · в работе: ${total}` : ''));
  }
  const moreCount = document.getElementById('mobile-running-count');
  if (moreCount) {
    const extra = [...document.querySelectorAll('#rail .tab.mobile-secondary')]
      .reduce((sum, link) => sum + (bySection.get(link.dataset.section) || 0), 0);
    moreCount.hidden = !extra;
    moreCount.textContent = extra ? String(extra) : '';
    const name = document.getElementById('mobile-more-label').textContent;
    moreCount.closest('button').setAttribute('aria-label', (name === 'Ещё' ? 'Ещё · все разделы' : name)
      + (extra ? ` · в работе: ${extra}` : ''));
  }
  if (panel.open) renderPanel();
}

function take(out) {
  const known = new Set(finished.map((f) => f.id));
  const fresh = out.finished.filter((f) => !known.has(f.id));
  if (!first && !out.push) for (const item of fresh) notify(item);
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
    // срок: iOS после сна держит fetch минуту и дольше, а inflight держал бы все следующие опросы
    out = await api('GET', `/api/jobs${since === null ? '' : `?since=${since}`}`, undefined,
      { signal: AbortSignal.timeout(POLL_TIMEOUT_MS) });
  } catch {
    // нет связи или срок вышел — следующий опрос по расписанию
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
  runningList.replaceChildren();              // «новые» — по отметке этого открытия
  finishedList.replaceChildren();
  renderPanel();
  panel.showModal();
  paint();
  clearInterval(ticker);
  ticker = setInterval(tick, 1000);
  poll();
  refreshCodeActivity();
}

/* ---- уведомления ------------------------------------------------------------------ */

/** Уведомления умеет браузер: только через service worker — `new Notification()` на Android запрещён
 * («Illegal constructor»), хотя 'Notification' in window там истинно. */
export function notifySupported() {
  return 'Notification' in window && 'serviceWorker' in navigator && typeof ServiceWorkerRegistration !== 'undefined'
    && 'showNotification' in ServiceWorkerRegistration.prototype;
}

/** Включены на этом устройстве: флаг studio-notify и разрешение браузера. */
export function notifyEnabled() {
  try {
    return notifySupported() && Notification.permission === 'granted' && localStorage.getItem(NOTIFY_KEY) === '1';
  } catch {
    return false;
  }
}

/** Включить (спрашивает разрешение — звать из нажатия) или выключить; true — получилось. */
export async function setNotify(on) {
  try {
    if (!on) {
      localStorage.removeItem(NOTIFY_KEY);
      return true;
    }
    // старый Safari отвечает в обратный вызов, а не обещанием
    const answer = await new Promise((resolve) => {
      const pending = Notification.requestPermission(resolve);
      if (pending) pending.then(resolve, () => resolve('denied'));
    });
    if (answer !== 'granted') return false;
    localStorage.setItem(NOTIFY_KEY, '1');
    return true;
  } catch {
    return false;
  }
}

/** Готовое за жизнь страницы, пока она скрыта: текст без содержимого, tag — id (повтор не дублирует),
 * data.href — куда вести по нажатию (sw.js). Видимой странице хватает значка; отменённое — не повод. */
async function notify(item) {
  const words = NOTICES[item.section];
  if (!words || item.state === 'cancelled' || document.visibilityState === 'visible' || !notifyEnabled()) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    await registration.showNotification(`Студия: ${words[item.state === 'done' ? 0 : 1]}`,
      { tag: item.id, icon: '/static/icons/icon-192.png', data: { href: item.href } });
  } catch {
    // браузер отказал — значок всё скажет при возвращении
  }
}

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
  document.addEventListener('studio-me', paint);
  document.addEventListener('studio-code-snapshot', paint);
  document.addEventListener('studio-navigation', paint);
  // нажатие на уведомление при открытом окне студии: sw.js шлёт адрес записи
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data && typeof event.data.jobs === 'string') openHref(event.data.jobs);
    });
  }
  paint();
  poll();
}
