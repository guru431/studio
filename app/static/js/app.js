/* Оболочка студии: профиль, тема, разделы по hash, меню аватара, значок «В работе» (jobs.js),
 * лупа «Поиска» (#search-open — ссылка на #/search, search.js), служебный работник.
 *
 * Маршруты: #/chat, #/chat/<id>, #/photo, #/video, #/transcribe, #/speech, #/ocr, #/translate, #/code,
 * #/code/<aid>[/<pid>[/<chat>]] (code.js), #/search, #/settings, #/settings/<раздел>, #/admin (только
 * администратору); прежний #/profile — #/settings, прочее — #/chat. Раздел — модуль с
 * `mount(el, ctx)` и `unmount()`; ctx = { me, navigate, id, query, hint }, id — хвост адреса
 * (#/chat/<id>, #/settings/<раздел>) или null, query — URLSearchParams после `?`
 * (#/video?source=<media_id> — «Оживить» из раздела фото; ?id=<запись> — переход к записи из
 * «В работе», jobs.js: раздел открывает её и убирает ?id из адреса). Смена id в том же разделе зовёт
 * `update(ctx)` модуля, если он его экспортирует, иначе раздел монтируется заново. Раздел с `hold()`
 * может задержать любую смену адреса (true — он сам вернул адрес и спросит; code.js — несохранённая правка).
 * Раздел без готовой модели (me.capabilities.photo === false) открывается всё равно — история
 * читается, скачивается и удаляется; hint(cls) — подсказка со ссылкой на настройки раздела, её
 * раздел ставит вместо поля создания (у готового раздела hint — null). Готовность
 * берётся при открытии раздела: открытый не пересобирается, когда профиль перечитан.
 * Готовая оболочка ставит <html data-ready="1"> (по нему ждут браузерные тесты). Профиль при запуске
 * не пришёл (нет связи) — «Повторить», и сам повтор при появлении сети и возвращении на экран.
 *
 * Тема — пунктом #theme-toggle меню аватара (сразу под именем; подпись — что включит нажатие, меню после
 * нажатия остаётся открытым): из «auto» первое нажатие ставит противоположную видимой,
 * дальше «Бумага» ↔ «Графит»; соседний выбор режима возвращает «Как на устройстве». Выбор уходит
 * в PATCH /api/me; отказ виден рядом с повтором, несохранённый выбор сохраняется в открытом окне.
 * Тема и вид ленты — в учётке: вернувшись
 * на экран, студия перечитывает профиль (их могли сменить на другом устройстве) — тема меняется
 * сразу, вид ленты — при следующем открытии раздела (открытый не пересобирается: набранное в нём цело).
 * Перечитанный профиль — событие `studio-me` на document: чат по нему перерисовывает кнопку «Интернет». */
import { api, loadMe, me } from './api.js';
import { setupJobs } from './jobs.js';
import { setupCodeActivity } from './code-activity.js';
import { ensureSectionOpen, installLock } from './lock.js';
import { cancelRecording } from './mic.js';
import { setupAppearance } from './appearance.js';
import { setupMobileNavigation } from './mobile-navigation.js';
import { passwordResetNotice } from './account-security.js';
import { setupPushSync } from './push-sync.js';
import { SECTIONS as SECTION_LIST, sectionName } from './sections-meta.js';
import { icon as svgIcon } from './icons.js';

const THEME_KEY = 'studio-theme';
const THEME_COLORS = { light: '#fbfaf7', dark: '#1b1b1b' };
const THEME_ICONS = {
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
    + 'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
};

const LOADERS = {
  chat: () => import('./chat.js'),
  photo: () => import('./photo.js'),
  video: () => import('./video.js'),
  speech: () => import('./speech.js'),
  transcribe: () => import('./transcribe.js'),
  ocr: () => import('./ocr.js'),
  translate: () => import('./translate.js'),
  code: () => import('./code.js'),
  search: () => import('./search.js'),
  settings: () => import('./settings.js'),
  admin: () => import('./admin.js'),
};
/** Экраны оболочки: имя — из реестра разделов (sections-meta.js), загрузка — модуль раздела. */
const SECTIONS = Object.fromEntries(Object.entries(LOADERS).map(([id, load]) =>
  [id, { title: sectionName(id), admin: id === 'admin', load }]));
const HINTS = Object.fromEntries(SECTION_LIST.filter((s) => s.hint).map((s) => [s.id, s.hint]));

const view = document.getElementById('view');
const preloadSection = document.getElementById('theme-start')?.preloadSection || (() => {});
const avatar = document.getElementById('avatar');
const toggle = document.getElementById('theme-toggle');
const darkMedia = matchMedia('(prefers-color-scheme: dark)');
const menu = document.getElementById('menu');
let current = null;      // { section, module }
let renders = 0;         // номер последнего перехода: медленная загрузка раздела не перебьёт новый
let entries = 0;         // номера меток записей истории ({ entry: n } в history.state) — для hold() раздела
let themeMode = null;
let pendingTheme = null;
let themeRevision = 0;
let savingTheme = false;
let mobileNavigation = null;
let announced = false;   // первая отрисовка: фокус остаётся у страницы

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

