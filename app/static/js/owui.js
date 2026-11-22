import { tr } from './i18n.js';
/* «Open WebUI» (#/owui, #/owui/<id> — учётка, #/owui/<id>/<chat> — чат): учётки Open WebUI внутри Studio, сервер
 * ходит в Open WebUI сам (docs/superpowers/specs/2026-11-22-studio-open-webui-design.md, «Страница»).
 *
 * Раскладка как у «Агентов» (code.js): слева панель — учётки сверху, под ними дерево чатов; справа — лента чата и поле
 * ввода. Телефон (≤ 760 px): панель выезжает по ☰ и прячется, как code-panel.js. Здесь только каркас и пустые состояния:
 * учётки, дерево, лента и поле — следующие шаги плана. */
import { api } from './api.js';
import { svg } from './icons.js';
import { sectionName } from './sections-meta.js';
import { el, words } from './ui.js';

let ui = null;
let controller = null;

function panelOpen(open) {
  if (!ui) return;
  ui.root.classList.toggle('panel-open', open);
  ui.backdrop.hidden = !open;
  ui.toggle.setAttribute('aria-expanded', String(open));
}

function onKey(event) {
  if (event.key === 'Escape') panelOpen(false);
}

/** Пустое состояние справа: значок и слова, без учёток — как подключить, с учётками — что выбрать. */
function empty(text) {
  const badge = el('span', { class: 'badge', 'aria-hidden': 'true' });
  badge.innerHTML = svg('owui');   // константа icons.js
  ui.body.replaceChildren(el('div', { id: 'owui-empty', class: 'empty' }, badge, el('p', {}, text)));
}

export function mount(view) {
  controller = new AbortController();
  const { signal } = controller;
  const toggle = el('button', { type: 'button', id: 'owui-panel-toggle', class: 'icon-btn owui-panel-toggle',
    'aria-controls': 'owui-panel', 'aria-expanded': 'false', 'aria-label': tr("Учётки и чаты Open WebUI"),
    title: tr("Учётки и чаты Open WebUI") });
  toggle.innerHTML = svg('list');   // константа icons.js
  const accounts = el('div', { id: 'owui-accounts', class: 'owui-accounts' });
  const tree = el('div', { id: 'owui-tree', class: 'owui-tree' });
  const panel = el('aside', { id: 'owui-panel', class: 'owui-panel', 'aria-label': tr("Учётки и чаты Open WebUI") },
    accounts, tree);
  const status = el('p', { id: 'owui-status', class: 'muted small', role: 'status' });
  const body = el('div', { class: 'owui-body' });
  const backdrop = el('div', { class: 'owui-backdrop', hidden: true });
  const main = el('section', { class: 'owui-main' },
    el('header', { class: 'head' }, toggle, el('h1', {}, sectionName('owui'))), status, body);
  const root = el('div', { class: 'owui-ws' }, panel, main, backdrop);
  toggle.addEventListener('click', () => panelOpen(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panelOpen(false));
  document.addEventListener('keydown', onKey);
  view.append(root);
  ui = { root, toggle, accounts, tree, status, body, backdrop };

  status.textContent = tr("Загрузка…");
  api('GET', '/api/owui/accounts', undefined, { signal }).then((found) => {
    if (signal.aborted) return;
    status.textContent = '';
    empty(found.length ? tr("Выберите чат слева или начните новый")
      : tr("Подключите свой Open WebUI: адрес сервера, email и пароль. Пароль Studio не сохраняет."));
  }, (err) => {
    if (signal.aborted) return;
    status.textContent = words(err);
    empty(tr("Open WebUI сейчас недоступен"));
  });
}

export function unmount() {
  controller?.abort();
  controller = null;
  document.removeEventListener('keydown', onKey);
  ui = null;
}
