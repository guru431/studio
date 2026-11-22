import { tr } from './i18n.js';
/* Панель раздела «Open WebUI» (спека 2026-11-22-studio-open-webui-design, «Страница»): учётки строками сверху, под ними —
 * дерево чатов показанной учётки. Образец — code-panel.js «Агентов».
 *
 * panelView(host, { selected, chose, opened }) → { ready, shown, accounts, select, here, title, connect, event, unmount }:
 * - учётки — GET /api/owui/accounts: точка состояния (на связи / нет связи / войдите заново; слова — для чтения с
 *   экрана), подпись, хост сервера; «⋯» — переименовать, переподключить, удалить (с подтверждением); «+ Подключить» —
 *   окно (подпись, адрес, email, пароль); 409 `certificate` — шаг сертификата: отпечаток SHA-256, владелец, срок и
 *   «Доверять этому сертификату» (повтор с cert_sha256). Пароль живёт только в поле окна, окно закрылось — его нет;
 * - показ учётки (selected, иначе первая; нажатие на строку; select(aid)) меняет только панель и зовёт
 *   opened(aid, учётка) — owui.js держит SSE показанной учётки; учёток нет — opened(null, null);
 * - учётка `relogin` (или событие link relogin, или отказ relogin) — вместо дерева поле пароля (#owui-relogin);
 * - дерево — GET …/tree: поиск (POST …/search, запрос — телом), «Новый чат», «Закреплённые», папки (раскрытие грузит
 *   GET …/folders/{fid}/chats), чаты без папки — страницы GET …/chats?page=N догружаются при прокрутке;
 *   точка у чата — идёт ответ, акцентная метка — ответ ждёт решения;
 * - event(type, data) — события SSE показанной учётки: list (дерево заново), title, active, pending, link;
 * - here(aid, chat) — открытый справа чат (aria-current); chose(aid, chat) — нажат чат или «Новый чат» (chat — null):
 *   телефон прячет панель, адрес меняет сама ссылка; title(chat) — название известного чата.
 * Названия чатов, подписи учёток и тексты сервера — только textContent и без перевода. */
import { api } from './api.js';
import { icon } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { dateOnly } from './time.js';
import { button, computerPin, el, field, openDialog, showError, words } from './ui.js';

/** Страница чатов без папки у Open WebUI 0.13: короче — страниц больше нет. */
const PAGE = 60;
const SEARCH_MS = 300;
const RELOAD_MS = 400;
/** До конца прокрутки дерева осталось меньше — следующая страница. */
const MORE_PX = 200;
const STATE_WORDS = { on: tr("на связи"), off: tr("нет связи"), relogin: tr("войдите заново") };

function clearErrors(form) {
  for (const p of form.querySelectorAll('.error[data-for]')) p.textContent = '';
}

/** Поле пароля Open WebUI — как PIN компьютера (ui.js computerPin): не password и вне <form>, у соседнего email —
 *  autocomplete off. Иначе менеджер паролей сохранил бы пароль Open WebUI под адресом Студии (и подставил бы его во
 *  вход Студии), а в это окно предложил бы сохранённый пароль Студии — и тот ушёл бы на чужой сервер. */
function secret(attrs = {}) {
  return computerPin({ name: 'password', inputmode: null, maxlength: '1024', ...attrs });
}

