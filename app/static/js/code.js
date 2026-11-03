/* Раздел «Claude Code» (#/code) — спека 2026-10-29-studio-code-workspace-design (раскладка, панель, адреса, рабочее
 * место) поверх 2026-10-28-studio-claude-code-design (мост, PIN, чат, файлы).
 *
 * Раскладка строится один раз на показ раздела (mount) и живёт до unmount: слева — панель компьютеров и проектов
 * (code-panel.js), справа — строка пути, статус Git и переключатели «Git · Файлы · Чат», под ними тело; update(ctx) перерисовывает
 * только правую часть. Компьютеров нет — панели нет, справа «Компьютеров пока нет.» и «Скачать мост» (ui.js
 * agentDownload). Прокручивается #view: панель и верх правой части к нему прилипают (лента чата — code-chat.js — по
 * #view, как прежде). Телефон (≤ 760 px): панель выезжает по ☰ (#code-panel-toggle), прячется после выбора проекта,
 * нажатия на фон и Escape; сама открыта, пока проекта нет, и при отказе closed.
 *
 * Адреса (§6.7):
 * - #/code — открытый проект, вид и активная вкладка из рабочего места учётки (code-workspace.js; location.replace на
 *   их адрес), их нет — «Выберите проект слева»; #/code/<aid> — входная ссылка: этот компьютер в панели, дальше как
 *   #/code;
 * - #/code/<aid>/<pid> — проект: есть вкладки — заменой на активную (или на «Файлы» / «Git», если рабочее место
 *   смотрело их у этого проекта), нет — создание чата и сессии во всю ширину (code-sessions.js); ?tab=new — тот же
 *   экран новой вкладки по «+»; ?tab=files|git — «Файлы» и «Git»
 *   (code-files.js: «Файлы» — один экземпляр, пока открыт тот же проект, «Git» — заново при каждом показе);
 * - #/code/<aid>/<pid>/<chat> — чат: section#code-chat сразу, затем chatView (code-chat.js); сессии нет во вкладках
 *   (push, «В работе») — она добавляется (act('open')); «Открыть файл» шага — «Файлы» проекта и файл: путь ждёт
 *   перехода в памяти модуля (wantFile), в адрес не попадает.
 * Проект — ссылкой (новая запись истории), вкладка и вид — заменой (location.replace): «Назад» не листает вкладки. Вид —
 * в рабочее место (act('place')). Плашка пути — имя компьютера (GET /api/agents) и проекта (GET …/projects — память на
 * время показа раздела, общая с панелью); не ссылка. Компьютер не на связи (по списку компьютеров) — справа только
 * слова, запросов к мосту нет.
 *
 * Вкладки (code-tabs.js, §6.4): ряд проекта — из рабочего места, показанная — активна (у «Файлов» и «Git» — ни одна);
 * названия и метки — из GET …/chats проекта (один запрос на открытие проекта, общий с сессиями и чатом). Выбор вкладки,
 * пункт «+», «Новый чат» — адрес чата заменой, вкладку открывает показ чата. «×» — act('close'), показанная — соседняя
 * справа, иначе слева, последняя — сессии проекта; так же «Закрыть чат» и 404 not_found истории (gone). Ряд
 * перерисовывается на каждую смену рабочего места; показанная здесь вкладка, которой там уже нет (убрали на другом
 * устройстве), остаётся, пока с неё не уйдут.
 *
 * PIN — в панели. Любой 409 closed или событие closed потока (сессию закрыли: блокировка, выход, 3 часа простоя,
 * перезапуск моста) — returnTo (адрес, где застал отказ, и файл отказа «Сохранить») и поле PIN этого компьютера в
 * панели; адрес не меняется. Второй отказ того же адреса returnTo не трогает. Компьютер открылся (opened) — правая
 * часть заново по этому адресу, у «Файлов» — с файлом и правкой. ?pin=<aid> (прежние ссылки) — то же поле, ?pin уходит
 * из адреса. PIN живёт только в поле панели и переменной её обработчика. Текст сервера и машины — только textContent.
 *
 * Правка файла не сохранена, а адрес уходит (переключатель, проект, «Назад», другой раздел студии) — адрес возвращается
 * (history.replaceState новой записи; «Назад» и «Вперёд» на запись с меткой entry роутера — pushState после неё) и
 * спрашивает окно «Изменения не сохранены» «Файлов»; «Уйти без сохранения» — снова туда (hold — его зовёт роутер
 * app.js до смены адреса или раздела). */
