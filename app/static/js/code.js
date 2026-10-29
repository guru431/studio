/* Раздел «Claude Code» (#/code) — спека 2026-10-28-studio-claude-code-design, 6.3 и 6.5.
 *
 * Уровни по хвосту адреса (ctx.id, делится по «/»); рамка уровня (шапка, строка пути) рисуется сразу, список —
 * по ответу (прежний уровень, например панель PIN, до ответа не остаётся):
 * - #/code — компьютеры учётки (GET /api/agents, без моста): имя, «на связи» / «не на связи», значок «PIN»;
 *   пусто — «Компьютеров пока нет.» и ссылка «Скачать мост» (ui.js agentDownload).
 *   Компьютер не на связи — только строка: нажатие говорит словами и ничего не запрашивает. На связи —
 *   POST …/open с pin: null; 204 — проекты, 409 pin_needed — панель PIN (#code-pin: не <form> и не password —
 *   менеджеры паролей не предложат сохранить PIN; Enter и «Открыть» отправляют).
 * - #/code/<aid> — проекты (GET …/projects); #/code/<aid>/<pid> — проект: «Новый чат», вкладки «Чаты»,
 *   «Файлы», «Git» (две последние рисует code-files.js: «Файлы» — при первом показе вкладки, «Git» — заново
 *   при каждом), чаты (GET …/chats) с пометками и «Закрыть» / «Открыть снова» (PATCH).
 * - #/code/<aid>/<pid>/<chat> — чат: section#code-chat сразу, затем компьютер, проект и откуда чат (GET агентов,
 *   проектов и чатов проекта — и при переходе по адресу из push) и chatView (code-chat.js). «Открыть файл»
 *   шага — вкладка «Файлы» проекта (#/code/<aid>/<pid>?tab=files) и filesView.open(путь): путь ждёт перехода
 *   в памяти модуля (wantFile), в адрес не попадает.
 *
 * PIN проверяет мост: набранный PIN живёт только в поле и переменной обработчика, после ответа поле
 * очищается; ни в хранилище браузера, ни в адрес он не попадает. Любой 409 closed или событие closed потока
 * (сессию закрыли: блокировка, выход, 30 минут простоя, перезапуск моста) — toPin: адрес уровня, где это
 * случилось, — в памяти модуля (returnTo), переход на #/code?pin=<aid>: у компьютера с PIN сразу панель, без
 * PIN — тот же POST open, что и нажатие, не на связи — слова; ?pin из адреса уходит. Второй отказ до перехода
 * (параллельные запросы уровня) адрес возврата не трогает. Открылся — назад на запомненный адрес
 * (чат, проект; отказ «Сохранить» — вкладка «Файлы» с этим файлом и правкой), а не к проектам. Перенаправления (к PIN и назад, хвост не того вида — на #/code, компьютера
 * нет) — location.replace: «Назад» не возвращает на них. Текст сервера и машины — только textContent.
 *
 * Правка файла не сохранена, а адрес уходит (строка пути, «Назад», другой раздел студии) — адрес возвращается
 * (history.replaceState новой записи; «Назад» и «Вперёд» на запись с меткой entry роутера — pushState после неё) и
 * спрашивает окно «Изменения не сохранены» вкладки «Файлы»; «Уйти без сохранения» — снова туда (hold — его зовёт
 * роутер app.js до смены уровня или раздела). */
import { api } from './api.js';
import { chatView } from './code-chat.js';
import { filesView, gitView } from './code-files.js';
import { attemptsLeft } from './lock.js';
import { agentDownload, el, words } from './ui.js';

const OFFLINE = 'Компьютер не на связи — включите его и запустите мост';
const PIN_LOCKED = 'PIN заблокирован после 5 ошибок — разблокируйте его в окне моста на самом компьютере';
const NONE = 'Компьютеров пока нет.';
const UNTITLED = 'Новый чат';
const AID_RE = /^\d+$/;
const PID_RE = /^[0-9a-f]{16}$/;
const CHAT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CODE_ICON = '<path d="M4 6l5 5-5 5M11 18h9"/>';
const TABS = [['chats', 'Чаты'], ['files', 'Файлы'], ['git', 'Git']];

let host = null;
let ctx = null;
let gen = 0;            // номер отрисовки: ответ прежнего уровня новый не перерисует
let view = null;        // чат (chatView) или вкладки проекта — снимается со сменой уровня
let wantFile = null;    // путь файла из шага чата до перехода на вкладку «Файлы» — только в памяти
let returnTo = null;    // { aid, hash, file } — где компьютер оказался закрыт: после PIN — туда (file — к файлу)
let freshChat = null;   // чат, заведённый «Новым чатом» в этом окне, до первого показа: ему — выбор устройства
let shownHash = null;   // адрес отрисованного уровня: сюда возвращает несохранённая правка
let shownEntry = null;  // метка записи отрисованного уровня ({ entry: n } в history.state — ставит роутер app.js)

