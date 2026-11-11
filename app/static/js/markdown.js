/* Текст модели → безопасный DOM: marked (разметка) → DOMPurify (очистка) → highlight.js (код).
 *
 * Библиотеки — обычные скрипты из /static/vendor (глобальные marked, DOMPurify, hljs), их
 * загружаются при первом импорте этого модуля. У каждого блока кода — кнопка «Копировать»; ссылки
 * открываются в новой вкладке без доступа к этой (rel=noopener noreferrer).
 *
 * HTML в ответе модели — текст, а не разметка: иначе ответ (или подсказка в приложенном PDF)
 * вставил бы в страницу <style>, форму входа на чужую учётку или кнопку на весь экран. marked
 * экранирует такие куски сам (кроме голого <br> — перенос строки в ячейке таблицы); DOMPurify пропускает только теги разметки, без style, id и форм;
 * class остаётся только language-* у кода. Флажки списков задач — знаками ☑/☐, без <input>.
 * Внешние <img> (markdown-картинка с чужим адресом) блокирует CSP страниц (`img-src`, main.CSP), поэтому
 * DOMPurify их не вычищает.
 *
 * renderMarkdown(text, { html: true }) — текст распознавания («Сканы», ocr.js): HunyuanOCR пишет
 * таблицы HTML-тегами, и они должны стать таблицей. HTML отдаётся DOMPurify как есть — остаются те же
 * теги и атрибуты (плюс colspan и rowspan ячеек), скрипты, стили и формы — нет. */

import { localFileHref } from './code-file-links.js';
import { visualizationExtension, visualizationReference } from './visualization-markup.js';

await Promise.all([['marked', 'marked.min.js'], ['DOMPurify', 'purify.min.js'], ['hljs', 'highlight.min.js']]
  .map(([name, file]) => {
    if (window[name]) return;
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = new URL('../vendor/' + file, import.meta.url).href;
      script.crossOrigin = 'anonymous';
      const timer = setTimeout(() => { script.remove(); reject(new Error('Markdown library timeout')); }, 30000);
      script.onload = () => {
        clearTimeout(timer);
        if (window[name]) resolve();
        else reject(new Error('Markdown library unavailable'));
      };
      script.onerror = () => { clearTimeout(timer); reject(new Error('Markdown library unavailable')); };
      document.head.append(script);
    });
  }));

const COPIED_MS = 1500;
const TAGS = ['a', 'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 'code', 'pre',
  'blockquote', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'img'];