import { api, me } from './api.js';
import { projectGit, gitLevel, GIT_ACTIONS } from './code-git.js';
import { engineOrder } from './code-options.js';
import { panelView } from './code-panel.js';
import { sessionList } from './code-sessions.js';
import { tabBar } from './code-tabs.js';
import { act, get, load, reset, subscribe, tabs as tabsOf } from './code-workspace.js';
import { agentDownload, el, openDialog, words } from './ui.js';

const OFFLINE = 'Компьютер не на связи — включите его и запустите мост';
const NONE = 'Компьютеров пока нет.';
const PICK = 'Выберите проект слева';
const NO_AGENT = 'Нет такого компьютера';
const AID_RE = /^\d+$/;
const PID_RE = /^[0-9a-f]{16}$/;
const CHAT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CODE_ICON = '<path d="M4 6l5 5-5 5M11 18h9"/>';
const MENU_ICON = '<path d="M4 6h16M4 12h16M4 18h16"/>';
const BRANCH_ICON = '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10M18 7v3a4 4 0 0 1-4 4h-4a4 4 0 0 0-4 4"/>';
const VIEWS = [['git', 'Git'], ['files', 'Файлы'], ['chat', 'Чат']];
const IDLE_POLL_SEC = 20;        // метки вкладок: GET …/chats открытого проекта (тесты подменяют паузу setTimeout)
const IDLE_POLL_FOR_SEC = 1800;  // ... не дольше этого после действия владельца в разделе (как опрос live чата)

let host = null;
let ctx = null;
let gen = 0;            // номер отрисовки: ответ прежнего адреса новый не перерисует
let ui = null;          // раскладка раздела
let panel = null;
let none = false;       // компьютеров нет
let view = null;        // чат или сессии — снимаются со сменой адреса
/** Открытый проект: { key, aid, pid, naming, listing, items, info, bar, files, git, chatBox, filesBox, gitBox }. */
let proj = null;
let shownChat = null;   // чат на экране (вкладка, что подсвечена)
let shownKey = null;    // вид на экране: chat, files, git
let shownStatus = null; // строка отказов показанного адреса
let blocked = null;     // aid компьютера, о котором правая часть сказала «не на связи» / «нет такого» (без запросов)
let acted = 0;          // последнее действие владельца в разделе (нажатие, клавиша)
let pollTimer = null;   // следующий опрос меток
let wantFile = null;    // путь файла из шага чата до перехода на «Файлы» — только в памяти
let returnTo = null;    // { aid, hash, file } — где компьютер оказался закрыт: после PIN — туда (file — к файлу)
let freshChat = null;   // чат, заведённый «Новым чатом» в этом окне, до первого показа: ему — выбор устройства
let shownHash = null;   // адрес отрисованной правой части: сюда возвращает несохранённая правка
let shownEntry = null;  // метка записи отрисованного адреса ({ entry: n } в history.state — ставит роутер app.js)
/** Проекты компьютеров (GET …/projects): aid → обещание списка, пока показан раздел; отказ не запоминается. */
const lists = new Map();
// Только метаданные списков, в памяти этой страницы; ключ включает учётку, компьютер и проект.
const chatLists = new Map();
const CHAT_LISTS_MAX = 20;
document.addEventListener('studio-code-clear', () => { lists.clear(); chatLists.clear(); });

function cacheKey(p) {
  return `${me.id}/${p.key}`;
}

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

