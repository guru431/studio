import { tr } from './i18n.js';
import { api } from './api.js';
import { icon } from './icons.js';
import { renderMarkdown } from './markdown.js';
import { sectionName } from './sections-meta.js';
import { el, words } from './ui.js';

const START = 'start';
const href = (id) => id === START ? '#/help' : `#/help/${id}`;
let screen = null;

export function mount(view, ctx) {
  const controller = new AbortController();
  const { signal } = controller;
  const groups = { start: '', work: tr("Диалоги и проекты"), media: tr("Медиа и документы"),
    history: tr("Файлы и история"), account: tr("Учётка и безопасность") };
  const icons = { start: 'info', chat: 'chat', internet: 'globe', owui: 'owui', agents: 'code',
    visual: 'photo', audio: 'speech', documents: 'doc', files: 'folder', account: 'info', security: 'pick', admin: 'list' };
  const quick = ['chat', 'agents', 'files', 'security'];
  const source = el('select', { id: 'help-source', class: 'input' },
    el('option', { value: '' }, tr("Выберите руководство")));
  const sources = el('label', { class: 'help-sources', hidden: true },
    el('span', { class: 'help-group-label' }, tr("Руководства MCP")), source);
  const toc = el('nav', { id: 'help-toc', 'aria-label': tr("Разделы справки") });
  const menu = el('div', { id: 'help-menu', class: 'help-menu' }, toc, sources);
  const toggle = el('button', { type: 'button', class: 'btn help-toggle',
    'aria-controls': menu.id, 'aria-expanded': 'false' }, icon('list'), tr("Разделы справки"), icon('down'));
  const sidebar = el('aside', { class: 'help-sidebar' }, toggle, menu);
  const status = el('p', { id: 'help-status', class: 'muted small', role: 'status' }, tr("Загрузка…"));
  const article = el('article', { id: 'help-text', class: 'help-text' }, status);
  view.append(el('header', { class: 'head' }, el('h1', {}, sectionName('help'))),
    el('div', { class: 'help-page' }, sidebar, article));

  let topics = [];
  let requested = ctx.id || START;
  let generation = 0;

  function setMenu(open) {
    toggle.setAttribute('aria-expanded', String(open));
  }
  toggle.addEventListener('click', () => setMenu(toggle.getAttribute('aria-expanded') !== 'true'));

  function focusArticle(heading) {
    requestAnimationFrame(() => {
      if (!heading.isConnected || view.inert) return;
      view.scrollTop = 0;
      heading.focus({ preventScroll: true });
    });
  }

  function topicLink(topic, card = false) {
    const link = el('a', { class: card ? 'help-card' : 'help-topic', href: href(topic.id), 'data-help-section': topic.id },
      icon(icons[topic.id] || 'doc'), el('span', {}, topic.title), card ? icon('next') : null);
    link.addEventListener('click', (event) => {
      if (event.button || event.altKey || event.ctrlKey || event.shiftKey || event.metaKey) return;
      setMenu(false);
      if (link.hash === location.hash) showTopic(topic.id, true);
    });
    return link;
  }

  function renderMenu() {
    for (const [group, label] of Object.entries(groups)) {
      const links = topics.filter((topic) => topic.group === group).map((topic) => topicLink(topic));
      if (links.length) toc.append(el('div', { class: 'help-group' },
        label ? el('p', { class: 'help-group-label' }, label) : null, ...links));
    }
  }

  function showTopic(id, focus = false) {
    requested = id || START;
    if (!topics.length) return;
    generation += 1;
    const topic = topics.find((item) => item.id === requested) || topics[0];
    const title = el('h2', { id: `help-${topic.id}`, tabindex: '-1' }, topic.title);
    for (const link of toc.querySelectorAll('a')) {
      if (link.dataset.helpSection === topic.id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    source.value = '';
    const cards = topic.id === START ? el('div', { class: 'help-cards' },
      ...quick.map((key) => topics.find((item) => item.id === key)).filter(Boolean).map((item) => topicLink(item, true))) : null;
    article.replaceChildren(title, el('div', { class: 'md help-prose' }, renderMarkdown(topic.markdown)), cards);
    if (focus) focusArticle(title);
  }

  source.addEventListener('change', async () => {
    if (!source.value) { showTopic(requested, true); return; }
    const mine = ++generation;
    const title = el('h2', { tabindex: '-1' }, source.selectedOptions[0].textContent);
    for (const link of toc.querySelectorAll('[aria-current]')) link.removeAttribute('aria-current');
    setMenu(false);
    status.textContent = tr("Загрузка…");
    article.replaceChildren(title, status);
    focusArticle(title);
    try {
      const reply = await api('POST', '/api/help/server', JSON.parse(source.value), { signal });
      if (mine !== generation || signal.aborted) return;
      status.replaceWith(el('div', { class: 'md help-prose' }, renderMarkdown(reply.markdown)));
    } catch (err) {
      if (mine === generation && !signal.aborted) status.textContent = words(err);
    }
  });

  api('GET', '/api/help/topics', undefined, { signal }).then((found) => {
    if (signal.aborted) return;
    topics = found.topics.filter((topic) => topic.id !== 'admin' || ctx.me.is_admin);
    if (!topics.length) { status.textContent = tr("Руководство не найдено"); return; }
    renderMenu();
    showTopic(requested);
  }, (err) => { if (!signal.aborted) status.textContent = words(err); });

  api('GET', '/api/help', undefined, { signal }).then((found) => {
    if (signal.aborted) return;
    for (const server of found.servers) {
      source.append(el('optgroup', { label: server.name }, ...server.resources.map((resource) =>
        el('option', { value: JSON.stringify({ sid: server.sid, uri: resource.uri }) }, resource.name))));
    }
    sources.hidden = source.options.length < 2;
  }, () => {});   // статьи Студии доступны во время сбоя MCP

  screen = { controller, showTopic };
}

export function update(ctx) {
  screen?.showTopic(ctx.id, true);
}

export function unmount() {
  screen?.controller.abort();
  screen = null;
}