/** Перенаправление без записи в истории браузера; своё — без вопроса о несохранённой правке. */
function redirect(path) {
  shownHash = `#/${path}`;
  location.replace(shownHash);
}

/** Роутер app.js перед любой сменой адреса (уровень, раздел, «Назад»): правка файла не сохранена — адрес назад,
 * вопрос окном вкладки «Файлы»; «Уйти без сохранения» — снова на новый адрес. true — переход задержан.
 * Новая запись (ссылка, location.hash =) метки не несёт — она и становится адресом файла. С меткой — переход по
 * истории («Назад», «Вперёд») на запись, что студия уже показывала (любой раздел): её не трогать (иначе запись,
 * куда ушли, пропала бы из истории), адрес файла — новой записью после неё; «Уйти» — назад на неё. */
export function hold() {
  if (!view || !view.dirty || !view.dirty() || !shownHash || location.hash === shownHash) return false;
  const target = location.hash;
  if (history.state && history.state.entry) {
    history.pushState({ entry: shownEntry }, '', shownHash);
    view.leave(() => history.back());
  } else {
    history.replaceState({ entry: shownEntry }, '', shownHash);
    view.leave(() => location.replace(target));
  }
  return true;
}

/** Компьютер закрыт для сессии — к его панели PIN, запомнив, откуда (`back` и `file` — вкладка «Файлы» и файл,
 * чьё сохранение застал отказ: после PIN он откроется с правкой). Адрес уже ведёт к PIN (второй отказ того же
 * уровня раньше hashchange первого) — запомненное не трогать. */
function toPin(aid, back = location.hash, file = null) {
  if (location.hash.startsWith('#/code?pin=')) return;
  returnTo = { aid: String(aid), hash: back, file };
  redirect(`code?pin=${aid}`);
}

/** Компьютер открыт: назад, где его закрытость застала, иначе — его проекты. */
function opened(aid) {
  const back = returnTo && returnTo.aid === String(aid) ? returnTo : null;
  returnTo = null;
  if (!back) {
    ctx.navigate(`code/${aid}`);
    return;
  }
  wantFile = back.file;                 // render возьмёт его для вкладки «Файлы»
  location.replace(back.hash);
}

