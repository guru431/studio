import { tr } from './i18n.js';
/* Оболочка студии: профиль, тема, разделы по hash, меню аватара, значок «В работе» (jobs.js),
 * лупа «Поиска» (#search-open — ссылка на #/search, search.js), служебный работник.
 *
 * Маршруты: #/chat, #/chat/<id>, #/photo, #/video, #/transcribe, #/speech, #/ocr, #/translate, #/code,
 * #/code/<aid>[/<pid>[/<chat>]] (code.js), #/search, #/settings, #/settings/<раздел>, #/help (help.js), #/trash (trash.js),
 * #/myfiles (my-files.js; пункт меню виден, если у учётки есть сервер MCP с «Моими файлами»), #/admin (только
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
import { api, loadMe, me, watchSession } from './api.js';
import { setupJobs } from './jobs.js';
import { setupCodeActivity } from './code-activity.js';
import { ensureSectionOpen, installLock } from './lock.js';
import { cancelRecording } from './mic.js';
import { choose, current as look, deviceSizeControls, follow, paintSave, retrySave, setupAppearance } from './appearance.js';
import { setupMobileNavigation } from './mobile-navigation.js';
import { passwordResetNotice } from './account-security.js';
import { setupPushSync } from './push-sync.js';
import { timezoneOffer } from './timezone-offer.js';
import { SECTIONS as SECTION_LIST, sectionName } from './sections-meta.js';
import { icon as svgIcon } from './icons.js';
import { lazy, pageVersion, watchDeploys } from './stale.js';
import { cancelRefresh, claimActiveSessions, claimRequested, refreshAfterFailure, reloadStudio } from './refresh.js';
import './code-drafts.js';

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
  help: () => import('./help.js'),
  trash: () => import('./trash.js'),
  myfiles: () => import('./my-files.js'),
  admin: () => import('./admin.js'),
};
/** Экраны оболочки: имя — из реестра разделов (sections-meta.js), загрузка — модуль раздела. */
const SECTIONS = Object.fromEntries(Object.entries(LOADERS).map(([id, load]) =>
  [id, { title: sectionName(id), admin: id === 'admin', load }]));
const HINTS = Object.fromEntries(SECTION_LIST.filter((s) => s.hint).map((s) => [s.id, s.hint]));
/** Клавиши разделов (sectionKey): на macOS Option+цифра вводит символы — там Control+Option. */
const MAC = /Mac|iPhone|iPad/.test(navigator.platform);
const HOTKEY = MAC ? 'Control+Alt+' : 'Alt+';

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
    { href: `#/settings/${section}`, text: tr("Открыть настройки") });
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
    const retry = el('button', { type: 'button', class: 'btn', id: 'section-unlock-retry' }, tr("Повторить"));
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
    module = await lazy(SECTIONS[route.section].load);
  } catch (err) {               // прежняя версия статики после выкладки — плашка «Студия обновилась» (stale.js)
    module = { mount(node) { node.replaceChildren(head(SECTIONS[route.section].title),
      notice('wait', err.message, 'empty')); }, unmount() {} };
  }
  if (mine !== renders) return;
  cancelRecording({ section: true });           // запись голоса прежнего раздела не переживает его (встреча — переживает)
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
  document.title = tr("{0} — Студия", SECTIONS[section].title);
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
  const refresh = document.getElementById('refresh-app');
  refresh.append(svgIcon('refresh'));
  refresh.addEventListener('click', reloadStudio);
  const search = document.getElementById('search-open');
  search.append(svgIcon('search'));
  search.setAttribute('aria-keyshortcuts', `${HOTKEY}/`);
  search.title = tr("Поиск · {0}/", HOTKEY);
  document.getElementById('jobs').prepend(svgIcon('jobs'));
  const spacer = rail.querySelector('.spacer');
  let group = null;
  for (const s of SECTION_LIST) {
    const glyph = s.id === 'code'
      ? el('span', { class: 'tab-icon' }, svgIcon(s.id),
        el('span', { id: 'code-running-count', class: 'tab-count', 'aria-hidden': 'true', hidden: '' }),
        el('span', { id: 'code-inbox-dot', class: 'tab-dot', 'aria-hidden': 'true', hidden: '' }))
      : el('span', { class: 'tab-icon' }, svgIcon(s.id),
        el('span', { class: 'tab-count section-running-count', 'aria-hidden': 'true', hidden: '' }));
    const keys = `${HOTKEY}${SECTION_LIST.indexOf(s) + 1}`;
    const tab = el('a', { class: group !== null && s.group !== group ? 'tab group-start' : 'tab', href: `#/${s.id}`,
      'data-section': s.id, 'aria-label': s.name, 'aria-keyshortcuts': keys, title: `${s.name} · ${keys}` },
      glyph, el('span', {}, s.name));
    group = s.group;
    rail.insertBefore(tab, spacer);
  }
  meetingDot(rail, refresh);
}

/** «● Запись мм:сс» у рельса, пока идёт запись встречи (recorder.js, событие `studio-meeting`): смена раздела её не
 * прерывает, точка ведёт обратно в «Расшифровку». На телефоне — плашкой над нижними вкладками. */