function projectsOf(aid, fresh = false, force = false) {
  const key = String(aid);
  if (fresh || !lists.has(key)) {
    const promise = api('GET', `${base(aid)}/projects${force ? '?refresh=true' : ''}`).then((out) => out.items);
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
  panel?.visible();
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
  const tabsHost = el('div', { class: 'code-tabs-host', hidden: true });
  const repoStatus = el('button', { type: 'button', id: 'code-repo-status', class: 'code-repo-status',
    'aria-haspopup': 'dialog', 'aria-controls': 'code-git-dialog', hidden: true });
  repoStatus.addEventListener('click', openGitActions);
  const top = el('div', { class: 'code-top' },
    el('div', { class: 'code-head' }, el('div', { class: 'code-location' }, toggle, place),
      el('div', { class: 'code-project-tools' }, repoStatus, views)), tabsHost);
  const statusBox = el('div', { class: 'code-status-box' });
  const body = el('div', { class: 'code-body' });
  const backdrop = el('div', { class: 'code-backdrop', hidden: true });
  const root = el('div', { class: 'code-ws' }, el('section', { class: 'code-main' }, top, statusBox, body), backdrop);
  toggle.addEventListener('click', () => panelOpen(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panelOpen(false));
  root.addEventListener('pointerdown', onAct);
  root.addEventListener('keydown', onAct);
  document.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', onVisibility);
  host.replaceChildren(root);
  ui = { root, top, tabsHost, toggle, place, placeAgent, placeProject, views, switches,
    repoStatus, statusBox, body, backdrop };
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
  ui.repoStatus.hidden = key === null || !ui.repoStatus.textContent;
  if (key === null) ui.tabsHost.hidden = true;
  for (const button of ui.switches) button.setAttribute('aria-selected', String(button.id === `code-tab-${key}`));
  if (!proj) return;
  if (key !== 'git' && proj.git) {
    proj.git.unmount();
    proj.git = null;
  }
  proj.chatBox.hidden = key !== 'chat';
  proj.filesBox.hidden = key !== 'files';
  proj.gitBox.hidden = key !== 'git';
}

/** «Чат» — активная вкладка (её нет — сессии проекта), «Файлы» и «Git» — вид; адрес — заменой. */
function switchTo(key) {
  if (!proj) return;
  const { active } = tabsOf(proj.aid, proj.pid);
  const tail = key !== 'chat' ? `?tab=${key}` : active ? `/${active}` : '';
  const hash = `#/code/${proj.aid}/${proj.pid}${tail}`;
  if (location.hash !== hash) location.replace(hash);
}

function toChat(p, chat) {
  const hash = `#/code/${p.aid}/${p.pid}/${chat}`;
  if (location.hash !== hash) location.replace(hash);
}

/** Сессии проекта (GET …/chats): один запрос на открытие проекта; ответ — названия и метки вкладок. */
function chatsOf(p, fresh = false) {
  if (!p.listing || (fresh && !p.listingPending)) {
    p.listingPending = true;
    const promise = api('GET', `${base(p.aid, p.pid)}/chats`, undefined,
      fresh ? { headers: { 'x-studio-refresh': 'true' } } : {}).then((out) => {
      if (p.listing === promise) {
        p.items = out.complete === false
          ? [...out.items, ...p.items.filter((old) => !out.items.some((item) => item.chat === old.chat))] : out.items;
        p.loaded = true;
        p.complete = out.complete !== false;
        p.info = new Map(p.items.map((c) => [c.chat, c]));
        if (proj === p) {
          const partial = 'Не все сессии удалось обновить. Показаны последние известные сведения.';
          if (shownStatus?.isConnected) {
            if (!p.complete) shownStatus.textContent = partial;
            else if (shownStatus.textContent === partial) shownStatus.textContent = '';
          }
          const key = cacheKey(p);
          chatLists.delete(key);
          // При возвращении метки активности ждут свежего ответа компьютера.
          chatLists.set(key, p.items.map((c) => ({ ...c, running: false, asking: false })));
          if (chatLists.size > CHAT_LISTS_MAX) chatLists.delete(chatLists.keys().next().value);
          paintTabs();
          p.gitActions?.controls();
        }
      }
      return p.items;
    });
    p.listing = promise;
    promise.catch(() => {
      if (p.listing === promise) p.listing = null;
    }).finally(() => { if (!p.listing || p.listing === promise) p.listingPending = false; });
  }
  return p.listing;
}

/** Ряд вкладок открытого проекта: вкладки рабочего места и показанная здесь, если там её уже нет. */
function paintTabs() {
  const p = proj;
  if (!p || !p.bar) return;
  const { tabs } = tabsOf(p.aid, p.pid);
  if (shownChat && !tabs.includes(shownChat)) tabs.push(shownChat);
  ui.tabsHost.hidden = blocked === String(p.aid) || !tabs.length;
  p.bar.show({ tabs, active: shownKey === 'chat' ? shownChat : null, info: p.info, loaded: p.loaded,
    choosing: shownKey === 'chat' && !shownChat });
  p.gitActions?.controls();
  ensurePoll();
}

/* ---- метки вкладок: опрос, пока страница видна и владелец недавно что-то делал в разделе ---------------------- */

/** Опрос продлевает открытость компьютера (каждая просьба мосту, протокол §3): забытая вкладка браузера не должна
 * держать его открытым вечно — только видимая страница, не дольше IDLE_POLL_FOR_SEC после действия, и только у
 * открытого проекта; список чатов нужен только при открытых вкладках, снимок Git — всегда. */
function polling(p) {
  return !!p && proj === p && document.visibilityState !== 'hidden' && Date.now() - acted < IDLE_POLL_FOR_SEC * 1000
    && p.canStart;
}

function stopPoll() {
  clearTimeout(pollTimer);
  pollTimer = null;
}

function ensurePoll() {
  const p = proj;
  if (pollTimer || !polling(p)) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    if (!polling(p)) return;
    try {
      await Promise.all([p.repo.refresh(true), ...(tabsOf(p.aid, p.pid).tabs.length ? [chatsOf(p, true)] : [])]);
      ensurePoll();
    } catch {
      // закрыт, не на связи — опрос до следующего действия; скажет о нём показанный чат
    }
  }, IDLE_POLL_SEC * (p.items.some((row) => row.running || row.asking) ? 1000 : 3000));
}

