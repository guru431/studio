/* Раздел «Claude Code» (#/code) — спека 2026-10-29-studio-code-workspace-design (раскладка, панель, адреса, рабочее
 * место) поверх 2026-10-28-studio-claude-code-design (мост, PIN, чат, файлы).
 *
 * Раскладка строится один раз на показ раздела (mount) и живёт до unmount: слева — панель компьютеров и проектов
 * (code-panel.js), справа — строка пути и переключатели «Чат · Файлы · Git», под ними тело; update(ctx) перерисовывает
 * только правую часть. Компьютеров нет — панели нет, справа «Компьютеров пока нет.» и «Скачать мост» (ui.js
 * agentDownload). Прокручивается #view: панель и верх правой части к нему прилипают (лента чата — code-chat.js — по
 * #view, как прежде). Телефон (≤ 760 px): панель выезжает по ☰ (#code-panel-toggle), прячется после выбора проекта,
 * нажатия на фон и Escape; сама открыта, пока проекта нет, и при отказе closed.
 *
 * Адреса (§6.7):
 * - #/code — открытый проект и вид из рабочего места учётки (code-workspace.js; location.replace на его адрес), его
 *   нет — «Выберите проект слева»; #/code/<aid> — входная ссылка: этот компьютер в панели, дальше как #/code;
 * - #/code/<aid>/<pid> — проект: сессии (code-sessions.js); ?tab=files|git — «Файлы» и «Git» (code-files.js: «Файлы» —
 *   один экземпляр, пока открыт тот же проект, «Git» — заново при каждом показе);
 * - #/code/<aid>/<pid>/<chat> — чат: section#code-chat сразу, затем chatView (code-chat.js); «Открыть файл» шага —
 *   «Файлы» проекта и файл: путь ждёт перехода в памяти модуля (wantFile), в адрес не попадает.
 * Переключатели меняют адрес заменой (location.replace); проект и вид — в рабочее место (act('place')). Плашка пути —
 * имя компьютера (GET /api/agents) и проекта (GET …/projects — память на время показа раздела, общая с панелью); не
 * ссылка. Компьютер не на связи (по списку компьютеров) — справа только слова, запросов к мосту нет.
 *
 * PIN — в панели. Любой 409 closed или событие closed потока (сессию закрыли: блокировка, выход, 30 минут простоя,
 * перезапуск моста) — returnTo (адрес, где застал отказ, и файл отказа «Сохранить») и поле PIN этого компьютера в
 * панели; адрес не меняется. Второй отказ того же адреса returnTo не трогает. Компьютер открылся (opened) — правая
 * часть заново по этому адресу, у «Файлов» — с файлом и правкой. ?pin=<aid> (прежние ссылки) — то же поле, ?pin уходит
 * из адреса. PIN живёт только в поле панели и переменной её обработчика. Текст сервера и машины — только textContent.
 *
 * Правка файла не сохранена, а адрес уходит (переключатель, проект, «Назад», другой раздел студии) — адрес возвращается
 * (history.replaceState новой записи; «Назад» и «Вперёд» на запись с меткой entry роутера — pushState после неё) и
 * спрашивает окно «Изменения не сохранены» «Файлов»; «Уйти без сохранения» — снова туда (hold — его зовёт роутер
 * app.js до смены адреса или раздела). */
import { api } from './api.js';
import { chatView } from './code-chat.js';
import { filesView, gitView } from './code-files.js';
import { panelView } from './code-panel.js';
import { sessionList } from './code-sessions.js';
import { act, get, load, reset } from './code-workspace.js';
import { agentDownload, el, words } from './ui.js';

const OFFLINE = 'Компьютер не на связи — включите его и запустите мост';
const NONE = 'Компьютеров пока нет.';
const PICK = 'Выберите проект слева';
const NO_AGENT = 'Нет такого компьютера';
const AID_RE = /^\d+$/;
const PID_RE = /^[0-9a-f]{16}$/;
const CHAT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CODE_ICON = '<path d="M4 6l5 5-5 5M11 18h9"/>';
const MENU_ICON = '<path d="M4 6h16M4 12h16M4 18h16"/>';
const VIEWS = [['chat', 'Чат'], ['files', 'Файлы'], ['git', 'Git']];