function icon(name) {
  return el('span', { class: 'badge', 'aria-hidden': 'true' }, svgIcon(name));
}

function head(title) {
  return el('header', { class: 'head' }, el('h1', {}, title));
}

function notice(iconName, text, kind, link) {
  const box = el('div', { class: kind }, icon(iconName), el('p', {}, text));
  if (link) box.append(el('a', { class: 'btn btn-accent', href: link.href }, link.text));
  return box;
}

/** Подсказка раздела без модели: новый узел на каждый вызов, `hint-bar` — полосой на месте поля
 * создания, без него — во весь пустой вид; раздел готов — null. */
function hintOf(section) {
  if (!(section in HINTS) || me.capabilities[section]) return null;
  return (cls = '') => notice(section, HINTS[section], `hint ${cls}`.trim(),
    { href: `#/settings/${section}`, text: 'Открыть настройки' });
}

function navigate(path) {
  location.hash = '#/' + String(path).replace(/^#?\/?/, '');
}

function applyTheme(theme) {
  const root = document.documentElement;
  const fixed = theme === 'dark' || theme === 'light';
  if (fixed) root.dataset.theme = theme;
  else delete root.dataset.theme;
  try {
    if (fixed) localStorage.setItem(THEME_KEY, theme);
    else localStorage.removeItem(THEME_KEY);
  } catch {
    // хранилище закрыто — тема всё равно применена
  }
  const [light, dark] = document.querySelectorAll('meta[name="theme-color"]');
  light.content = fixed ? THEME_COLORS[theme] : THEME_COLORS.light;
  dark.content = fixed ? THEME_COLORS[theme] : THEME_COLORS.dark;
  const scheme = (name) => (fixed ? (theme === name ? 'all' : 'not all') : `(prefers-color-scheme: ${name})`);
  document.getElementById('hl-paper').media = scheme('light');
  document.getElementById('hl-graphite').media = scheme('dark');
}

function visibleTheme() {
  const theme = themeMode ?? me.theme;
  if (theme === 'dark' || theme === 'light') return theme;
  return darkMedia.matches ? 'dark' : 'light';
}

function paintToggle() {
  const dark = visibleTheme() === 'dark';
  toggle.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${THEME_ICONS[dark ? 'sun' : 'moon']}</svg>`;   // константа
  toggle.append(dark ? 'Светлая тема' : 'Тёмная тема');
  document.getElementById('theme-mode').value = themeMode ?? me.theme;
}

async function saveTheme() {
  if (savingTheme || pendingTheme === null) return;
  savingTheme = true;
  const status = document.getElementById('theme-save-status');
  const retry = document.getElementById('theme-save-retry');
  retry.hidden = true;
  status.classList.remove('error');
  status.textContent = 'Сохраняем тему…';
  while (pendingTheme !== null) {
    const choice = pendingTheme;
    try {
      // Последовательные запросы: медленный ответ старого выбора не перезапишет новый.
      await api('PATCH', '/api/me', { theme: choice });
    } catch {
      if (pendingTheme !== choice) continue;
      status.classList.add('error');
      status.textContent = 'Не удалось сохранить тему. Выбор действует в этом окне.';
      retry.hidden = false;
      savingTheme = false;
      return;
    }
    if (pendingTheme === choice) pendingTheme = null;
  }
  savingTheme = false;
  status.textContent = 'Тема сохранена для учётки.';
}

function chooseTheme(next) {
  themeMode = me.theme = pendingTheme = next;
  themeRevision++;
  applyTheme(next);
  paintToggle();
  saveTheme();
}

function setupTheme() {
  setupAppearance();
  themeMode = me.theme;
  paintToggle();
  toggle.addEventListener('click', () => {
    chooseTheme(visibleTheme() === 'dark' ? 'light' : 'dark');
  });
  document.getElementById('theme-mode').addEventListener('change', (event) => chooseTheme(event.target.value));
  document.getElementById('theme-save-retry').addEventListener('click', saveTheme);
  window.addEventListener('online', saveTheme);
  darkMedia.addEventListener('change', paintToggle);
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    const revision = themeRevision;
    const hadPendingTheme = pendingTheme !== null || savingTheme;
    try {
      await loadMe();
    } catch {
      return;              // нет связи — остаётся прежнее; сессия кончилась — api.js увёл на вход
    }
    if (hadPendingTheme || pendingTheme !== null || revision !== themeRevision) me.theme = themeMode;
    else themeMode = me.theme;
    applyTheme(themeMode);
    paintToggle();
    document.dispatchEvent(new Event('studio-me'));   // открытый раздел (чат: ключ Exa) перерисует своё
  });
}

function parse(hash) {
  const match = /^#\/([a-z]+)(?:\/([^?]+))?(?:\?(.*))?$/.exec(hash);
  const query = new URLSearchParams(match ? match[3] || '' : '');
  if (!match) return { section: '', id: null, query };
  try {
    return { section: match[1], id: match[2] ? decodeURIComponent(match[2]) : null, query };
  } catch {
    return { section: '', id: null, query };   // битый %-код в адресе — на раздел по умолчанию
  }
}

function allowed({ section, id }) {
  const def = SECTIONS[section];
  return def && !(def.admin && !me.is_admin)
    && (id === null || section === 'chat' || section === 'settings' || section === 'code');
}

function revealView() {
  view.style.visibility = '';
  view.inert = false;
  // WebKit сохраняет hidden у потомков, созданных под шторкой. Явно обновляем наследование.
  // inherit оставляет их скрытыми, если lock.js всё ещё держит шторку.
  if (current?.section === 'settings') {
    getComputedStyle(view).visibility;
    for (const child of view.children) child.style.visibility = 'inherit';
  }
}

async function render() {
  if (current && current.module.hold && current.module.hold()) {
    // Несохранённый файл вернул прежний адрес. Его диалог и сохранённые узлы тоже защищены PIN.
    const held = ++renders;
    view.style.visibility = 'hidden';
    view.inert = true;
    try {
      await ensureSectionOpen(current.section);
    } catch {
      return;                // данные остаются скрыты; следующая попытка перехода повторит проверку
    }
    if (held === renders) {
      revealView();
    }
    return;
  }
  let route = parse(location.hash);
  if (route.section === 'profile') {            // прежний адрес профиля — настройки
    route = { section: 'settings', id: null, query: new URLSearchParams() };
    history.replaceState(history.state, '', '#/settings');
  }
  if (!allowed(route)) {
    route = { section: 'chat', id: null, query: new URLSearchParams() };
    history.replaceState(history.state, '', '#/chat');
  }
  const mine = ++renders;
  preloadSection(route.section);
  // Проверка идёт до update/mount: старые узлы и кэш защищённого раздела не мелькают при переходе.
  view.style.visibility = 'hidden';
  view.inert = true;
  const guardedSection = route.section === 'settings' && route.id === 'cleaner' ? 'security'
    : route.section === 'settings' && route.id in SECTIONS && route.id !== 'other' ? route.id : route.section;
  try {
    await ensureSectionOpen(guardedSection);
  } catch (err) {
    if (mine !== renders) return;
    if (current) current.module.unmount();
    current = null;
    const retry = el('button', { type: 'button', class: 'btn', id: 'section-unlock-retry' }, 'Повторить');
    retry.addEventListener('click', render);
    view.replaceChildren(head(SECTIONS[route.section].title), notice('wait', err.message, 'empty'), retry);
    revealView();
    announce(route.section);
    return;
  }
  if (mine !== renders) return;
  // метка записи: hold() раздела отличает «Назад»/«Вперёд» (запись с меткой) от новой записи; разделы, меняя адрес
  // своей записи, держат history.state
  if (!(history.state && history.state.entry)) history.replaceState({ ...history.state, entry: ++entries }, '');
  for (const tab of document.querySelectorAll('#rail .tab, #search-open')) {
    if (tab.dataset.section === route.section) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
  mobileNavigation?.setActive(route.section);
  const ctx = { me, navigate, id: route.id, query: route.query, hint: hintOf(route.section) };
  if (current && current.section === route.section && current.module.update) {
    await current.module.update(ctx);
    if (mine !== renders) return;
    revealView();
    return;
  }
  let module;
  try {
    module = await SECTIONS[route.section].load();
  } catch {
    module = { mount(node) { node.replaceChildren(head(SECTIONS[route.section].title),
      notice('wait', 'Раздел не загрузился. Проверьте связь и обновите страницу.', 'empty')); }, unmount() {} };
  }
  if (mine !== renders) return;
  cancelRecording();                            // запись голоса прежнего раздела не переживает его
  if (current) current.module.unmount();
  view.replaceChildren();
  current = { section: route.section, module };
  module.mount(view, ctx);
  revealView();
  announce(route.section);
}

/** Показан новый раздел: имя во вкладке браузера и фокус на его заголовке — диктор слышит, куда перешли
 * (WCAG 2.4.2, 2.4.3). Фокус — кадром позже и только если раздел не взял его сам (поле ввода на компьютере,
 * ui.js focusDesktop) и он не внутри раздела; первая отрисовка страницы фокус не трогает. */
function announce(section) {
  document.title = `${SECTIONS[section].title} — Студия`;
  if (!announced) { announced = true; return; }
  const mine = renders;
  requestAnimationFrame(() => {
    const active = document.activeElement;
    if (mine !== renders || view.inert || (active && active !== document.body && view.contains(active))
      || document.querySelector('dialog[open]')) return;
    const target = view.querySelector('h1') || view;
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    target.focus({ preventScroll: true });
  });
}

/** Вкладки рельса — из реестра разделов, группами; у «Агентов» — число работающих сессий и точка входящих
 * (code-activity.js). Подпись — и текстом, и в aria-label: на узком экране в режиме «все разделы» видны
 * только значки (app.css). */
function buildRail() {
  const rail = document.getElementById('rail');
  const spacer = rail.querySelector('.spacer');
  let group = null;
  for (const s of SECTION_LIST) {
    const glyph = s.id === 'code'
      ? el('span', { class: 'tab-icon' }, svgIcon(s.id),
        el('span', { id: 'code-running-count', class: 'tab-count', 'aria-hidden': 'true', hidden: '' }),
        el('span', { id: 'code-inbox-dot', class: 'tab-dot', 'aria-hidden': 'true', hidden: '' }))
      : svgIcon(s.id);
    const tab = el('a', { class: group !== null && s.group !== group ? 'tab group-start' : 'tab', href: `#/${s.id}`,
      'data-section': s.id, 'aria-label': s.name }, glyph, el('span', {}, s.name));
    group = s.group;
    rail.insertBefore(tab, spacer);
  }
}