function onAct() {
  acted = Date.now();
  ensurePoll();
}

/** Вернулись на экран — рабочее место заново (другое устройство могло его поменять): панель и ряд — по нему, а проект,
 * вкладка и адрес на экране — прежние; ушли — опроса нет. */
function onVisibility() {
  if (document.visibilityState === 'hidden') {
    stopPoll();
    return;
  }
  load().catch(() => {});
  if (proj?.canStart) proj.repo.refresh(true);
  ensurePoll();
}

/** Вкладка уходит (сессия на машине остаётся); показанная — на соседнюю справа, иначе слева, последняя — к сессиям. */
function closeTab(p, chat) {
  const shown = proj === p && shownKey === 'chat' && shownChat === chat;
  if (shown) shownChat = null;
  act('close', { aid: Number(p.aid), pid: p.pid, chat });
  if (!shown) {
    if (proj === p && shownKey === 'chat' && !shownChat && view) {
      view.unmount();
      sessions(gen, p, shownStatus);
    }
    return;
  }
  const { active } = tabsOf(p.aid, p.pid);
  if (active) toChat(p, active);
  else location.replace(`#/code/${p.aid}/${p.pid}`);
}

function created(p, chat, engine = 'claude') {
  freshChat = chat;
  p.info.set(chat, { chat, title: null, engine });
  toChat(p, chat);
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
  const p = { key, aid, pid, naming: null, listing: null, loaded: false, items: [], info: new Map(), bar: null, files: null, git: null,
    repo: projectGit(aid, pid),
    chatBox: pane(false), filesBox: pane(true), gitBox: pane(true) };
  const cached = chatLists.get(cacheKey(p));
  if (cached) {
    p.items = cached;
    p.info = new Map(cached.map((c) => [c.chat, c]));
    p.loaded = true;
  }
  proj = p;
  ui.repoStatus.replaceChildren();
  ui.repoStatus.hidden = true;
  p.repo.subscribe((state) => {
    if (proj !== p) return;
    const { data, error } = state;
    ui.repoStatus.dataset.gitLevel = data ? gitLevel(data.files?.length, data.upstream ? data.ahead : data.unpushed) : 'unknown';
    ui.repoStatus.classList.toggle('unavailable', !!error);
    ui.repoStatus.hidden = !data && !error;
    if (!data) {
      ui.repoStatus.textContent = error?.code === 'not_git' ? 'Без Git' : error ? 'Git недоступен' : '';
      ui.repoStatus.title = error?.code === 'not_git' ? 'Не репозиторий git' : 'Действия Git и повторная проверка';
      ui.repoStatus.setAttribute('aria-label', ui.repoStatus.title);
      return;
    }
    const files = Array.isArray(data.files) ? data.files : [];
    const branch = typeof data.branch === 'string' && data.branch ? data.branch : 'HEAD';
    const ahead = data.upstream ? Number(data.ahead) || 0 : data.unpushed || 0;
    const behind = data.upstream ? Number(data.behind) || 0 : 0;
    const glyph = el('span', { class: 'code-repo-icon', 'aria-hidden': 'true' });
    glyph.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${BRANCH_ICON}</svg>`;
    ui.repoStatus.replaceChildren(glyph, el('span', { class: 'code-repo-branch' }, `${branch}${files.length ? '*' : ''}`));
    if (behind) ui.repoStatus.append(el('span', { class: 'code-repo-sync' }, `↓${behind}`));
    if (ahead) ui.repoStatus.append(el('span', { class: 'code-repo-sync' }, `↑${ahead}`));
    ui.repoStatus.title = [`Ветка ${branch}`, files.length ? `Незакоммиченных файлов: ${files.length}` : 'Изменений нет',
      data.upstream || '', behind ? `Позади на ${behind}` : '', ahead ? `Не запушено: ${ahead}` : '',
      'По локальным данным Git, без fetch', 'Действия Git'].filter(Boolean).join(' · ');
    ui.repoStatus.setAttribute('aria-label', ui.repoStatus.title);
  });
  p.bar = tabBar(ui.tabsHost, {
    add: () => location.replace(`#/code/${aid}/${pid}?tab=new`),
    pick: (chat) => toChat(p, chat),
    close: (chat) => closeTab(p, chat),
  });
  ui.body.replaceChildren(p.chatBox, p.filesBox, p.gitBox);
  ui.placeAgent.textContent = '';
  ui.placeProject.textContent = '…';
  panel.here(aid, pid);
  plaque(proj);
}