let host = null;
let ctx = null;
let gen = 0;            // номер отрисовки: ответ прежнего адреса новый не перерисует
let ui = null;          // раскладка раздела
let panel = null;
let none = false;       // компьютеров нет
let view = null;        // чат или сессии — снимаются со сменой адреса
let proj = null;        // открытый проект: { key, aid, pid, naming, files, git, chatBox, filesBox, gitBox }
let wantFile = null;    // путь файла из шага чата до перехода на «Файлы» — только в памяти
let returnTo = null;    // { aid, hash, file } — где компьютер оказался закрыт: после PIN — туда (file — к файлу)
let freshChat = null;   // чат, заведённый «Новым чатом» в этом окне, до первого показа: ему — выбор устройства
let shownHash = null;   // адрес отрисованной правой части: сюда возвращает несохранённая правка
let shownEntry = null;  // метка записи отрисованного адреса ({ entry: n } в history.state — ставит роутер app.js)
/** Проекты компьютеров (GET …/projects): aid → обещание списка, пока показан раздел; отказ не запоминается. */
const lists = new Map();

/** Перенаправление без записи в истории браузера; своё — без вопроса о несохранённой правке. */
function redirect(path) {
  shownHash = `#/${path}`;
  location.replace(shownHash);
}

/** Роутер app.js перед любой сменой адреса (переключатель, проект, раздел, «Назад»): правка файла не сохранена —
 * адрес назад, вопрос окном «Файлов»; «Уйти без сохранения» — снова на новый адрес. true — переход задержан.
 * Новая запись (ссылка, location.hash =, location.replace) метки не несёт — она и становится адресом файла. С меткой —
 * переход по истории («Назад», «Вперёд») на запись, что студия уже показывала (любой раздел): её не трогать (иначе
 * запись, куда ушли, пропала бы из истории), адрес файла — новой записью после неё; «Уйти» — назад на неё. */
export function hold() {
  const files = proj && proj.files;
  if (!files || !files.dirty() || !shownHash || location.hash === shownHash) return false;
  const target = location.hash;
  if (history.state && history.state.entry) {
    history.pushState({ entry: shownEntry }, '', shownHash);
    files.leave(() => history.back());
  } else {
    history.replaceState({ entry: shownEntry }, '', shownHash);
    files.leave(() => location.replace(target));
  }
  return true;
}

function base(aid, pid) {
  return `/api/code/agents/${aid}${pid ? `/projects/${pid}` : ''}`;
}

function projectsOf(aid, fresh = false) {
  const key = String(aid);
  if (fresh || !lists.has(key)) {
    const promise = api('GET', `${base(aid)}/projects`).then((out) => out.items);
    lists.set(key, promise);
    promise.catch(() => {
      if (lists.get(key) === promise) lists.delete(key);
    });
  }
  return lists.get(key);
}

async function agentOf(aid) {
  return ((await panel.agents()) || []).find((a) => String(a.id) === String(aid)) || null;
}

/** Имя проекта (из списка проектов его компьютера) — один запрос на проект; отказ — повтор при следующем показе. */
function projectName(p) {
  if (!p.naming) {
    p.naming = projectsOf(p.aid).then((items) => (items.find((x) => x.pid === p.pid) || {}).name || null);
    p.naming.catch(() => {
      p.naming = null;
    });
  }
  return p.naming;
}

/* ---- раскладка ------------------------------------------------------------------- */

function panelOpen(open) {
  if (!ui) return;
  ui.root.classList.toggle('panel-open', open);
  ui.backdrop.hidden = !open;
  ui.toggle.setAttribute('aria-expanded', String(open));
}

function onKey(event) {
  if (event.key === 'Escape') panelOpen(false);
}

