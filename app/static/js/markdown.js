/* Текст модели → безопасный DOM: marked (разметка) → DOMPurify (очистка) → highlight.js (код).
 *
 * Библиотеки — обычные скрипты из /static/vendor (глобальные marked, DOMPurify, hljs), их
 * подключает index.html до модулей. У каждого блока кода — кнопка «Копировать»; ссылки
 * открываются в новой вкладке без доступа к этой (rel=noopener). */

const COPIED_MS = 1500;

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
  const html = window.marked.parse(String(text ?? ''), { gfm: true, async: false });
  const fragment = window.DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true });
  for (const code of fragment.querySelectorAll('pre code')) {
    const lang = /(?:^|\s)language-([\w+#-]+)/.exec(code.className);
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
    link.rel = 'noopener';
  }
  return fragment;
}
