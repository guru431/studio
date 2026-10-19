/* Оболочка студии: профиль, тема, разделы по hash, меню аватара, служебный работник.
 *
 * Маршруты: #/chat, #/chat/<id>, #/photo, #/video, #/profile, #/admin (только администратору);
 * прочее — #/chat. Раздел — модуль с `mount(el, ctx)` и `unmount()`; ctx = { me, navigate, id,
 * applyTheme }, id — хвост адреса (#/chat/<id>) или null. Смена id в том же разделе зовёт
 * `update(ctx)` модуля, если он его экспортирует, иначе раздел монтируется заново.
 * Раздел без возможности (me.capabilities.photo === false) открывается подсказкой со ссылкой
 * на профиль. Готовая оболочка ставит <html data-ready="1"> (по нему ждут браузерные тесты). */
import { loadMe, me } from './api.js';

const THEME_KEY = 'studio-theme';
const THEME_COLORS = { light: '#fbfaf7', dark: '#1b1b1b' };
const HINTS = {
  chat: 'Чтобы писать в чат, добавьте в профиле подключение: шлюз srvllm или облачный сервис',
  photo: 'Чтобы генерировать фото, добавьте в профиле подключение к шлюзу srvllm',
  video: 'Чтобы делать видео, добавьте в профиле подключение к шлюзу srvllm',
};
const ICONS = {
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  photo: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5v5l4.5-2.5z"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};

/** Заглушка раздела до его задачи плана; задача заменит `load` на () => import('./<раздел>.js'). */
function soon(title) {
  return async () => ({
    mount(el) { el.replaceChildren(head(title), notice('wait', 'Раздел скоро появится', 'empty')); },
    unmount() {},
  });
}

const SECTIONS = {
  chat: { title: 'Чат', load: () => import('./chat.js') },
  photo: { title: 'Фото', load: soon('Фото') },
  video: { title: 'Видео', load: soon('Видео') },
  profile: { title: 'Профиль', load: () => import('./profile.js') },
  admin: { title: 'Панель', admin: true, load: () => import('./admin.js') },
};

const view = document.getElementById('view');
const avatar = document.getElementById('avatar');
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
        notice(section, HINTS[section], 'hint', { href: '#/profile', text: 'Открыть профиль' }));
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

function parse(hash) {
  const match = /^#\/([a-z]+)(?:\/(.+))?$/.exec(hash);
  if (!match) return { section: '', id: null };
  try {
    return { section: match[1], id: match[2] ? decodeURIComponent(match[2]) : null };
  } catch {
    return { section: '', id: null };          // битый %-код в адресе — на раздел по умолчанию
  }
}

function allowed({ section, id }) {
  const def = SECTIONS[section];
  return def && !(def.admin && !me.is_admin) && (id === null || section === 'chat');
}

async function render() {
  let route = parse(location.hash);
  if (!allowed(route)) {
    route = { section: 'chat', id: null };
    history.replaceState(null, '', '#/chat');
  }
  for (const tab of document.querySelectorAll('#rail .tab')) {
    if (tab.dataset.section === route.section) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
  const ctx = { me, navigate, id: route.id, applyTheme };
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
  window.addEventListener('hashchange', render);
  await render();
  await worker;
  document.documentElement.dataset.ready = '1';
}

start();