function layout() {
  const toggle = el('button', { type: 'button', id: 'code-panel-toggle', class: 'icon-btn code-panel-toggle',
    'aria-controls': 'code-panel', 'aria-expanded': 'false', 'aria-label': 'Компьютеры и проекты',
    title: 'Компьютеры и проекты' });
  toggle.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${MENU_ICON}</svg>`;   // константа выше
  const placeAgent = el('span', { class: 'code-place-agent' });
  const placeProject = el('b', { class: 'code-place-project' });
  const place = el('span', { id: 'code-place', class: 'code-place', hidden: true }, placeAgent,
    el('span', { class: 'code-place-sep', 'aria-hidden': 'true' }, ' / '), placeProject);
  const switches = VIEWS.map(([key, text]) => {
    const button = el('button', { type: 'button', role: 'tab', id: `code-tab-${key}`, class: 'chip', 'aria-selected': 'false' },
      text);
    button.addEventListener('click', () => switchTo(key));
    return button;
  });
  const views = el('div', { class: 'code-views', role: 'tablist', 'aria-label': 'Вид проекта', hidden: true }, ...switches);
  const top = el('div', { class: 'code-top' }, el('div', { class: 'code-head' }, toggle, place, views));
  const statusBox = el('div', { class: 'code-status-box' });
  const body = el('div', { class: 'code-body' });
  const backdrop = el('div', { class: 'code-backdrop', hidden: true });
  const root = el('div', { class: 'code-ws' }, el('section', { class: 'code-main' }, top, statusBox, body), backdrop);
  toggle.addEventListener('click', () => panelOpen(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panelOpen(false));
  document.addEventListener('keydown', onKey);
  host.replaceChildren(root);
  ui = { root, top, toggle, place, placeAgent, placeProject, views, switches, statusBox, body, backdrop };
}

/** Компьютеров нет: панели нет, справа во всю ширину подсказка, как подключить. */
function empty() {
  none = true;
  closeProject();
  panelOpen(false);
  if (panel) panel.unmount();
  ui.top.hidden = true;
  ui.statusBox.replaceChildren();
  const badge = el('span', { class: 'badge', 'aria-hidden': 'true' });
  badge.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${CODE_ICON}</svg>`;   // константа выше
  ui.body.replaceChildren(el('div', { id: 'code-empty', class: 'empty' }, badge, el('p', {}, NONE), agentDownload()));
}

/** Переключатели вида и плашка пути: key — вид открытого проекта, null — проекта нет. */
function frame(key) {
  ui.place.hidden = key === null;
  ui.views.hidden = key === null;
  for (const button of ui.switches) button.setAttribute('aria-selected', String(button.id === `code-tab-${key}`));
  if (!proj) return;
  proj.chatBox.hidden = key !== 'chat';
  proj.filesBox.hidden = key !== 'files';
  proj.gitBox.hidden = key !== 'git';
}

function switchTo(key) {
  if (!proj) return;
  const hash = `#/code/${proj.aid}/${proj.pid}${key === 'chat' ? '' : `?tab=${key}`}`;
  if (location.hash !== hash) location.replace(hash);
}

/** Проект справа: «Файлы» и «Git» прежнего проекта уходят, плашка — имя компьютера и проекта. */
function enter(aid, pid) {
  const key = `${aid}/${pid}`;
  if (proj && proj.key === key) {
    if (!ui.placeProject.textContent || ui.placeProject.textContent === 'Проект') plaque(proj);
    return;
  }
  closeProject();
  const pane = (hidden) => el('div', { class: 'code-pane', hidden });
  proj = { key, aid, pid, naming: null, files: null, git: null, chatBox: pane(false), filesBox: pane(true), gitBox: pane(true) };
  ui.body.replaceChildren(proj.chatBox, proj.filesBox, proj.gitBox);
  ui.placeAgent.textContent = '';
  ui.placeProject.textContent = '…';
  panel.here(aid, pid);
  plaque(proj);
}

function closeProject() {
  if (!proj) return;
  if (proj.files) proj.files.unmount();
  if (proj.git) proj.git.unmount();
  proj = null;
  if (panel) panel.here(null);
}

async function plaque(p) {
  const agent = await agentOf(p.aid);
  if (proj !== p) return;
  ui.placeAgent.textContent = agent ? agent.name : 'Компьютер';
  if (!agent || !agent.online) {
    ui.placeProject.textContent = 'Проект';
    return;
  }
  let name = null;
  try {
    name = await projectName(p);
  } catch {
    // отказ скажет правая часть
  }
  if (proj === p) ui.placeProject.textContent = name || 'Проект';
}

