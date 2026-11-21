import { tr } from './i18n.js';
/* Подсказки пустого экрана чата (спека 2026-11-21-studio-source-capabilities-design, п. 2): GET /api/chats/suggestions —
 * профиль подключения, затем `prompts` MCP диалога (не больше chat.SUGGESTIONS_MAX). Чипы — под пустой заметкой;
 * нажатие вставляет текст в поле и не отправляет. Подсказка MCP: текст — POST /api/mcp/<sid>/prompt; с аргументами —
 * сначала форма в панели у чипа (menu.js popupPanel). Текст подсказок — пользовательский: не переводится. */
import { api } from './api.js';
import { popupPanel } from './menu.js';
import { el, words } from './ui.js';

/** Вставить текст в поле: пустое — заменить, набранное остаётся, подсказка — после пустой строки. Событие `input` —
 *  черновик, высота и оценка контекста видят вставку. */
function put(field, text) {
  field.value = field.value.trim() ? `${field.value.trimEnd()}\n\n${text}` : text;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  field.focus({ preventScroll: true });
}

/** Чипы подсказок `items` в `host` (ответ GET /api/chats/suggestions); `field` — поле ввода. */
export function paintSuggestions(host, items, field) {
  const note = el('p', { class: 'error small chat-suggest-note', 'aria-live': 'polite' });

  async function fetchText(item, args, chip) {
    chip.disabled = true;
    note.textContent = '';
    try {
      const out = await api('POST', `/api/mcp/${item.prompt.sid}/prompt`, { name: item.prompt.name, arguments: args });
      put(field, out.text);
      return true;
    } catch (err) {
      note.textContent = words(err);
      return false;
    } finally {
      chip.disabled = false;
    }
  }

  function form(item, chip) {
    const panel = el('form', { class: 'suggest-form' });
    const popup = popupPanel(chip, panel, { label: item.title, side: 'below' });
    const inputs = item.prompt.arguments.map((arg) => {
      const input = el('input', { class: 'input', name: arg.name, maxlength: '20000', required: arg.required || null });
      return [arg.name, input, el('label', { class: 'suggest-arg' }, el('span', {}, arg.description || arg.name), input)];
    });
    const go = el('button', { type: 'submit', class: 'btn btn-sm btn-accent suggest-go' }, tr("Вставить"));
    panel.append(el('p', { class: 'suggest-title' }, item.title), ...inputs.map(([, , label]) => label), go);
    panel.addEventListener('submit', async (event) => {
      event.preventDefault();
      const args = Object.fromEntries(inputs.filter(([, input]) => input.value).map(([name, input]) => [name, input.value]));
      go.disabled = true;
      const done = await fetchText(item, args, chip);
      go.disabled = false;
      if (done) popup.close();
    });
    return panel;
  }

  const chips = items.map((item) => {
    const chip = el('button', { type: 'button', class: 'chip suggest-chip', title: item.text || item.title,
      'data-source': item.source }, item.title);
    if (item.prompt && item.prompt.arguments.length) return [chip, form(item, chip)];
    chip.addEventListener('click', () => {
      if (item.prompt) fetchText(item, {}, chip);
      else put(field, item.text);
    });
    return [chip];
  });
  host.replaceChildren(...chips.flat(), note);
  host.hidden = !items.length;
}
