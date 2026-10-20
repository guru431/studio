/* Текст модели → безопасный DOM: marked (разметка) → DOMPurify (очистка) → highlight.js (код).
 *
 * Библиотеки — обычные скрипты из /static/vendor (глобальные marked, DOMPurify, hljs), их
 * подключает index.html до модулей. У каждого блока кода — кнопка «Копировать»; ссылки
 * открываются в новой вкладке без доступа к этой (rel=noopener noreferrer).
 *
 * HTML в ответе модели — текст, а не разметка: иначе ответ (или подсказка в приложенном PDF)
 * вставил бы в страницу <style>, форму входа на чужую учётку или кнопку на весь экран. marked
 * экранирует такие куски сам; DOMPurify пропускает только теги разметки, без style, id и форм;
 * class остаётся только language-* у кода. Флажки списков задач — знаками ☑/☐, без <input>. */

const COPIED_MS = 1500;
const TAGS = ['a', 'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 'code', 'pre',
  'blockquote', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'img'];
const ATTRS = ['href', 'title', 'alt', 'src', 'class', 'start', 'align'];
const LANG = /^language-[\w+#-]+$/;

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

let parser = null;

function markdownParser() {
  parser ||= new window.marked.Marked({
    gfm: true,
    async: false,
    renderer: {
      html({ text, block }) {
        return block ? `<p>${escapeHtml(text)}</p>` : escapeHtml(text);
      },
      checkbox({ checked }) {
        return checked ? '☑ ' : '☐ ';
      },
    },
  });
  return parser;
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

export function renderMarkdown(text) {
  const html = markdownParser().parse(String(text ?? ''));
  const fragment = window.DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true, ALLOWED_TAGS: TAGS,
    ALLOWED_ATTR: ATTRS, ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false });
  for (const node of fragment.querySelectorAll('[class]')) {
    if (node.tagName !== 'CODE' || !LANG.test(node.className)) node.removeAttribute('class');
  }
  for (const code of fragment.querySelectorAll('pre code')) {
    const lang = /^language-([\w+#-]+)$/.exec(code.className);
    if (!lang || window.hljs.getLanguage(lang[1])) window.hljs.highlightElement(code);
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
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
  }
  return fragment;
}