/** Проект и вид — в рабочее место, если там другое. */
function remember(aid, pid, key) {
  const place = get().place;
  if (!place || place.aid !== Number(aid) || place.pid !== pid || place.view !== key) act('place', { aid: Number(aid), pid, view: key });
}

/* ---- отказы и PIN ------------------------------------------------------------------ */

/** Отказ запроса: closed — поле PIN этого компьютера в панели (с `file` — после PIN к этому файлу), offline — словами
 * из «Текстов», прочее — словами. */
function failed(err, aid, status, file = null) {
  if (err && err.code === 'closed') {
    const key = String(aid);
    if (!returnTo || returnTo.aid !== key || returnTo.hash !== location.hash) returnTo = { aid: key, hash: location.hash, file };
    else if (file) returnTo.file = file;
    panelOpen(true);
    panel.pin(aid);
    return;
  }
  if (status) status.textContent = err && err.code === 'offline' ? OFFLINE : words(err);
}

/** Компьютер открыт (панель): назад, где его закрытость застала; иначе правая часть не меняется. */
function opened(aid) {
  const back = returnTo && returnTo.aid === String(aid) ? returnTo : null;
  if (!back) return;
  returnTo = null;
  panelOpen(false);
  wantFile = back.file;                 // render возьмёт его для «Файлов»
  if (location.hash !== back.hash) location.replace(back.hash);
  else render();
}

function usable(agent, status) {
  if (!agent) status.textContent = NO_AGENT;
  else if (!agent.online) status.textContent = OFFLINE;
  return !!agent && agent.online;
}

/* ---- правая часть ------------------------------------------------------------------ */

async function lobby(mine, aid) {
  const pinFor = ctx.query && ctx.query.get('pin');
  if (!aid && pinFor && AID_RE.test(pinFor)) {
    history.replaceState(history.state, '', '#/code');     // метка записи (hold) — та же
    shownHash = location.hash;
    panelOpen(true);
    panel.pin(pinFor);
  } else if (aid) panel.select(aid);
  const place = get().place;
  if (place) {
    redirect(`code/${place.aid}/${place.pid}${place.view === 'chat' ? '' : `?tab=${place.view}`}`);
    return;
  }
  closeProject();
  frame(null);
  ui.body.replaceChildren();
  const items = await panel.agents();
  if (mine !== gen || none) return;
  if (items && !items.length) {
    empty();
    return;
  }
  ui.body.replaceChildren(el('p', { id: 'code-pick', class: 'muted code-pick-note' }, PICK));
  panelOpen(true);
}

async function sessions(mine, p, status) {
  let items;
  try {
    items = (await api('GET', `${base(p.aid, p.pid)}/chats`)).items;
  } catch (err) {
    if (mine === gen) failed(err, p.aid, status);
    return;
  }
  if (mine !== gen) return;
  const to = (chat) => ctx.navigate(`code/${p.aid}/${p.pid}/${chat}`);
  view = sessionList(p.chatBox, { aid: p.aid, pid: p.pid, items, picked: to,
    created: (chat) => {
      freshChat = chat;
      to(chat);
    },
    failed: (err) => {
      if (mine === gen) failed(err, p.aid, status);
    } });
}

async function chat(mine, p, id, agent, section, status) {
  let name;
  let row;
  try {
    [name, row] = await Promise.all([projectName(p),
      api('GET', `${base(p.aid, p.pid)}/chats`).then((o) => o.items.find((c) => c.chat === id) || null)]);
  } catch (err) {
    if (mine === gen) failed(err, p.aid, status);
    return;
  }
  if (mine !== gen) return;
  ui.statusBox.replaceChildren();            // у чата своя строка #code-status
  const fresh = freshChat === id;
  freshChat = null;                          // дальше «новый» — только по пустой истории (loadHistory)
  view = chatView(section, { aid: p.aid, pid: p.pid, chat: id, agent, project: name ? { pid: p.pid, name } : null,
    origin: row ? row.origin : 'studio', fresh, navigate: ctx.navigate,
    closed: () => failed({ code: 'closed' }, p.aid, null),
    openFile: (path) => {
      wantFile = path;
      ctx.navigate(`code/${p.aid}/${p.pid}?tab=files`);
    } });
}

