/* Оболочка студии: профиль, тема, разделы по hash, меню аватара, значок «В работе» (jobs.js),
 * лупа «Поиска» (#search-open — ссылка на #/search, search.js), служебный работник.
 *
 * Маршруты: #/chat, #/chat/<id>, #/photo, #/video, #/transcribe, #/speech, #/ocr, #/translate, #/code,
 * #/code/<aid>[/<pid>[/<chat>]] (code.js), #/search, #/settings, #/settings/<раздел>, #/admin (только
 * администратору); прежний #/profile — #/settings, прочее — #/chat. Раздел — модуль с
 * `mount(el, ctx)` и `unmount()`; ctx = { me, navigate, id, query, hint }, id — хвост адреса
 * (#/chat/<id>, #/settings/<раздел>) или null, query — URLSearchParams после `?`
 * (#/video?photo=<image_id> — «Создать видео» из раздела фото с контекстом; ?id=<запись> — переход к записи из
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
 * Меню аватара (#menu) — раскрывающаяся панель, не role="menu": имя и логин, сегмент «Тема: Авто · Светлая ·
 * Тёмная» (радиокнопки — стрелки переключают; меню после выбора остаётся открытым) со строкой сохранения и
 * «Повторить», «Настройки», «Администрирование» (администратору), разделитель, «Выйти». У аватара aria-expanded;
 * Escape и нажатие вне панели закрывают её, Escape возвращает фокус аватару; фокус, ушедший из панели Tab'ом, — тоже.
 * Тема и акцент — appearance.js (выбор уходит в PATCH /api/me; отказ виден под сегментом с повтором). Тема и вид
 * ленты — в учётке: вернувшись на экран, студия перечитывает профиль (их могли сменить на другом устройстве) —
 * тема меняется сразу, вид ленты — при следующем открытии раздела (открытый не пересобирается: набранное в нём цело).
 * Перечитанный профиль — событие `studio-me` на document: чат по нему перерисовывает кнопку «Интернет». */
import { loadMe, me } from './api.js';
import { setupJobs } from './jobs.js';
import { setupCodeActivity } from './code-activity.js';
import { ensureSectionOpen, installLock } from './lock.js';
import { cancelRecording } from './mic.js';
import { choose, current as look, deviceSizeControls, follow, paintSave, retrySave, setupAppearance } from './appearance.js';
import { setupMobileNavigation } from './mobile-navigation.js';
import { passwordResetNotice } from './account-security.js';
import { setupPushSync } from './push-sync.js';
import { SECTIONS as SECTION_LIST, sectionName } from './sections-meta.js';
import { icon as svgIcon } from './icons.js';

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
const menu = document.getElementById('menu');
let current = null;      // { section, module }
let renders = 0;         // номер последнего перехода: медленная загрузка раздела не перебьёт новый
let entries = 0;         // номера меток записей истории ({ entry: n } в history.state) — для hold() раздела
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
 * (code-activity.js). Подпись остаётся видимой на узком экране и продублирована в aria-label. */
function buildRail() {
  const rail = document.getElementById('rail');
  const spacer = rail.querySelector('.spacer');
  let group = null;
  for (const s of SECTION_LIST) {
    const glyph = s.id === 'code'
      ? el('span', { class: 'tab-icon' }, svgIcon(s.id),
        el('span', { id: 'code-running-count', class: 'tab-count', 'aria-hidden': 'true', hidden: '' }),
        el('span', { id: 'code-inbox-dot', class: 'tab-dot', 'aria-hidden': 'true', hidden: '' }))
      : el('span', { class: 'tab-icon' }, svgIcon(s.id),
        el('span', { class: 'tab-count section-running-count', 'aria-hidden': 'true', hidden: '' }));
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
  document.getElementById('menu-device-controls').append(deviceSizeControls('menu'));
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
  // фокус ушёл из панели (Tab за «Выйти», Shift+Tab за аватар) — панель закрыта; щелчок по пустому месту
  // (relatedTarget null) оставляет её — его закрывает обработчик нажатия выше
  menu.addEventListener('focusout', (event) => {
    const next = event.relatedTarget;
    if (next && !menu.contains(next) && next !== avatar) openMenu(false);
  });
  menu.addEventListener('click', (event) => {   // выбор темы меню не закрывает: видно, что тема сменилась
    if (event.target.closest('a.menu-item')) openMenu(false);
  });
  const themes = [...menu.querySelectorAll('input[name="menu-theme"]')];
  for (const input of themes) input.addEventListener('change', () => { if (input.checked) choose('theme', input.value); });
  const status = document.getElementById('theme-save-status');
  const retry = document.getElementById('theme-save-retry');
  retry.addEventListener('click', retrySave);
  follow(menu, () => {
    for (const input of themes) input.checked = input.value === look().theme;
    paintSave(status, retry);
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
  setupAppearance();
  setupMenu();
  setupJobs({ navigate });
  mobileNavigation = setupMobileNavigation(me);
  window.addEventListener('hashchange', render);
  await render();
  setupCodeActivity();
  await worker;
  setupPushSync();           // подписка push, которую служебный работник не смог отправить запертым (sw.js)
  passwordResetNotice(me, navigate);   // пароль сбросил администратор — совет про аварийный выход
  document.documentElement.dataset.ready = '1';
}

start();