function closeProject() {
  if (!proj) return;
  proj.gitActions?.unmount();
  stopPoll();
  if (proj.bar) proj.bar.unmount();
  if (proj.files) proj.files.unmount();
  if (proj.git) proj.git.unmount();
  proj.repo.unmount();
  proj = null;
  if (panel) panel.here(null);
}

async function plaque(p) {
  const agent = await agentOf(p.aid);
  if (proj !== p) return;
  ui.placeAgent.textContent = agent ? agent.name : 'Компьютер';
  if (!agent || !agent.online || !(await panel.ready(p.aid))) {
    if (proj === p) ui.placeProject.textContent = 'Проект';
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
  if (['closed', 'not_found'].includes(err?.code)) document.dispatchEvent(new Event('studio-code-clear'));
  if (err && err.code === 'closed') {
    if (proj && String(proj.aid) === String(aid)) {
      proj.canStart = false;
      proj.gitActions?.unmount();
      proj.repo.clear();
      chatLists.delete(cacheKey(proj));
      proj.listing = null;
      proj.items = [];
      proj.info.clear();
      proj.loaded = false;
    }
    const key = String(aid);
    if (!returnTo || returnTo.aid !== key || returnTo.hash !== location.hash) returnTo = { aid: key, hash: location.hash, file };
    else if (file !== null) returnTo.file = file;
    panelOpen(true);
    panel.pin(aid);
    return;
  }
  if (status) status.textContent = err && err.code === 'offline' ? OFFLINE : words(err);
}

/** Компьютер открыт (панель): назад, где его закрытость застала; справа о нём было «не на связи» (мост вернулся, его
 * нажали в панели) — правая часть заново; иначе она не меняется. */
function opened(aid) {
  const back = returnTo && returnTo.aid === String(aid) ? returnTo : null;
  if (!back) {
    if (blocked === String(aid)) render();
    return;
  }
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
  const pinning = !aid && !!pinFor && AID_RE.test(pinFor);
  if (pinning) {
    history.replaceState(history.state, '', '#/code');     // метка записи (hold) — та же
    shownHash = location.hash;
    panel.pin(pinFor);
  } else if (aid) panel.select(aid);
  const place = !pinning && get().place;           // ссылка ради PIN — к месту не уводит: поле осталось бы скрытым
  if (place) {
    const { active } = tabsOf(place.aid, place.pid);
    const tail = place.view !== 'chat' ? `?tab=${place.view}` : active ? `/${active}` : '';
    redirect(`code/${place.aid}/${place.pid}${tail}`);
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

/** Экран новой вкладки: создание чата и сессии без открытых вкладок во всю рабочую область. */
function sessions(mine, p, status) {
  const picker = el('section', { id: 'code-tab-menu', class: 'code-session-picker', 'aria-label': 'Новая вкладка' });
  p.chatBox.replaceChildren(picker);
  const listing = chatsOf(p, true);
  view = sessionList(picker, { aid: p.aid, pid: p.pid, items: p.loaded ? p.items : listing,
    refresh: p.loaded ? listing : null, exclude: new Set(tabsOf(p.aid, p.pid).tabs),
    picked: (chat, row) => { if (row) p.info.set(chat, row); toChat(p, chat); },
    contentSearch: !!p.chatSearch,
    worktrees: !!p.worktrees,
    worktreeOpened: async () => { lists.delete(String(p.aid)); await panel.select(p.aid); },
    created: (chat, engine) => created(p, chat, engine), codex: !!p.codex, opencode: !!p.opencode,
    failed: (err) => {
      if (mine === gen) failed(err, p.aid, status);
    } });
  const scroller = host.closest('#view');
  if (scroller) scroller.scrollTop = 0;
}

async function chat(mine, p, id, agent, section, status) {
  const gitAction = p.gitAction?.chat === id ? p.gitAction.prompt : null;
  p.gitAction = null;
  let name;
  let row;
  let chatView;
  const listing = chatsOf(p);
  const cached = p.info.get(id);
  try {
    if (cached) await api('GET', `${base(p.aid)}/access`);
    if (mine !== gen) return;
    [name, row, { chatView }] = await Promise.all([projectName(p),
      cached || listing.then((items) => items.find((c) => c.chat === id) || p.info.get(id) || null),
      import('./code-chat.js')]);
  } catch (err) {
    if (mine === gen) failed(err, p.aid, status);
    return;
  }
  if (mine !== gen) return;
  if (!row && p.complete === false) {
    failed({ message: 'Не удалось проверить движок сессии. Обновите список и повторите открытие.' }, p.aid, status);
    return;
  }
  ui.statusBox.replaceChildren();            // у чата своя строка #code-status
  const fresh = freshChat === id;
  freshChat = null;                          // дальше «новый» — только по пустой истории (loadHistory)
  view = chatView(section, { aid: p.aid, pid: p.pid, chat: id, agent, project: name ? { pid: p.pid, name } : null,
    origin: row ? row.origin : 'studio', fresh, engine: row?.engine || 'claude', title: row?.title,
    git: p.repo, gitAction,
    onReady: () => { if (mine === gen && proj === p) p.bar?.restoreFocus(id); },
    verify: cached ? listing.then(() => {
      const current = p.info.get(id);
      if (current && (current.engine || 'claude') !== (row?.engine || 'claude')) {
        if (mine === gen) {
          view?.unmount(); view = null;
          section.replaceChildren(); delete section.dataset.loaded;
          chat(mine, p, id, agent, section, status);
        }
        throw new DOMException('Сведения о сессии изменились', 'AbortError');
      }
    }) : null,
    taskDone: () => { p.repo.refresh(true); panel.updateSummary(p.aid, p.pid); },
    forked: (data) => {
      p.info.set(data.chat, { chat: data.chat, engine: data.engine, origin: 'studio' });
      p.listing = null;
      toChat(p, data.chat);
    },
    closed: () => failed({ code: 'closed' }, p.aid, null),
    gone: () => closeTab(p, id),
    openFile: (path) => {
      wantFile = path;
      ctx.navigate(`code/${p.aid}/${p.pid}?tab=files`);
    } });
}

async function files(mine, p, path, status) {
  let name, agent;
  let filesView;
  try {
    [name, agent, { filesView }] = await Promise.all([projectName(p), agentOf(p.aid), import('./code-files.js')]);
  } catch (err) {
    if (mine === gen) failed(err, p.aid, status);
    return;
  }
  if (mine !== gen || proj !== p) return;
  const refuse = (err, box, file = null) => {
    if (proj === p) failed(err, p.aid, box, file);
  };
  if (!p.files) p.files = filesView(p.filesBox, { aid: p.aid, pid: p.pid, name: name || '', failed: refuse, path,
    git: p.repo, operations: agent?.file_operations === true });
  else if (path !== null) p.files.open(path);
}

function gitControls(p) {
  const { active } = tabsOf(p.aid, p.pid);
  const busy = p.info.get(active)?.running || p.info.get(active)?.asking;
  const known = !active || p.info.has(active);
  return { enabled: p.canStart && known && !busy && !p.gitStarting,
    message: p.gitStarting ? 'Подготавливаю действие Git…'
      : !known ? 'Обновите список, чтобы проверить выбранную сессию'
      : busy ? 'Дождитесь завершения задания в выбранном чате' : '' };
}

function openGitActions() {
  const p = proj;
  if (!p || p.gitActions) return;
  let hasGit = false;
  const status = el('p', { id: 'code-git-action-error', class: 'error', 'aria-live': 'polite' });
  const hint = el('p', { class: 'muted small code-git-hint', 'aria-live': 'polite' });
  const buttons = GIT_ACTIONS.map(({ id, label, prompt }) => {
    const button = el('button', { type: 'button', id: `code-git-${id}`, class: 'btn', disabled: true }, label);
    button.addEventListener('click', () => {
      if (!hasGit || !gitControls(p).enabled) return;
      if (p.files?.dirty()) {
        const mine = gen;
        p.gitActions.unmount();
        p.files.leave(() => {
          if (proj !== p || mine !== gen) return;
          openGitActions();
          runGitAction(p, prompt);
        });
      } else runGitAction(p, prompt);
    });
    return button;
  });
  const actions = el('div', { class: 'code-git-actions', role: 'group', 'aria-label': 'Действия Git в чате проекта' }, ...buttons);
  const dialog = openDialog(ui.root, { id: 'code-git-dialog', title: 'Действия Git',
    body: [actions, hint, status], cancelText: 'Закрыть' });
  function controls() {
    const allowed = gitControls(p);
    for (const button of buttons) button.disabled = !hasGit || !allowed.enabled;
    hint.textContent = allowed.message || '';
    hint.hidden = !hint.textContent;
  }
  const unsubscribe = p.repo.subscribe(({ data, error, loading }) => {
    hasGit = !!data;
    controls();
    if (error && !loading) status.textContent = error.code === 'not_git' ? 'Не репозиторий git' : words(error);
  });
  function unmount() {
    unsubscribe();
    dialog.close();
    dialog.remove();
    if (p.gitActions?.dialog === dialog) p.gitActions = null;
  }
  p.gitActions = { dialog, controls, unmount, status };
  dialog.addEventListener('close', unmount, { once: true });
  p.repo.refresh(true);
  chatsOf(p, true).catch((err) => { if (p.gitActions?.dialog === dialog) failed(err, p.aid, status); });
}

async function runGitAction(p, prompt) {
  if (!gitControls(p).enabled || !p.gitActions) return;
  const popup = p.gitActions;
  const mine = gen;
  const current = () => proj === p && mine === gen && p.gitActions === popup;
  p.gitStarting = true;
  popup.controls();
  popup.status.textContent = '';
  try {
    await chatsOf(p, true);
    if (!current() || !p.canStart) return;
    const { active } = tabsOf(p.aid, p.pid);
    const row = p.info.get(active);
    if (row?.running || row?.asking) return;
    if (active) {
      p.gitAction = { chat: active, prompt };
      popup.unmount();
      if (shownKey === 'chat' && shownChat === active) render();
      else toChat(p, active);
    } else {
      const engine = engineOrder(me.code_defaults).find((name) => name === 'claude' || p[name]);
      const suffix = engine === 'claude' ? '' : `?engine=${engine}`;
      const { chat } = await api('POST', `${base(p.aid, p.pid)}/chats${suffix}`);
      p.listing = null;
      if (!current() || !p.canStart) return;
      p.gitAction = { chat, prompt };
      popup.unmount();
      created(p, chat, engine);
    }
  } catch (err) {
    if (current()) failed(err, p.aid, popup.status);
  } finally {
    p.gitStarting = false;
    if (proj === p) p.gitActions?.controls();
  }
}

async function git(p) {
  try {
    const { gitView } = await import('./code-files.js');
    if (proj !== p || shownKey !== 'git') return;
    if (p.git) p.git.unmount();
    p.git = gitView(p.gitBox, { git: p.repo,
      failed: (err, box) => {
        if (proj === p) failed(err, p.aid, box);
      } });
  } catch {
    if (proj === p) failed({ message: 'Не удалось загрузить Git. Проверьте связь и обновите страницу.' }, p.aid, shownStatus);
  }
}

async function project(mine, aid, pid, id, key, path, status) {
  const p = proj;
  const section = id ? el('section', { id: 'code-chat', class: 'code-chat', 'data-chat': id }) : null;
  if (key === 'chat') p.chatBox.replaceChildren(...(section ? [section] : []));
  const agent = await agentOf(aid);
  if (mine !== gen) return;
  p.codex = agent?.codex || null;
  p.chatSearch = !!agent?.chat_search;
  p.worktrees = !!agent?.worktrees;
  p.opencode = agent?.opencode || null;
  p.canStart = false;
  if (agent && id) {
    // чат на экране — его вкладка активна (нет её — добавляется: push, «В работе», выбор в «+»), проект и вид — его
    const { tabs, active } = tabsOf(aid, pid);
    const place = get().place;
    if (!tabs.includes(id) || active !== id || !place || place.aid !== Number(aid) || place.pid !== pid
      || place.view !== 'chat') act('open', { aid: Number(aid), pid, chat: id });
  } else if (agent) remember(aid, pid, key);
  if (!usable(agent, status)) {
    blocked = String(aid);
    paintTabs();
    return;
  }
  const open = await panel.ready(aid);      // панель открывает этот компьютер — запросы моста после неё
  if (mine !== gen) return;
  if (!open) {
    failed({ code: 'closed' }, aid, status);
    return;
  }
  p.canStart = true;
  p.repo.refresh(key === 'git');
  ensurePoll();
  if (id) chat(mine, p, id, agent, section, status);
  else if (key === 'files') files(mine, p, path, status);
  else if (key === 'git') git(p);
  else sessions(mine, p, status);
  if (key !== 'chat') chatsOf(p, key === 'git').catch(() => {}); // Git также сверяет занятость выбранного чата.
}

function render() {
  const mine = ++gen;
  proj?.gitActions?.unmount();
  if (view) view.unmount();
  view = null;
  shownHash = location.hash;
  shownEntry = history.state && history.state.entry;
  const journal = ctx.query && ctx.query.get('journal');
  const path = wantFile ?? (journal === 'findings' ? 'FINDINGS.md' : journal === 'ideas' ? 'IDEAS.md' : null);
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
  shownStatus = status;
  shownChat = null;
  shownKey = null;
  blocked = null;
  if (!pid) {
    lobby(mine, aid);
    return;
  }
  const tab = ctx.query && ctx.query.get('tab');
  const key = !id && (tab === 'files' || tab === 'git') ? tab : 'chat';
  if (!id && key === 'chat' && tab !== 'new') {
    const { active } = tabsOf(aid, pid);
    const place = get().place;
    const seen = place && place.aid === Number(aid) && place.pid === pid && place.view !== 'chat' ? place.view : null;
    if (active) {                            // у проекта есть вкладки: активная (или вид, что смотрели у него)
      redirect(`code/${aid}/${pid}${seen ? `?tab=${seen}` : `/${active}`}`);
      return;
    }
  }
  panelOpen(false);                          // проект открыт — панели на телефоне незачем (отказ closed откроет снова)
  enter(aid, pid);
  shownChat = id || null;
  shownKey = key;
  frame(key);
  paintTabs();
  project(mine, aid, pid, id, key, key === 'files' ? path : null, status);
}

export function mount(node, context) {
  host = node;
  ctx = context;
  none = false;
  acted = Date.now();                     // вход в раздел — действие владельца
  const mine = ++gen;
  layout();
  const initialAgents = api('GET', '/api/agents');
  initialAgents.catch(() => {});
  load().catch(() => null).then(() => {
    if (mine !== gen || !host) return;
    const ws = get();
    const [aid, pid] = ctx.id ? ctx.id.split('/') : [];
    const pinFor = ctx.query && ctx.query.get('pin');
    const first = [pinFor, pid ? null : aid, ws.panel, aid, ws.place && ws.place.aid].map(Number)
      .find((n) => Number.isInteger(n) && n > 0);
    panel = panelView(ui.root, { selected: first ?? null, opened, initialAgents,
      chose: () => panelOpen(false), list: projectsOf, openJournal: (path) => proj?.files?.open(path) });
    ui.root.prepend(ui.root.querySelector('#code-panel'));
    const shown = panel;
    panel.agents().then((items) => {
      if (panel === shown && items && !items.length && !none) empty();
    });
    subscribe(paintTabs);                 // рабочее место сменилось (действие здесь, ответ сервера) — ряд заново
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
  stopPoll();
  document.removeEventListener('keydown', onKey);
  document.removeEventListener('visibilitychange', onVisibility);
  reset();
  lists.clear();
  shownChat = null;
  shownKey = null;
  shownStatus = null;
  shownHash = null;
  wantFile = null;
  returnTo = null;
  none = false;
  host = null;
  ctx = null;
  ui = null;
}