function openMenu(open) {
  menu.hidden = !open;
  avatar.setAttribute('aria-expanded', String(open));
}

function setupMenu() {
  avatar.textContent = (me.name || me.login).slice(0, 1).toUpperCase();
  document.getElementById('menu-name').textContent = me.name || me.login;
  document.getElementById('menu-login').textContent = me.login;
  document.getElementById('menu-login').hidden = !me.name || me.name === me.login;
  document.getElementById('menu-admin').hidden = !me.is_admin;
  avatar.addEventListener('click', (event) => {
    event.stopPropagation();
    openMenu(menu.hidden);
  });
  document.addEventListener('click', (event) => {     // в фазе захвата: лента в режиме выделения глушит нажатие
    if (!menu.hidden && !menu.contains(event.target) && !avatar.contains(event.target)) openMenu(false);
  }, true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !menu.hidden) {
      openMenu(false);
      avatar.focus();
    }
  });
  menu.addEventListener('click', (event) => {   // пункт темы (button) меню не закрывает: видно, что тема сменилась
    if (event.target.closest('a.menu-item')) openMenu(false);
  });
}

async function start() {
  buildRail();
  // Публичный код выбранного раздела можно получать параллельно с профилем, до показа любых данных.
  if (parse(location.hash).section === 'code') SECTIONS.code.load().catch(() => {});
  const worker = 'serviceWorker' in navigator
    ? navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => null) : null;
  installLock();             // до профиля: запертая студия покажет экран блокировки раньше раздела
  try {
    await loadMe();
  } catch (err) {
    if (err.code !== 'login') retryLater(err, worker);
    return;
  }
  await enter(worker);
}

