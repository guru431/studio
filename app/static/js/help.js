import { tr } from './i18n.js';
/* «Справка» (#/help, меню аватара): руководство Studio (GET /api/help/studio — docs/guide.ru.md без вступления) и
 * руководства MCP-серверов учётки (POST /api/help/server — ресурсы `text/markdown` для пользователя, список —
 * GET /api/help). Текст рисует тот же безопасный Markdown, что ответы чата (markdown.js: HTML — текстом).
 * Разделы руководства Studio — кнопки, а не ссылки-якоря: якорь сменил бы раздел страницы. */
import { api } from './api.js';
import { renderMarkdown } from './markdown.js';
import { sectionName } from './sections-meta.js';
import { button, el, words } from './ui.js';

const STUDIO = 'studio';
let controller = null;

export function mount(view) {
  controller = new AbortController();
  const { signal } = controller;
  const source = el('select', { id: 'help-source', class: 'input', 'aria-label': tr("Руководство") },
    el('option', { value: STUDIO }, 'Studio'));
  const chips = el('div', { class: 'help-chips' });
  const toc = el('details', { id: 'help-toc', class: 'help-toc', hidden: '' }, el('summary', {}, tr("Разделы")), chips);
  const status = el('p', { id: 'help-status', class: 'muted small', role: 'status' });
  const text = el('article', { id: 'help-text', class: 'md help-text' });
  view.append(el('header', { class: 'head' }, el('h1', {}, sectionName('help'))),
    el('div', { class: 'page help-page' }, source, toc, status, text));

  let sections = [];
  let shown = 0;               // номер показа: запоздалый ответ прежнего выбора не ляжет

  function paintToc() {
    toc.hidden = source.value !== STUDIO || !sections.length || !text.childNodes.length;
    const headings = [...text.querySelectorAll('h2')];
    chips.replaceChildren(...sections.map((section, i) => {
      const heading = headings[i];
      if (heading) {
        heading.id = `help-${section.id}`;
        heading.tabIndex = -1;
      }
      return button(section.title, { class: 'chip', 'data-help-section': section.id }, () => {
        if (!heading) return;
        heading.scrollIntoView({ block: 'start' });
        heading.focus({ preventScroll: true });
      });
    }));
  }

  async function show() {
    const mine = ++shown;
    const value = source.value;
    status.textContent = tr("Загрузка…");
    text.replaceChildren();
    paintToc();
    try {
      const reply = value === STUDIO ? await api('GET', '/api/help/studio', undefined, { signal })
        : await api('POST', '/api/help/server', JSON.parse(value), { signal });
      if (mine !== shown || signal.aborted) return;
      text.replaceChildren(renderMarkdown(reply.markdown));
      status.textContent = '';
    } catch (err) {
      if (mine !== shown || signal.aborted) return;
      status.textContent = words(err);
    }
    paintToc();
  }

  source.addEventListener('change', show);
  show();
  api('GET', '/api/help', undefined, { signal }).then((found) => {
    if (signal.aborted) return;
    sections = found.studio.sections;
    for (const server of found.servers) for (const resource of server.resources) {
      source.append(el('option', { value: JSON.stringify({ sid: server.sid, uri: resource.uri }) },
        `${server.name} · ${resource.name}`));
    }
    paintToc();
  }, (err) => { if (!signal.aborted) status.textContent = words(err); });
}

export function unmount() {
  controller?.abort();
  controller = null;
}