async function files(mine, p, path, status) {
  let name;
  try {
    name = await projectName(p);
  } catch (err) {
    if (mine === gen) failed(err, p.aid, status);
    return;
  }
  if (mine !== gen || proj !== p) return;
  const refuse = (err, box, file = null) => {
    if (proj === p) failed(err, p.aid, box, file);
  };
  if (!p.files) p.files = filesView(p.filesBox, { aid: p.aid, pid: p.pid, name: name || '', failed: refuse, path });
  else if (path) p.files.open(path);
}

function git(p) {
  if (p.git) p.git.unmount();
  p.git = gitView(p.gitBox, { aid: p.aid, pid: p.pid,
    failed: (err, box) => {
      if (proj === p) failed(err, p.aid, box);
    } });
}

async function project(mine, aid, pid, id, key, path, status) {
  const p = proj;
  const section = id ? el('section', { id: 'code-chat', class: 'code-chat', 'data-chat': id }) : null;
  if (key === 'chat') p.chatBox.replaceChildren(...(section ? [section] : []));
  const agent = await agentOf(aid);
  if (mine !== gen) return;
  if (agent) remember(aid, pid, key);
  if (!usable(agent, status)) return;
  if (id) chat(mine, p, id, agent, section, status);
  else if (key === 'files') files(mine, p, path, status);
  else if (key === 'git') git(p);
  else sessions(mine, p, status);
}

function render() {
  const mine = ++gen;
  if (view) view.unmount();
  view = null;
  shownHash = location.hash;
  shownEntry = history.state && history.state.entry;
  const path = wantFile;
  wantFile = null;
  if (none) return;
  const parts = ctx.id ? ctx.id.split('/') : [];
  const [aid, pid, id] = parts;
  const ok = parts.length <= 3 && (!aid || AID_RE.test(aid)) && (!pid || PID_RE.test(pid)) && (!id || CHAT_RE.test(id));
  if (!ok) {
    redirect('code');
    return;
  }
  // запомненный адрес живёт, пока на нём остаются: ушли куда-то ещё — забыт
  if (returnTo && returnTo.hash !== location.hash) returnTo = null;
  const status = el('p', { id: 'code-status', class: 'error', 'aria-live': 'polite' });
  ui.statusBox.replaceChildren(status);
  if (!pid) {
    lobby(mine, aid);
    return;
  }
  const tab = ctx.query && ctx.query.get('tab');
  const key = !id && (tab === 'files' || tab === 'git') ? tab : 'chat';
  panelOpen(false);                          // проект открыт — панели на телефоне незачем (отказ closed откроет снова)
  enter(aid, pid);
  frame(key);
  project(mine, aid, pid, id, key, key === 'files' ? path : null, status);
}

export function mount(node, context) {
  host = node;
  ctx = context;
  none = false;
  const mine = ++gen;
  layout();
  load().catch(() => null).then(() => {
    if (mine !== gen || !host) return;
    const ws = get();
    const [aid, pid] = ctx.id ? ctx.id.split('/') : [];
    const pinFor = ctx.query && ctx.query.get('pin');
    const first = [pinFor, pid ? null : aid, ws.panel, aid, ws.place && ws.place.aid].map(Number)
      .find((n) => Number.isInteger(n) && n > 0);
    panel = panelView(ui.root, { selected: first ?? null, opened,
      chose: () => panelOpen(false), list: projectsOf });
    ui.root.prepend(ui.root.querySelector('#code-panel'));
    const shown = panel;
    panel.agents().then((items) => {
      if (panel === shown && items && !items.length && !none) empty();
    });
    render();
  });
}

export function update(context) {
  ctx = context;
  if (panel) render();
}

export function unmount() {
  gen += 1;
  if (view) view.unmount();
  view = null;
  closeProject();
  if (panel) panel.unmount();
  panel = null;
  document.removeEventListener('keydown', onKey);
  reset();
  lists.clear();
  shownHash = null;
  wantFile = null;
  returnTo = null;
  none = false;
  host = null;
  ctx = null;
  ui = null;
}