/** Профиль не пришёл (нет связи): в PWA обновить страницу нечем — «Повторить», а ещё сам повтор при
 * появлении сети и возвращении на экран. По таймеру не повторяет: без связи запросы не копятся. */
function retryLater(first, worker) {
  let asking = false;
  const say = (err) => {
    const box = notice('wait', `Студия не открылась: ${err.message}.`, 'empty');
    const again = el('button', { type: 'button', class: 'btn btn-accent', id: 'start-retry' }, 'Повторить');
    again.addEventListener('click', retry);
    box.append(again);
    view.replaceChildren(box);
  };
  async function retry() {
    if (asking || document.visibilityState !== 'visible') return;
    asking = true;
    try {
      await loadMe();
    } catch (err) {
      asking = false;
      if (err.code !== 'login') say(err);
      return;
    }
    window.removeEventListener('online', retry);
    document.removeEventListener('visibilitychange', retry);
    await enter(worker);
  }
  say(first);
  window.addEventListener('online', retry);
  document.addEventListener('visibilitychange', retry);
}

/** Профиль есть: тема, меню, раздел по адресу. */
async function enter(worker) {
  applyTheme(me.theme);
  setupMenu();
  setupJobs({ navigate });
  setupTheme();
  mobileNavigation = setupMobileNavigation(me, () => openMenu(false));
  window.addEventListener('hashchange', render);
  await render();
  setupCodeActivity();
  await worker;
  setupPushSync();           // подписка push, которую служебный работник не смог отправить запертым (sw.js)
  passwordResetNotice(me, navigate);   // пароль сбросил администратор — совет про аварийный выход
  document.documentElement.dataset.ready = '1';
}

start();
