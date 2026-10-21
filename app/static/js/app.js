/* Оболочка студии: профиль, тема, разделы по hash, меню аватара, служебный работник.
 *
 * Маршруты: #/chat, #/chat/<id>, #/photo, #/video, #/transcribe, #/speech, #/settings, #/settings/<раздел>, #/admin (только
 * администратору); прежний #/profile — #/settings, прочее — #/chat. Раздел — модуль с
 * `mount(el, ctx)` и `unmount()`; ctx = { me, navigate, id, query }, id — хвост адреса
 * (#/chat/<id>, #/settings/<раздел>) или null, query — URLSearchParams после `?`
 * (#/video?source=<media_id> — «Оживить» из раздела фото). Смена id в том же разделе зовёт
 * `update(ctx)` модуля, если он его экспортирует, иначе раздел монтируется заново.
 * Раздел без готовой модели (me.capabilities.photo === false) открывается подсказкой со ссылкой
 * на настройки раздела. Готовая оболочка ставит <html data-ready="1"> (по нему ждут браузерные тесты).
 *
 * Тема — кнопкой #theme-toggle над аватаром: из «auto» первое нажатие ставит противоположную видимой,
 * дальше «Бумага» ↔ «Графит»; выбор уходит в PATCH /api/me. Тема и вид ленты — в учётке: вернувшись
 * на экран, студия перечитывает профиль (их могли сменить на другом устройстве) — тема меняется
 * сразу, вид ленты — при следующем открытии раздела (открытый не пересобирается: набранное в нём цело). */
import { api, loadMe, me } from './api.js';
import { installLock } from './lock.js';
import { cancelRecording } from './mic.js';

const THEME_KEY = 'studio-theme';
const THEME_COLORS = { light: '#fbfaf7', dark: '#1b1b1b' };
const HINTS = {
  chat: 'Чтобы писать в чат, выберите модель в настройках раздела «Чат»',
  photo: 'Чтобы генерировать фото, выберите модель в настройках раздела «Фото»',
  video: 'Чтобы делать видео, выберите модель в настройках раздела «Видео»',
  transcribe: 'Чтобы расшифровывать записи, выберите модель в настройках раздела «Расшифровка»',
  speech: 'Чтобы озвучивать текст, выберите модель в настройках раздела «Озвучка»',
};
const ICONS = {
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  photo: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  transcribe: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
  speech: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
};
const THEME_ICONS = {
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
    + 'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
};

const SECTIONS = {
  chat: { title: 'Чат', load: () => import('./chat.js') },
  photo: { title: 'Фото', load: () => import('./photo.js') },
  video: { title: 'Видео', load: () => import('./video.js') },
  transcribe: { title: 'Расшифровка', load: () => import('./transcribe.js') },
  speech: { title: 'Озвучка', load: () => import('./speech.js') },
  settings: { title: 'Настройки', load: () => import('./settings.js') },
  admin: { title: 'Панель', admin: true, load: () => import('./admin.js') },
};

const view = document.getElementById('view');
const avatar = document.getElementById('avatar');
const toggle = document.getElementById('theme-toggle');
const darkMedia = matchMedia('(prefers-color-scheme: dark)');
const menu = document.getElementById('menu');
let current = null;      // { section, module }
let renders = 0;         // номер последнего перехода: медленная загрузка раздела не перебьёт новый

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

function hintModule(section) {
  return {
    mount(node) {
      node.replaceChildren(head(SECTIONS[section].title),
        notice(section, HINTS[section], 'hint', { href: `#/settings/${section}`, text: 'Открыть настройки' }));
    },
    unmount() {},
  };
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
  if (me.theme === 'dark' || me.theme === 'light') return me.theme;
  return darkMedia.matches ? 'dark' : 'light';
}

function paintToggle() {
  const dark = visibleTheme() === 'dark';
  const label = dark ? 'Светлая тема' : 'Тёмная тема';
  toggle.setAttribute('aria-label', label);
  toggle.title = label;
  toggle.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${THEME_ICONS[dark ? 'sun' : 'moon']}</svg>`;   // константа
}

function setupTheme() {
  paintToggle();
  toggle.addEventListener('click', () => {
    const next = visibleTheme() === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    me.theme = next;
    paintToggle();
    // тема уже применена и лежит в localStorage; без сети сохранится при следующем нажатии
    api('PATCH', '/api/me', { theme: next }).catch(() => {});
  });
  darkMedia.addEventListener('change', paintToggle);
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    try {
      await loadMe();
    } catch {
      return;              // нет связи — остаётся прежнее; сессия кончилась — api.js увёл на вход
    }
    applyTheme(me.theme);
    paintToggle();
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
  return def && !(def.admin && !me.is_admin) && (id === null || section === 'chat' || section === 'settings');
}

async function render() {
  let route = parse(location.hash);
  if (route.section === 'profile') {            // прежний адрес профиля — настройки
    route = { section: 'settings', id: null, query: new URLSearchParams() };
    history.replaceState(null, '', '#/settings');
  }
  if (!allowed(route)) {
    route = { section: 'chat', id: null, query: new URLSearchParams() };
    history.replaceState(null, '', '#/chat');
  }
  for (const tab of document.querySelectorAll('#rail .tab')) {
    if (tab.dataset.section === route.section) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
  const ctx = { me, navigate, id: route.id, query: route.query };
  if (current && current.section === route.section && current.module.update) {
    current.module.update(ctx);
    return;
  }
  const mine = ++renders;
  let module;
  try {
    module = route.section in HINTS && !me.capabilities[route.section]
      ? hintModule(route.section) : await SECTIONS[route.section].load();
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
  document.addEventListener('click', (event) => {
    if (!menu.hidden && !menu.contains(event.target)) openMenu(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !menu.hidden) {
      openMenu(false);
      avatar.focus();
    }
  });
  menu.addEventListener('click', (event) => {
    if (event.target.closest('a.menu-item')) openMenu(false);
  });
}

async function start() {
  const worker = 'serviceWorker' in navigator
    ? navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => null) : null;
  installLock();             // до профиля: запертая студия покажет экран блокировки раньше раздела
  try {
    await loadMe();
  } catch (err) {
    if (err.code !== 'login') {
      view.replaceChildren(notice('wait', `Студия не открылась: ${err.message}. Обновите страницу.`, 'empty'));
    }
    return;
  }
  applyTheme(me.theme);
  setupMenu();
  setupTheme();
  window.addEventListener('hashchange', render);
  await render();
  await worker;
  document.documentElement.dataset.ready = '1';
}

start();