function when(ts) {
  return new Date(ts * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function base(aid, pid) {
  return `/api/code/agents/${aid}${pid ? `/projects/${pid}` : ''}`;
}

function crumbsNav(crumbs) {
  const nav = el('nav', { class: 'code-crumbs', 'aria-label': 'Путь' });
  crumbs.forEach(([text, href], i) => {
    if (i) nav.append(el('span', { class: 'muted', 'aria-hidden': 'true' }, '›'));
    nav.append(href ? el('a', { href }, text) : el('span', { class: 'code-crumb-here' }, text));
  });
  return nav;
}

/** Шапка, строка пути (ссылки на уровни выше) и строка отказов; тело уровня — в возвращённом main. */
function frame(crumbs) {
  const status = el('p', { id: 'code-status', class: 'error', 'aria-live': 'polite' });
  const nav = crumbs.length ? crumbsNav(crumbs) : null;
  const main = el('div', { class: 'page code' }, nav, status);
  host.replaceChildren(el('header', { class: 'head' }, el('h1', {}, 'Claude Code')), main);
  return { main, status, nav };
}

/** Отказ запроса: closed — к панели PIN этого компьютера (с `file` — после PIN к этому файлу проекта `pid`),
 * offline — словами из «Текстов», прочее — словами. */
function failed(err, aid, status, pid = null, file = null) {
  if (err && err.code === 'closed') {
    if (file && pid) toPin(aid, `#/code/${aid}/${pid}?tab=files`, file);
    else toPin(aid);
    return;
  }
  status.textContent = err && err.code === 'offline' ? OFFLINE : words(err);
}

async function agentsList() {
  return (await api('GET', '/api/agents')).items;
}

/* ---- компьютеры ----------------------------------------------------------------- */

function computers(mine) {
  const { main, status } = frame([]);
  const wanted = ctx.query && ctx.query.get('pin');
  if (wanted) history.replaceState(history.state, '', '#/code');   // метка записи (hold) — та же
  let pinBox = null;

  function showPin(agent) {
    if (pinBox) pinBox.remove();
    // не <form> и не password: исчезнув после удачного запроса, они дали бы менеджеру паролей сохранить PIN
    // компьютера в синхронизируемое хранилище браузера; точки вместо цифр — -webkit-text-security (app.css)
    const input = el('input', { id: 'code-pin-input', class: 'input', type: 'text', inputmode: 'numeric',
      autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', maxlength: '64',
      'aria-label': 'PIN компьютера' });
    const go = el('button', { id: 'code-pin-open', class: 'btn btn-accent', type: 'button' }, 'Открыть');
    const error = el('p', { id: 'code-pin-error', class: 'error', 'aria-live': 'polite' });
    pinBox = el('div', { id: 'code-pin', class: 'code-pin', role: 'group', 'aria-label': 'PIN компьютера', 'data-id': agent.id },
      el('h2', {}, 'PIN компьютера'), el('p', { class: 'muted code-pin-name' }, agent.name),
      el('div', { class: 'code-pin-row' }, input, go), error);
    go.addEventListener('click', send);
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.isComposing) return;
      event.preventDefault();
      send();
    });
    async function send() {
      if (go.disabled || !input.value) return;
      const pin = input.value;
      go.disabled = true;
      error.textContent = '';
      try {
        await api('POST', `${base(agent.id)}/open`, { pin });
        if (mine === gen) opened(agent.id);
        return;
      } catch (err) {
        if (mine !== gen) return;
        const left = err.detail && err.detail.left;
        if (err.code === 'pin') error.textContent = Number.isInteger(left) ? `Неверный PIN — ${attemptsLeft(left)}` : 'Неверный PIN';
        else if (err.code === 'pin_locked') error.textContent = PIN_LOCKED;
        else if (err.code === 'offline') error.textContent = OFFLINE;
        else error.textContent = words(err);
      } finally {
        input.value = '';
        go.disabled = false;
      }
      input.focus();
    }
    main.append(pinBox);
    input.focus();
  }

  async function choose(agent) {
    status.textContent = '';
    if (pinBox) pinBox.remove();
    pinBox = null;
    if (!agent.online) {
      status.textContent = OFFLINE;
      return;
    }
    try {
      await api('POST', `${base(agent.id)}/open`, { pin: null });
      if (mine === gen) opened(agent.id);
    } catch (err) {
      if (mine !== gen) return;
      if (err.code === 'pin_needed') showPin(agent);
      else if (err.code === 'pin_locked') status.textContent = PIN_LOCKED;
      else failed(err, agent.id, status);
    }
  }

  agentsList().then((items) => {
    if (mine !== gen) return;
    if (!items.length) {
      const badge = el('span', { class: 'badge', 'aria-hidden': 'true' });
      badge.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${CODE_ICON}</svg>`;   // константа выше
      main.append(el('div', { id: 'code-empty', class: 'empty' }, badge, el('p', {}, NONE), agentDownload()));
      return;
    }
    const list = el('ul', { id: 'code-agents', class: 'code-list' });
    for (const agent of items) {
      const row = el('button', { type: 'button', class: 'code-agent' },
        el('b', { class: 'code-agent-name' }, agent.name),
        el('span', { class: `tag code-agent-state${agent.online ? ' server' : ''}` }, agent.online ? 'на связи' : 'не на связи'),
        agent.has_pin ? el('span', { class: 'tag code-agent-pin' }, 'PIN') : null,
        agent.online && agent.cc_version ? el('span', { class: 'muted small code-agent-meta' }, `Claude Code ${agent.cc_version}`) : null);
      row.addEventListener('click', () => choose(agent));
      list.append(el('li', { 'data-id': agent.id }, row));
    }
    main.insertBefore(list, status);
    const back = items.find((a) => String(a.id) === wanted);
    if (back && back.online) {
      if (back.has_pin) showPin(back);
      else choose(back);
    } else if (back) status.textContent = OFFLINE;   // пропал, пока шёл переход к PIN
  }, (err) => {
    if (mine === gen) status.textContent = words(err);
  });
}

/* ---- проекты ---------------------------------------------------------------------- */

async function agentName(aid) {
  try {
    const agent = (await agentsList()).find((a) => String(a.id) === aid);
    return agent ? agent.name : '';
  } catch {
    return '';            // имя — только подпись пути
  }
}

async function projects(mine, aid) {
  const { main, status, nav } = frame([['Компьютеры', '#/code'], ['Компьютер', null]]);
  const [name, out] = await Promise.all([agentName(aid),
    api('GET', `${base(aid)}/projects`).then((o) => ({ items: o.items }), (err) => ({ err }))]);
  if (mine !== gen) return;
  if (name) nav.replaceWith(crumbsNav([['Компьютеры', '#/code'], [name, null]]));
  if (out.err) {
    failed(out.err, aid, status);
    return;
  }
  const list = el('ul', { id: 'code-projects', class: 'code-list' });
  for (const p of out.items) {
    list.append(el('li', { 'data-pid': p.pid }, el('a', { class: 'code-project', href: `#/code/${aid}/${p.pid}` }, p.name)));
  }
  main.insertBefore(list, status);
}

/* ---- проект ----------------------------------------------------------------------- */

function chatRow(mine, aid, pid, chat, reload, status) {
  const marks = [el('span', { class: 'tag code-origin' }, chat.origin === 'studio' ? 'студия' : 'VS Code или терминал')];
  if (chat.state === 'closed') marks.push(el('span', { class: 'tag code-closed' }, 'закрыт'));
  if (chat.asking) marks.push(el('span', { class: 'tag code-asking' }, 'ждёт ответа'));
  else if (chat.running) marks.push(el('span', { class: 'tag code-running' }, 'идёт ход'));
  const closed = chat.state === 'closed';
  const toggle = el('button', { type: 'button', class: `btn btn-sm ${closed ? 'code-chat-reopen' : 'code-chat-close'}` },
    closed ? 'Открыть снова' : 'Закрыть');
  toggle.addEventListener('click', async () => {
    toggle.disabled = true;
    status.textContent = '';
    try {
      await api('PATCH', `${base(aid, pid)}/chats/${chat.chat}`, { state: closed ? 'open' : 'closed' });
      reload();
    } catch (err) {
      toggle.disabled = false;
      if (mine === gen) failed(err, aid, status);
    }
  });
  const link = el('a', { class: 'code-chat-link', href: `#/code/${aid}/${pid}/${chat.chat}` },
    el('span', { class: 'code-chat-title' }, chat.title || UNTITLED),
    typeof chat.modified === 'number' && chat.modified > 0 ? el('span', { class: 'muted small code-chat-time' }, when(chat.modified)) : null);
  return el('li', { 'data-chat': chat.chat, class: closed ? 'closed' : null }, link,
    el('span', { class: 'code-marks' }, ...marks), toggle);
}

async function project(mine, aid, pid, tab, path) {
  const { main, status, nav } = frame([['Компьютеры', '#/code'], ['Компьютер', `#/code/${aid}`], ['Проект', null]]);
  const [name, found] = await Promise.all([agentName(aid),
    api('GET', `${base(aid)}/projects`).then((o) => o.items.find((p) => p.pid === pid), () => null)]);
  if (mine !== gen) return;
  let files = null;
  let git = null;
  view = {
    dirty: () => !!files && files.dirty(),             // несохранённая правка вкладки «Файлы» (hold)
    leave: (go) => files.leave(go),
    unmount() {
      if (files) files.unmount();
      if (git) git.unmount();
    },
  };
  nav.replaceWith(crumbsNav([['Компьютеры', '#/code'], [name || 'Компьютер', `#/code/${aid}`],
    [found ? found.name : 'Проект', null]]));
  const add = el('button', { type: 'button', id: 'code-new-chat', class: 'btn btn-accent' }, 'Новый чат');
  add.addEventListener('click', async () => {
    add.disabled = true;
    status.textContent = '';
    try {
      const { chat } = await api('POST', `${base(aid, pid)}/chats`);
      if (mine !== gen) return;
      freshChat = chat;
      ctx.navigate(`code/${aid}/${pid}/${chat}`);
    } catch (err) {
      add.disabled = false;
      if (mine === gen) failed(err, aid, status);
    }
  });
  const tabs = el('div', { class: 'code-tabs', role: 'tablist' });
  const panes = {};
  const first = TABS.some(([key]) => key === tab) ? tab : 'chats';
  const refuse = (err, box, file = null) => {
    if (mine === gen) failed(err, aid, box, pid, file);
  };
  let shown = null;
  function show(key) {
    if (shown === 'files' && key !== 'files' && files) {
      files.leave(() => {
        if (mine === gen) showTab(key);
      });
      return;
    }
    showTab(key);
  }
  function showTab(key) {
    shown = key;
    for (const t of tabs.children) t.setAttribute('aria-selected', String(t.id === `code-tab-${key}`));
    for (const [k, pane] of Object.entries(panes)) pane.hidden = k !== key;
    if (key === 'files' && !files) {
      files = filesView(panes.files, { aid, pid, name: found ? found.name : '', failed: refuse, path });
    }
    if (key === 'git') {
      if (git) git.unmount();
      git = gitView(panes.git, { aid, pid, failed: refuse });
    }
  }
  for (const [key, text] of TABS) {
    const tab = el('button', { type: 'button', role: 'tab', id: `code-tab-${key}`, class: 'chip',
      'aria-selected': String(key === first), 'aria-controls': `code-pane-${key}` }, text);
    panes[key] = el('div', { id: `code-pane-${key}`, role: 'tabpanel', 'aria-labelledby': `code-tab-${key}`,
      hidden: key !== first });
    tab.addEventListener('click', () => show(key));
    tabs.append(tab);
  }
  const list = el('ul', { id: 'code-chats', class: 'code-list code-chats' });
  panes.chats.append(list);
  main.insertBefore(el('div', { class: 'code-bar' }, add, tabs), status);
  main.append(panes.chats, panes.files, panes.git);
  show(first);

  async function reload() {
    try {
      const { items } = await api('GET', `${base(aid, pid)}/chats`);
      if (mine !== gen) return;
      list.replaceChildren(...items.map((c) => chatRow(mine, aid, pid, c, reload, status)));
    } catch (err) {
      if (mine === gen) failed(err, aid, status);
    }
  }
  await reload();
}

/* ---- чат (code-chat.js) ------------------------------------------------------------- */

async function chat(mine, aid, pid, id) {
  const { main, status, nav } = frame([['Компьютеры', '#/code'], ['Проекты', `#/code/${aid}`],
    ['Чаты', `#/code/${aid}/${pid}`]]);
  const section = el('section', { id: 'code-chat', class: 'code-chat', 'data-chat': id });
  main.append(section);
  let agents;
  let found;
  let row;
  try {
    [agents, found, row] = await Promise.all([agentsList(),
      api('GET', `${base(aid)}/projects`).then((o) => o.items.find((p) => p.pid === pid) || null),
      api('GET', `${base(aid, pid)}/chats`).then((o) => o.items.find((c) => c.chat === id) || null)]);
  } catch (err) {
    if (mine === gen) failed(err, aid, status);
    return;
  }
  if (mine !== gen) return;
  const agent = agents.find((a) => String(a.id) === aid);
  if (!agent) {
    redirect('code');
    return;
  }
  nav.replaceWith(crumbsNav([['Компьютеры', '#/code'], [agent.name, `#/code/${aid}`],
    [found ? found.name : 'Проект', `#/code/${aid}/${pid}`], [(row && row.title) || UNTITLED, null]]));
  status.remove();                           // у чата своя строка #code-status
  const fresh = freshChat === id;
  freshChat = null;                          // дальше «новый» — только по пустой истории (loadHistory)
  view = chatView(section, { aid, pid, chat: id, agent, project: found ? { pid, name: found.name } : null,
    origin: row ? row.origin : 'studio', fresh, navigate: ctx.navigate,
    closed: () => toPin(aid),
    openFile: (path) => {
      wantFile = path;
      ctx.navigate(`code/${aid}/${pid}?tab=files`);
    } });
}

/* ---- маршрут ---------------------------------------------------------------------- */

function render() {
  const mine = ++gen;
  if (view) view.unmount();
  view = null;
  shownHash = location.hash;
  shownEntry = history.state && history.state.entry;
  const path = wantFile;
  wantFile = null;
  const parts = ctx.id ? ctx.id.split('/') : [];
  const [aid, pid, id] = parts;
  const ok = parts.length <= 3 && (!aid || AID_RE.test(aid)) && (!pid || PID_RE.test(pid)) && (!id || CHAT_RE.test(id));
  if (!ok) {
    redirect('code');
    return;
  }
  // запомненный адрес живёт только до панели PIN своего компьютера: ушли куда-то ещё — забыт
  if (returnTo && (aid || !ctx.query || ctx.query.get('pin') !== returnTo.aid)) returnTo = null;
  if (id) chat(mine, aid, pid, id);
  else if (pid) {
    const tab = ctx.query && ctx.query.get('tab');
    project(mine, aid, pid, tab, tab === 'files' ? path : null);
  }
  else if (aid) projects(mine, aid);
  else computers(mine);
}

export function mount(view, context) {
  host = view;
  ctx = context;
  render();
}

export function update(context) {
  ctx = context;
  render();
}

export function unmount() {
  shownHash = null;
  gen += 1;
  if (view) view.unmount();
  view = null;
  wantFile = null;
  returnTo = null;
  host = null;
  ctx = null;
}