/** Enter в поле `box` — `go()` (вместо отправки <form>, которой здесь нет). */
function onEnter(box, go) {
  box.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.isComposing || !(event.target instanceof HTMLInputElement)) return;
    event.preventDefault();
    go();
  });
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function panelView(host, { selected: first = null, chose, opened }) {
  const controller = new AbortController();
  const { signal } = controller;
  let items = [];                  // учётки
  let shown = null;                // id показанной учётки
  let here = { aid: null, chat: null };
  const links = new Map();         // id учётки → up | down | relogin (события SSE)
  const offline = new Set();       // id учёток, чьё дерево не прочиталось: Open WebUI не ответил
  const known = new Map();         // id чата → строка дерева показанной учётки (для событий)
  let gen = 0;                     // номер показа учётки: ответы прежнего показа не рисуются
  let tree = null;                 // { page, done, loading, open: Set(fid), folders: [] }
  let reloadTimer = 0;
  let searchTimer = 0;
  let searchGen = 0;
  let reloginFor = null;           // поле пароля показано по просьбе («Переподключить») у учётки на связи

  const list = el('ul', { id: 'owui-account-list', class: 'owui-account-list' });
  const connectButton = el('button', { type: 'button', id: 'owui-connect', class: 'btn-link owui-connect',
    'aria-haspopup': 'dialog' }, icon('plus'), el('span', {}, tr("Подключить")));
  const accountsBox = el('div', { id: 'owui-accounts', class: 'owui-accounts' }, list, connectButton);

  const search = el('input', { id: 'owui-search', class: 'input owui-search', type: 'search', autocomplete: 'off',
    maxlength: '256', placeholder: tr("Поиск чатов"), 'aria-label': tr("Поиск чатов") });
  const newChat = el('a', { id: 'owui-new-chat', class: 'btn owui-new-chat', href: '#/owui' },
    icon('plus'), el('span', {}, tr("Новый чат")));
  newChat.addEventListener('click', () => chose(shown, null));
  const tools = el('div', { class: 'owui-tree-tools' }, search, newChat);
  const status = el('p', { id: 'owui-panel-status', class: 'error owui-panel-status', 'aria-live': 'polite' });
  const relogin = el('div', { id: 'owui-relogin-box', class: 'owui-relogin-box', hidden: true });
  const results = el('ul', { id: 'owui-results', class: 'owui-chats', 'aria-label': tr("Найденные чаты"), hidden: true });
  const pinnedList = el('ul', { class: 'owui-chats' });
  const pinned = el('section', { id: 'owui-pinned', class: 'owui-group', hidden: true },
    el('h3', { class: 'owui-group-head' }, tr("Закреплённые")), pinnedList);
  const foldersList = el('ul', { class: 'owui-folders' });
  const folders = el('section', { id: 'owui-folders', class: 'owui-group', hidden: true },
    el('h3', { class: 'owui-group-head' }, tr("Папки")), foldersList);
  const chatList = el('ul', { id: 'owui-chat-list', class: 'owui-chats' });
  const more = el('p', { id: 'owui-chats-more', class: 'muted small owui-more', hidden: true }, tr("Загрузка…"));
  const chats = el('section', { id: 'owui-chats', class: 'owui-group', hidden: true },
    el('h3', { class: 'owui-group-head' }, tr("Чаты")), chatList, more);
  const treeBody = el('div', { class: 'owui-tree-body' }, pinned, folders, chats);
  const treeBox = el('div', { id: 'owui-tree', class: 'owui-tree', hidden: true }, tools, status, relogin, results, treeBody);
  host.append(accountsBox, treeBox);

  connectButton.addEventListener('click', () => connect());
  treeBox.addEventListener('scroll', () => moreIfNear(), { passive: true });
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, SEARCH_MS);
  });

  /* ---- учётки ---- */

  const account = (aid) => items.find((a) => a.id === aid) || null;

  function stateOf(acc) {
    if (acc.state === 'relogin' || links.get(acc.id) === 'relogin') return 'relogin';
    if (links.get(acc.id) === 'down' || offline.has(acc.id)) return 'off';
    return 'on';
  }

  function paintAccounts() {
    list.replaceChildren(...items.map((acc) => {
      const state = stateOf(acc);
      const row = el('button', { type: 'button', class: 'owui-account', 'aria-current': acc.id === shown ? 'true' : null,
        title: [acc.who?.email, acc.version ? `Open WebUI ${acc.version}` : null].filter(Boolean).join(' · ') || null },
      el('span', { class: `owui-state ${state}` }, el('span', { class: 'sr' }, STATE_WORDS[state])),
      el('span', { class: 'owui-account-text' }, el('span', { class: 'owui-account-label' }, acc.label),
        el('span', { class: 'owui-account-host' }, hostOf(acc.url))));
      row.addEventListener('click', () => select(acc.id));
      const trigger = el('button', { type: 'button', class: 'icon-btn owui-account-more',
        'aria-label': tr("Действия с учёткой · {0}", acc.label), title: tr("Действия с учёткой") }, icon('more'));
      const menu = el('div', { class: 'owui-account-menu', 'aria-label': tr("Действия с учёткой · {0}", acc.label) });
      const popup = popupMenu(trigger, menu, { align: 'end' });
      const act = (fn) => () => { popup.close(true); fn(acc); };
      menu.append(menuItem(tr("Переименовать"), { attrs: { class: 'owui-rename' }, onSelect: act(renameDialog) }),
        menuItem(tr("Переподключить"), { attrs: { class: 'owui-relogin-item' }, onSelect: act(askPassword) }),
        menuItem(tr("Удалить"), { attrs: { class: 'owui-delete btn-danger' }, onSelect: act(deleteDialog) }));
      return el('li', { class: 'owui-account-row', 'data-id': acc.id }, row, trigger, menu);
    }));
  }

  async function loadAccounts() {
    try {
      items = await api('GET', '/api/owui/accounts', undefined, { signal });
    } catch (err) {
      if (signal.aborted) throw err;
      status.textContent = words(err);
      treeBox.hidden = false;
      tools.hidden = true;
      throw err;
    }
    paintAccounts();
    const start = account(first) ? first : items[0]?.id ?? null;
    select(start, { force: true });
    return items;
  }

  /** Показать учётку в панели: дерево или поле пароля; правая часть не меняется. */
  function select(aid, { force = false } = {}) {
    const acc = account(aid);
    if (!acc) {
      shown = null;
      gen += 1;
      paintAccounts();
      treeBox.hidden = true;
      opened(null, null);
      return;
    }
    if (acc.id === shown && !force) {
      if (stateOf(acc) !== 'relogin' && reloginFor === null) reloadTree();
      return;
    }
    shown = acc.id;
    reloginFor = null;
    search.value = '';
    paintAccounts();
    show();
    opened(acc.id, acc);
  }

  function show() {
    const acc = account(shown);
    gen += 1;
    clearTimeout(reloadTimer);
    known.clear();
    tree = null;
    treeBox.hidden = false;
    status.textContent = '';
    results.hidden = true;
    newChat.href = `#/owui/${acc.id}`;
    if (stateOf(acc) === 'relogin' || reloginFor === acc.id) {
      paintRelogin(acc);
      return;
    }
    relogin.hidden = true;
    relogin.replaceChildren();
    tools.hidden = false;
    treeBody.hidden = false;
    loadTree(acc);
  }

  /* ---- окна учёток ---- */

  function connect() {
    const label = el('input', { class: 'input', name: 'label', maxlength: '80', autocomplete: 'off' });
    const url = el('input', { class: 'input', name: 'url', type: 'url', inputmode: 'url', autocomplete: 'off',
      spellcheck: 'false', autocapitalize: 'off' });
    const email = el('input', { class: 'input', name: 'email', type: 'email', autocomplete: 'off',
      autocapitalize: 'off', spellcheck: 'false' });
    const password = secret();
    const cert = el('div', { id: 'owui-cert', class: 'owui-cert', hidden: true });
    const fail = el('p', { id: 'owui-connect-error', class: 'error', 'aria-live': 'polite' });
    const form = el('div', { id: 'owui-connect-form', class: 'owui-connect-form' },
      field(tr("Подпись"), label, 'label'),
      field(tr("Адрес сервера"), url, 'url'),
      el('p', { class: 'muted small owui-hint' }, tr("Адрес со схемой https или http, как в строке браузера.")),
      field(tr("Email в Open WebUI"), email, 'email'),
      field(tr("Пароль Open WebUI"), password, 'password'),
      el('p', { class: 'muted small owui-hint' }, tr("Пароль нужен только для входа: Studio его не сохраняет.")),
      cert, fail);
    const submit = el('button', { type: 'button', id: 'owui-connect-submit', class: 'btn btn-accent' }, tr("Подключить"));
    const dialog = openDialog(host, { id: 'owui-connect-dialog', title: tr("Подключить Open WebUI"), body: [form],
      actions: [submit] });
    let trusted = null;                          // отпечаток, которому владелец доверился на шаге сертификата
    url.addEventListener('input', () => { trusted = null; cert.hidden = true; cert.replaceChildren(); });
    const send = async (sha256 = null) => {
      if (submit.disabled) return;
      submit.disabled = true;
      fail.textContent = '';
      clearErrors(form);
      const body = { label: label.value, url: url.value.trim(), email: email.value.trim(), password: password.value };
      if (sha256) body.cert_sha256 = sha256;
      try {
        const acc = await api('POST', '/api/owui/accounts', body);
        dialog.close();
        items = [...items, acc];
        offline.delete(acc.id);
        select(acc.id, { force: true });
      } catch (err) {
        if (err.code === 'certificate' && err.detail?.sha256) {
          paintCertificate(cert, err.detail, (sha) => { trusted = sha; send(sha); });
        } else {
          showError(form, err, { label: 'label', url: 'url', unlisted: 'url', owui_login: 'password' }, 'none');
          if (!form.querySelector('.error[data-for]:not(:empty)')) fail.textContent = words(err);
        }
      } finally {
        submit.disabled = false;
      }
    };
    submit.addEventListener('click', () => send(trusted));
    onEnter(form, () => send(trusted));
    label.focus();
  }

  function paintCertificate(box, detail, trust) {
    const row = (name, value) => value ? el('div', {}, el('dt', {}, name), el('dd', {}, value)) : null;
    const yes = button(tr("Доверять этому сертификату"), { id: 'owui-cert-trust', class: 'btn' },
      () => trust(detail.sha256));
    box.replaceChildren(
      el('p', {}, tr("Система не подтвердила сертификат сервера. Сверьте отпечаток SHA-256 с сертификатом сервера и доверьтесь ему, только если они совпадают.")),
      el('dl', { class: 'owui-cert-facts' },
        el('div', {}, el('dt', {}, tr("Отпечаток SHA-256")), el('dd', {}, el('code', { id: 'owui-cert-sha256' }, detail.sha256))),
        row(tr("Владелец"), detail.subject),
        row(tr("Действует до"), Number.isFinite(detail.not_after) ? dateOnly(detail.not_after) : null)),
      yes);
    box.hidden = false;
    yes.focus();
  }

  function renameDialog(acc) {
    const input = el('input', { class: 'input', name: 'label', maxlength: '80', autocomplete: 'off', value: acc.label });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'owui-rename-save' });
    const dialog = openDialog(host, { id: 'owui-rename-dialog', title: tr("Переименовать"),
      body: [field(tr("Подпись"), input, 'label'), fail], actions: [save] });
    const submit = async () => {
      if (save.disabled) return;
      save.disabled = true;
      try {
        const fresh = await api('PATCH', `/api/owui/accounts/${acc.id}`, { label: input.value });
        items = items.map((a) => a.id === fresh.id ? fresh : a);
        paintAccounts();
        dialog.close();
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    };
    save.addEventListener('click', submit);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); submit(); }
    });
    input.focus();
    input.select();
  }

  function deleteDialog(acc) {
    const yes = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'owui-delete-confirm' });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const dialog = openDialog(host, { id: 'owui-delete-dialog', title: tr("Удалить учётку?"),
      body: [el('p', {}, tr("«{0}» исчезнет из Studio, а её сессия в Open WebUI будет отозвана. Чаты в Open WebUI останутся.",
        acc.label)), fail], actions: [yes] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/owui/accounts/${acc.id}`);
        dialog.close();
        items = items.filter((a) => a.id !== acc.id);
        links.delete(acc.id);
        offline.delete(acc.id);
        if (shown === acc.id) select(items[0]?.id ?? null, { force: true });
        else paintAccounts();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  /** «Переподключить» учётки на связи — то же поле пароля, что у `relogin`, с «Отменой». */
  function askPassword(acc) {
    reloginFor = acc.id;
    if (shown !== acc.id) {
      shown = acc.id;
      paintAccounts();
      opened(acc.id, acc);
    }
    show();
  }

  function paintRelogin(acc) {
    tools.hidden = true;
    treeBody.hidden = true;
    const needed = stateOf(acc) === 'relogin';
    const password = secret({ id: 'owui-relogin-password' });
    const submit = el('button', { type: 'button', class: 'btn btn-accent', id: 'owui-relogin-submit' }, tr("Войти"));
    const cancel = needed ? null : button(tr("Отмена"), { class: 'btn', id: 'owui-relogin-cancel' }, () => {
      reloginFor = null;
      show();
    });
    const form = el('div', { id: 'owui-relogin', class: 'owui-relogin', role: 'group',
      'aria-label': tr("Войти в Open WebUI заново") },
      el('p', {}, needed ? tr("Войдите в учётку Open WebUI заново: Studio больше не может продлить её сессию.")
        : tr("Войдите в учётку Open WebUI заново, чтобы обновить её сессию в Studio.")),
      acc.who?.email ? el('p', { class: 'muted small owui-relogin-who' }, acc.who.email) : null,
      field(tr("Пароль Open WebUI"), password, 'password'),
      el('div', { class: 'actions owui-relogin-actions' }, cancel, submit));
    const go = async () => {
      if (submit.disabled) return;
      submit.disabled = true;
      clearErrors(form);
      try {
        const fresh = await api('POST', `/api/owui/accounts/${acc.id}/login`, { password: password.value });
        password.value = '';
        items = items.map((a) => a.id === fresh.id ? fresh : a);
        links.delete(fresh.id);
        offline.delete(fresh.id);
        reloginFor = null;
        paintAccounts();
        show();
        opened(fresh.id, fresh);
      } catch (err) {
        showError(form, err, {}, 'password');
        submit.disabled = false;
      }
    };
    submit.addEventListener('click', go);
    onEnter(form, go);
    relogin.replaceChildren(form);
    relogin.hidden = false;
  }

  /** Учётке нужен вход заново (отказ relogin или событие link relogin). */
  function needRelogin(aid) {
    const acc = account(aid);
    if (!acc) return;
    acc.state = 'relogin';
    links.set(aid, 'relogin');
    paintAccounts();
    if (shown === aid) show();
  }

  /* ---- дерево ---- */

  const mine = (n, aid) => n === gen && aid === shown && !signal.aborted;
  const base = (aid) => `/api/owui/accounts/${aid}`;

  function mark(chat) {
    if (chat.pending) return el('span', { class: 'owui-chat-pending' }, tr("ждёт решения"));
    if (chat.active) return el('span', { class: 'owui-chat-active', role: 'img', 'aria-label': tr("идёт ответ"),
      title: tr("идёт ответ") });
    return null;
  }

  function chatRow(aid, chat) {
    const current = here.aid === aid && here.chat === chat.id;
    const link = el('a', { class: 'owui-chat', href: `#/owui/${aid}/${encodeURIComponent(chat.id)}`, 'data-chat': chat.id,
      'aria-current': current ? 'page' : null },
    el('span', { class: 'owui-chat-title' }, chat.title || tr("Без названия")), mark(chat));
    link.addEventListener('click', () => chose(aid, chat.id));
    return el('li', {}, link);
  }

  function remember(rows) {
    for (const chat of rows) known.set(chat.id, chat);
    return rows;
  }

  /** Все строки чата заново (он бывает и в «Закреплённых», и в найденном). */
  function repaintChat(chat) {
    for (const link of treeBox.querySelectorAll(`.owui-chat[data-chat="${CSS.escape(chat.id)}"]`)) {
      link.querySelector('.owui-chat-title').textContent = chat.title || tr("Без названия");
      link.querySelector('.owui-chat-pending, .owui-chat-active')?.remove();
      const next = mark(chat);
      if (next) link.append(next);
    }
  }

  function folderNode(aid, folder, children) {
    const open = tree.open.has(folder.id);
    const body = el('ul', { class: 'owui-folder-body', hidden: !open });
    const toggle = el('button', { type: 'button', class: 'owui-folder', 'aria-expanded': String(open),
      'data-folder': folder.id }, icon('folder'), el('span', { class: 'owui-folder-name' }, folder.name || tr("Без названия")));
    const sub = (children.get(folder.id) || []).map((f) => folderNode(aid, f, children));
    const chatsBox = el('ul', { class: 'owui-chats owui-folder-chats' });
    body.append(...sub.map((node) => el('li', {}, node)), el('li', {}, chatsBox));
    const node = el('div', { class: 'owui-folder-node' }, toggle, body);
    const load = async () => {
      const n = gen;
      chatsBox.replaceChildren(el('li', { class: 'muted small owui-folder-note' }, tr("Загрузка…")));
      try {
        const found = remember((await api('GET', `${base(aid)}/folders/${encodeURIComponent(folder.id)}/chats`,
          undefined, { signal })).chats);
        if (!mine(n, aid)) return;
        chatsBox.replaceChildren(...(found.length ? found.map((chat) => chatRow(aid, chat))
          : [el('li', { class: 'muted small owui-folder-note' }, tr("В папке нет чатов"))]));
      } catch (err) {
        if (!mine(n, aid)) return;
        chatsBox.replaceChildren(el('li', { class: 'error small owui-folder-note' }, words(err)));
      }
    };
    toggle.addEventListener('click', () => {
      const opening = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(opening));
      body.hidden = !opening;
      if (opening) {
        tree.open.add(folder.id);
        load();
      } else tree.open.delete(folder.id);
    });
    if (open) load();
    return node;
  }

  function paintFolders(aid, list) {
    const ids = new Set(list.map((f) => f.id));
    const children = new Map();
    for (const folder of list) {
      const parent = folder.parent_id && ids.has(folder.parent_id) ? folder.parent_id : null;
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(folder);
    }
    foldersList.replaceChildren(...(children.get(null) || []).map((f) => el('li', {}, folderNode(aid, f, children))));
    folders.hidden = !list.length;
  }

  async function loadTree(acc, { keep = null } = {}) {
    const n = gen, aid = acc.id;
    if (!keep) {
      pinned.hidden = folders.hidden = chats.hidden = true;
      status.classList.remove('error');
      status.classList.add('muted');
      status.textContent = tr("Загрузка…");
    }
    try {
      const found = await api('GET', `${base(aid)}/tree`, undefined, { signal });
      if (!mine(n, aid)) return;
      const wasOffline = offline.delete(aid);
      known.clear();
      tree = { page: 1, done: found.chats.length < PAGE, loading: false, open: keep || new Set() };
      status.textContent = '';
      pinnedList.replaceChildren(...remember(found.pinned).map((chat) => chatRow(aid, chat)));
      pinned.hidden = !found.pinned.length;
      paintFolders(aid, found.folders);
      chatList.replaceChildren(...remember(found.chats).map((chat) => chatRow(aid, chat)));
      chats.hidden = false;
      more.hidden = tree.done;
      if (!found.chats.length && !found.pinned.length && !found.folders.length) {
        chatList.replaceChildren(el('li', { class: 'muted small owui-folder-note' }, tr("Чатов пока нет")));
      }
      if (wasOffline) paintAccounts();
      if (search.value.trim()) runSearch();
      moreIfNear();
    } catch (err) {
      if (!mine(n, aid)) return;
      if (err.code === 'relogin') {
        needRelogin(aid);
        return;
      }
      if (err.code === 'owui_offline' && !offline.has(aid)) {
        offline.add(aid);
        paintAccounts();
      }
      status.classList.remove('muted');
      status.classList.add('error');
      status.textContent = words(err);
    }
  }

  function reloadTree() {
    const acc = account(shown);
    if (!acc || stateOf(acc) === 'relogin' || reloginFor !== null) return;
    loadTree(acc, { keep: tree ? tree.open : null });
  }

  function scheduleReload() {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(reloadTree, RELOAD_MS);
  }

  function moreIfNear() {
    if (!tree || tree.done || tree.loading || treeBody.hidden || !results.hidden) return;
    if (treeBox.scrollTop + treeBox.clientHeight < treeBox.scrollHeight - MORE_PX) return;
    loadMore();
  }

  async function loadMore() {
    const n = gen, aid = shown, page = tree.page + 1;
    tree.loading = true;
    try {
      const found = (await api('GET', `${base(aid)}/chats?page=${page}`, undefined, { signal })).chats;
      if (!mine(n, aid)) return;
      const fresh = found.filter((chat) => !known.has(chat.id));
      remember(fresh);
      chatList.append(...fresh.map((chat) => chatRow(aid, chat)));
      tree.page = page;
      tree.done = found.length < PAGE || !fresh.length;
      more.hidden = tree.done;
    } catch (err) {
      if (!mine(n, aid)) return;
      tree.done = true;            // отказ не повторяется прокруткой: дерево заново — событием или нажатием на учётку
      more.hidden = true;
      status.classList.remove('muted');
      status.classList.add('error');
      status.textContent = words(err);
      return;
    } finally {
      if (mine(n, aid) && tree) tree.loading = false;
    }
    moreIfNear();
  }

  async function runSearch() {
    const text = search.value.trim();
    const mineSearch = ++searchGen, n = gen, aid = shown;
    if (!text) {
      results.hidden = true;
      results.replaceChildren();
      treeBody.hidden = false;
      return;
    }
    try {
      const found = (await api('POST', `${base(aid)}/search`, { text }, { signal })).chats;
      if (mineSearch !== searchGen || !mine(n, aid)) return;
      for (const chat of found) if (!known.has(chat.id)) known.set(chat.id, chat);
      results.replaceChildren(...(found.length ? found.map((chat) => chatRow(aid, known.get(chat.id)))
        : [el('li', { class: 'muted small owui-folder-note' }, tr("Ничего не найдено"))]));
      results.hidden = false;
      treeBody.hidden = true;
    } catch (err) {
      if (mineSearch !== searchGen || !mine(n, aid)) return;
      results.replaceChildren(el('li', { class: 'error small owui-folder-note' }, words(err)));
      results.hidden = false;
      treeBody.hidden = true;
    }
  }

  /* ---- события показанной учётки ---- */

  function event(type, data) {
    if (shown === null || !data || typeof data !== 'object') return;
    if (type === 'link') {
      const before = links.get(shown);
      if (data.state === 'relogin') {
        needRelogin(shown);
        return;
      }
      if (data.state !== 'up' && data.state !== 'down') return;
      links.set(shown, data.state);
      paintAccounts();
      if (data.state === 'up' && (before === 'down' || offline.has(shown))) scheduleReload();   // пропущенное — заново
      return;
    }
    if (type === 'list') {
      scheduleReload();
      return;
    }
    const chat = typeof data.chat_id === 'string' ? known.get(data.chat_id) : null;
    if (type === 'title' && typeof data.title === 'string') {
      if (!chat) { scheduleReload(); return; }
      chat.title = data.title;
      repaintChat(chat);
    } else if (type === 'active') {
      if (!chat) { if (data.active) scheduleReload(); return; }
      chat.active = data.active === true;
      // пауза 0.13.4 кончается `active: false` — метка остаётся; `active: true` — решение принято, ответ продолжился
      if (chat.active) chat.pending = false;
      repaintChat(chat);
    } else if (type === 'pending') {
      if (!chat) { scheduleReload(); return; }
      chat.pending = true;
      repaintChat(chat);
    }
  }

  /* ---- для owui.js ---- */

  function markHere(aid, chat) {
    here = { aid, chat };
    for (const link of treeBox.querySelectorAll('.owui-chat')) {
      if (aid === shown && link.dataset.chat === chat) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  }

  const ready = loadAccounts();
  ready.catch(() => {});

  return {
    ready,
    shown: () => shown,
    accounts: () => items,
    select: (aid) => select(aid),
    here: markHere,
    title: (chat) => known.get(chat)?.title ?? null,
    connect,
    event,
    unmount() {
      controller.abort();
      clearTimeout(reloadTimer);
      clearTimeout(searchTimer);
      host.replaceChildren();
    },
  };
}