function meetingDot(rail, before) {
  const time = el('span', { class: 'rec-dot-time' });
  const dot = el('a', { id: 'rec-dot', class: 'rec-dot', href: '#/transcribe', hidden: '' },
    el('span', { class: 'rec-dot-mark', 'aria-hidden': 'true' }), el('span', {}, tr("Запись")), time);
  rail.insertBefore(dot, before);
  document.addEventListener('studio-meeting', ({ detail }) => {
    dot.hidden = !detail;
    if (!detail) return;
    const s = Math.floor(detail.seconds);
    const mmss = `${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
    time.textContent = s >= 3600 ? `${Math.floor(s / 3600)}:${mmss}` : mmss;
    dot.classList.toggle('paused', detail.paused);
    const label = detail.paused ? tr("Запись на паузе · {0}", time.textContent) : tr("Идёт запись · {0}", time.textContent);
    dot.setAttribute('aria-label', label);
    dot.title = label;
  });
}

/** Клавиши разделов на компьютере: Alt+1…8 — разделы в порядке рельса (реестр sections-meta.js), Alt+/ — «Поиск»;
 * на macOS — Control+Option (Option+цифра там вводит символы). По event.code — работают и в русской раскладке.
 * Не срабатывают в открытом окне (dialog), при наборе IME, с Shift/Meta (и Ctrl вне macOS: Ctrl+Alt — это AltGr) и в
 * contenteditable. В обычном поле работают: Alt+цифра текста не вводит, а поле задания «Агентов» и «Чата» держит фокус.
 * Клавиши «Агентов» (code.js shortcut: Ctrl+Alt+←/→, Ctrl+K) с этими не пересекаются. */
function sectionKey(event) {
  if (event.defaultPrevented || event.isComposing || event.repeat || !event.altKey || event.shiftKey || event.metaKey
    || event.ctrlKey !== MAC) return;
  if (document.querySelector('dialog[open]')
    || (event.target instanceof Element && event.target.closest('[contenteditable]:not([contenteditable="false"])'))) return;
  const digit = /^Digit([1-9])$/.exec(event.code);
  const target = event.code === 'Slash' ? document.getElementById('search-open')
    : digit ? document.querySelectorAll('#rail a.tab[data-section]')[Number(digit[1]) - 1] : null;
  if (!target) return;
  event.preventDefault();
  location.hash = target.getAttribute('href');
}

/** «Мои файлы» в меню — только при сервере MCP с контрактом `studio/files` (GET /api/my-files/servers; каталоги
 * серверов — из кэша сервера). Сбой проверки пункт не меняет. */
function checkMyFiles() {
  api('GET', '/api/my-files/servers').then((items) => {
    document.getElementById('menu-my-files').hidden = !(Array.isArray(items) && items.length);
  }).catch(() => {});
}

function openMenu(open) {
  if (open) {
    const version = document.getElementById('menu-version');
    version.textContent = pageVersion ? `Studio ${pageVersion}` : '';   // версия страницы, не перечитанного профиля
    version.hidden = !pageVersion;
    checkMyFiles();
  }
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
  checkMyFiles();
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
  watchSession();            // выход в другой вкладке этого браузера — на вход сразу
  try {
    await loadMe();
  } catch (err) {
    if (err.code !== 'login') retryLater(err, worker);
    return;
  }
  await enter(worker);
}

/** Профиль не пришёл: даём повторить вход в оболочку и запускаем общее обновление. */
function retryLater(first, worker) {
  let asking = false;
  const say = (err) => {
    const box = notice('wait', tr("Студия не открылась: {0}.", err.message), 'empty');
    const again = el('button', { type: 'button', class: 'btn btn-accent', id: 'start-retry' }, tr("Повторить"));
    again.addEventListener('click', retry);
    box.append(again);
    view.replaceChildren(box);
  };
  async function retry() {
    if (asking || document.visibilityState !== 'visible') return;
    asking = true;
    cancelRefresh();         // повтор на месте: перезагрузка refresh.js по тому же `online` открыла бы студию второй раз
    try {
      await loadMe();
    } catch (err) {
      asking = false;
      if (err.code !== 'login') {
        say(err);
        refreshAfterFailure(err);
      }
      return;
    }
    window.removeEventListener('online', retry);
    document.removeEventListener('visibilitychange', retry);
    await enter(worker);
  }
  say(first);
  refreshAfterFailure(first);
  window.addEventListener('online', retry);
  document.addEventListener('visibilitychange', retry);
}

/** Профиль есть: тема, меню, раздел по адресу. */
async function enter(worker) {
  setupAppearance();
  watchDeploys();            // выкладка при возвращении на экран — плашка «Студия обновилась»
  setupMenu();
  setupJobs({ navigate });
  mobileNavigation = setupMobileNavigation(me);
  window.addEventListener('hashchange', render);
  document.addEventListener('keydown', sectionKey);
  await render();
  if (claimRequested) {
    let failed;
    try { failed = await claimActiveSessions(); }
    catch { failed = true; }
    if (failed) {
      view.prepend(el('p', { id: 'refresh-status', class: 'status error', role: 'status' },
        tr("Не все активные сессии удалось перенести сюда. Студия повторит обновление.")));
      refreshAfterFailure(undefined, { claim: true });
    }
  }
  const offer = timezoneOffer();   // пояс устройства не совпал с учёткой — предложить свой
  if (offer) view.prepend(offer);
  setupCodeActivity();
  await worker;
  setupPushSync();           // подписка push, которую служебный работник не смог отправить запертым (sw.js)
  passwordResetNotice(me, navigate);   // пароль сбросил администратор — совет про аварийный выход
  document.documentElement.dataset.ready = '1';
}

start();