const ATTRS = ['href', 'title', 'alt', 'src', 'class', 'start', 'align'];
const LANG = /^language-[\w+#-]+$/;
const BR = /^<br\s*\/?>$/i;     // единственный тег из ответа модели, что остаётся тегом

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

const parsers = {};

function markdownParser(html) {
  if (parsers[html]) return parsers[html];
  parsers[html] ||= new window.marked.Marked({
    gfm: true,
    async: false,
    renderer: {
      html({ text, block }) {
        if (html) return text;                         // дальше — DOMPurify
        if (!block && BR.test(text)) return '<br>';   // модели так переносят строку в ячейке таблицы
        return block ? `<p>${escapeHtml(text)}</p>` : escapeHtml(text);
      },
      checkbox({ checked }) {
        return checked ? '☑ ' : '☐ ';
      },
      tablecell(token) {
        const tag = token.header ? 'th' : 'td';
        const align = ['left', 'center', 'right'].includes(token.align) ? ` align="${token.align}"` : '';
        return `<${tag}${align}>${this.parser.parseInline(token.tokens)}</${tag}>\n`;
      },
      link(token) {
        const href = localFileHref(token.href);
        // Prefix a drive letter before sanitizing: DOMPurify correctly rejects a raw C: URI.
        if (!href) return false;
        return `<a href="${escapeHtml(href)}">${this.parser.parseInline(token.tokens)}</a>`;
      },
      image(token) {
        const href = localFileHref(token.href);
        if (!href) return false;
        return `<a href="${escapeHtml(href)}">${escapeHtml(token.text || 'Открыть изображение')}</a>`;
      },
    },
  });
  parsers[html].use({ extensions: [visualizationExtension] });
  return parsers[html];
}

async function copy(pre, button) {
  const code = pre.querySelector('code') || pre;
  try {
    await navigator.clipboard.writeText(code.textContent);
    button.textContent = 'Скопировано';
  } catch {
    button.textContent = 'Не вышло';
  }
  setTimeout(() => { button.textContent = 'Копировать'; }, COPIED_MS);
}

function fragmentOf(out, { html = false, highlight = true, fileLinks = null } = {}) {
  const fragment = window.DOMPurify.sanitize(out, { RETURN_DOM_FRAGMENT: true, ALLOWED_TAGS: TAGS,
    ALLOWED_ATTR: html ? [...ATTRS, 'colspan', 'rowspan'] : ATTRS, ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false });
  for (const node of fragment.querySelectorAll('[class]')) {
    if (node.tagName !== 'CODE' || !LANG.test(node.className)) node.removeAttribute('class');
  }
  for (const code of fragment.querySelectorAll('pre code')) {
    const lang = /^language-([\w+#-]+)$/.exec(code.className);
    if (highlight && code.textContent.length <= (lang ? 10000 : 2000)
        && (!lang || window.hljs.getLanguage(lang[1]))) window.hljs.highlightElement(code);
    else code.classList.add('hljs');                  // язык не из сборки — без подсветки
  }
  for (const pre of fragment.querySelectorAll('pre')) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'copy';
    button.textContent = 'Копировать';
    button.addEventListener('click', () => copy(pre, button));
    pre.append(button);
  }
  for (const link of fragment.querySelectorAll('a[href]')) {
    const href = link.getAttribute('href');
    if (href === '#studio-visualization') {
      let reference;
      try { reference = visualizationReference(JSON.parse(link.getAttribute('title'))); } catch { /* literal link */ }
      if (reference) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn code-visualization-open';
        button.textContent = 'Открыть визуализацию';
        button.title = reference.title;
        button.disabled = !fileLinks?.visualization;
        button.addEventListener('click', () => fileLinks.visualization(reference));
        link.replaceWith(button);
        continue;
      }
    }
    const path = fileLinks?.resolve(href);
    if (path) {
      link.setAttribute('href', '#');
      link.classList.add('code-message-file');
      link.addEventListener('click', (event) => { event.preventDefault(); fileLinks.open(path); });
      continue;
    }
    if (localFileHref(href)) {
      link.removeAttribute('href');
      link.title = 'Файл вне текущего проекта или недоступен в этом разделе';
      continue;
    }
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  }
  return fragment;
}

export function renderMarkdown(text, options = {}) {
  return fragmentOf(markdownParser(!!options.html).parse(String(text ?? '')), options);
}

const renderQueue = [];
const renderVersions = new WeakMap();
let renderFrame = null;
let parserWorker = null;
let parseId = 0;
const parses = new Map();

function stopParser() {
  parserWorker?.terminate();
  parserWorker = null;
  for (const { done, timer } of parses.values()) { clearTimeout(timer); done(null); }
  parses.clear();
}
document.addEventListener('studio-code-clear', stopParser);

function tokenize(text) {
  try {
    if (!parserWorker) {
      parserWorker = new Worker(new URL('./markdown-worker.js', import.meta.url), { type: 'module' });
      parserWorker.onmessage = ({ data }) => {
        const pending = parses.get(data.id);
        if (pending) { clearTimeout(pending.timer); pending.done(data.tokens); }
        parses.delete(data.id);
      };
      parserWorker.onerror = (event) => { event.preventDefault(); stopParser(); };
    }
    return new Promise((done) => {
      const id = ++parseId;
      parses.set(id, { done, timer: setTimeout(stopParser, 10000) });
      parserWorker.postMessage({ id, text });
    });
  } catch {
    stopParser();
    return Promise.resolve(null);
  }
}

function drain() {
  renderFrame = null;
  const deadline = performance.now() + 6;
  while (renderQueue.length && performance.now() < deadline) {
    const job = renderQueue.shift();
    if (!job.valid() || renderVersions.get(job.host) !== job) { job.done(false); continue; }
    try {
      if (job.nodes) {
        const fragment = document.createDocumentFragment();
        const end = Math.min(job.index + 2, job.nodes.length);
        while (job.index < end) fragment.append(job.build(job.nodes[job.index++]));
        job.update(() => job.host.append(fragment));
        if (job.index < job.nodes.length) renderQueue.push(job);
        else job.done(true);
        continue;
      }
      job.tokens ||= markdownParser(false).lexer(job.text);
      const part = [];
      let size = 0;
      while (job.index < job.tokens.length && part.length < 8 && size < 1500) {
        const token = job.tokens[job.index++];
        part.push(token);
        size += (token.raw || token.text || '').length;
      }
      part.links = job.tokens.links;
      const fragment = fragmentOf(markdownParser(false).parser(part), { fileLinks: job.fileLinks });
      if (job.text.length > 8000) {
        if (!job.group || job.groupSize >= 4000) {
          job.group = document.createElement('div');
          job.group.className = 'md-part';
          job.groupSize = 0;
        }
        job.groupSize += size;
        job.update(() => {
          if (job.group.parentNode !== job.host) job.host.append(job.group);
          job.group.append(fragment);
        });
      } else job.update(() => job.host.append(fragment));
      if (job.index < job.tokens.length) renderQueue.push(job);
      else job.done(true);
    } catch {
      // Даже при отказе парсера текст остаётся текстом, никогда не HTML.
      job.host.textContent = job.text;
      job.done(false);
    }
  }
  if (renderQueue.length) renderFrame = requestAnimationFrame(drain);
}

/** Общий бюджет для всех сообщений: несколько историй не создают отдельные тяжёлые RAF. */
export async function renderMarkdownInto(host, text, { valid = () => true, update = (change) => change(), fileLinks = null } = {}) {
  host.replaceChildren();
  let done;
  const finished = new Promise((resolve) => { done = resolve; });
  const job = { host, text: String(text ?? ''), valid, update, done, fileLinks, tokens: null, index: 0 };
  renderVersions.set(host, job);
  if (job.text.length > 8000) {
    job.tokens = await tokenize(job.text);
    if (!valid() || renderVersions.get(host) !== job) return false;
    if (!job.tokens) { update(() => { host.textContent = job.text; }); return false; }
  }
  renderQueue.push(job);
  if (renderFrame === null) renderFrame = requestAnimationFrame(drain);
  return finished;
}

/** Построение больших списков шагов использует тот же бюджет кадра, что Markdown. */
export function renderNodesInto(host, nodes, build, { valid = () => true, update = (change) => change() } = {}) {
  host.replaceChildren();
  return new Promise((done) => {
    const job = { host, nodes, build, valid, update, done, index: 0, text: '' };
    renderVersions.set(host, job);
    renderQueue.push(job);
    if (renderFrame === null) renderFrame = requestAnimationFrame(drain);
  });
}
