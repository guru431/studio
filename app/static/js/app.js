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
import { choose, current as look, follow, paintSave, retrySave, setupAppearance } from './appearance.js';
import { setupMobileNavigation } from './mobile-navigation.js';
import { passwordResetNotice } from './account-security.js';
import { setupPushSync } from './push-sync.js';

const HINTS = {
  chat: 'Чтобы писать в чат, выберите модель в настройках раздела «Чат»',
  photo: 'Чтобы генерировать фото, выберите модель в настройках раздела «Фото»',
  video: 'Чтобы делать видео, выберите модель в настройках раздела «Видео»',
  transcribe: 'Чтобы расшифровывать записи, выберите модель в настройках раздела «Разговоры»',
  speech: 'Чтобы озвучивать текст, выберите модель в настройках раздела «Речь»',
  ocr: 'Чтобы распознавать сканы, выберите модель в настройках раздела «Сканы»',
  translate: 'Чтобы переводить, выберите модель в настройках раздела «Перевод»',
};
const ICONS = {
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  photo: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  transcribe: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
  speech: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
  ocr: '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M8 9h8M8 12h8M8 15h5"/>',
  translate: '<path d="M4 7h7M7.5 5v2M5 15c2-2 4-5 4.5-8M6 11c1 2 3 3 5 4"/><path d="M13 20l3.5-8 3.5 8M14.2 17.5h4.6"/>',
  code: '<path d="M4 6l5 5-5 5M11 18h9"/>',
};

const SECTIONS = {
  chat: { title: 'Чат', load: () => import('./chat.js') },
  photo: { title: 'Фото', load: () => import('./photo.js') },
  video: { title: 'Видео', load: () => import('./video.js') },
  speech: { title: 'Речь', load: () => import('./speech.js') },
  transcribe: { title: 'Разговоры', load: () => import('./transcribe.js') },
  ocr: { title: 'Распознавание', load: () => import('./ocr.js') },
  translate: { title: 'Перевод', load: () => import('./translate.js') },
  code: { title: 'Агенты', load: () => import('./code.js') },
  search: { title: 'Поиск', load: () => import('./search.js') },
  settings: { title: 'Настройки', load: () => import('./settings.js') },
  admin: { title: 'Панель', admin: true, load: () => import('./admin.js') },
};

const view = document.getElementById('view');
const preloadSection = document.getElementById('theme-start')?.preloadSection || (() => {});
const avatar = document.getElementById('avatar');
const menu = document.getElementById('menu');
let current = null;      // { section, module }
let renders = 0;         // номер последнего перехода: медленная загрузка раздела не перебьёт новый
let entries = 0;         // номера меток записей истории ({ entry: n } в history.state) — для hold() раздела
let mobileNavigation = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

function icon(name) {
  const box = el('span', { class: 'badge', 'aria-hidden': 'true' });
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${ICONS[name]}</svg>`;   // разметка — константа выше
  return box;
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
