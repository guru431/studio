import { tr } from './i18n.js';
/* Поле ввода со значками (спека 2026-10-27-studio-ui-tidy, 4.4): чат, фото, «Изменить», видео, озвучка, поиск.
 *
 * fieldBox(field, { mic, clear, attach, tools, fit }) → { box, error }: box — `div.field-box`, в нём поле и справа значки
 * `div.field-tools`: «очистить» (`.field-clear`), прикрепление `attach`, затем `tools`, микрофон
 * (attachMic). В основных `.composer` значки всегда колонкой; у input поиска — рядом.
 * «Очистить» и `tools` видны, только если в поле есть текст: по событию
 * `input` и при любой записи `field.value` кодом раздела (ответ модели, перевод, пресет, отправка) — иначе
 * значок «очистить» оставался бы у пустого поля. Нажатие «очистить» — пустое поле, событие `input` (раздел
 * пересчитает высоту, счётчик, поиск), фокус в поле; у поля только для чтения (перевод «EN») — ничего.
 * `painted()` раздела зовётся после каждой такой сверки (чат прячет «Разговор», пока в поле текст).
 * `dialog()` — подключение диалога для микрофона (attachMic): поле чата.
 * error — место ошибки микрофона под рамкой: его ставит раздел.
 * fold — первой в `.field-tools` кнопка `.field-fold` «Свернуть поле» / «Развернуть поле» (`aria-pressed`): видна,
 * пока текст выше FOLD_EM строк шрифта поля или поле свёрнуто; нажатие переключает класс `.field-folded` рамки
 * (высота поля — FOLD_EM, app.css); пустое поле состояние сбрасывает. */
import { icon } from './icons.js';
import { attachMic } from './mic.js';
import { el } from './ui.js';

const FOLD_EM = 6;

export function fieldBox(field, { mic = true, clear = true, attach = null, tools = [], fit = false, painted = null,
  dialog = null, fold = false } = {}) {
  const error = el('div', { class: 'field-error' });
  const column = el('div', { class: 'field-tools' });
  const shown = [...tools];
  let folder = null;
  if (fold) {
    folder = el('button', { type: 'button', class: 'icon-btn field-fold', 'aria-pressed': 'false', hidden: true });
    folder.addEventListener('click', () => {
      box.classList.toggle('field-folded');
      paint();
      field.dispatchEvent(new Event('input', { bubbles: true }));   // раздел пересчитает высоту поля
      field.focus();
    });
    column.append(folder);
  }
  if (clear) {
    const button = el('button', { type: 'button', class: 'icon-btn field-clear', 'aria-label': tr("Очистить поле"),
      title: tr("Очистить поле") });
    button.append(icon('x'));
    button.addEventListener('click', () => {
      // поле только для чтения (идёт перевод «EN») — не трогать: пустое поле спрятало бы «EN» с отменой
      if (field.readOnly || field.disabled) return;
      field.value = '';
      field.dispatchEvent(new Event('input', { bubbles: true }));
      field.focus();
    });
    column.append(button);
    shown.unshift(button);
  }
  if (attach) column.append(attach);
  column.append(...tools);
  if (mic) column.append(attachMic(field, error, { dialog }));
  const box = el('div', { class: fold ? 'field-box field-foldable' : 'field-box' }, field, column);
  const paint = () => {
    for (const node of shown) node.hidden = !field.value;
    if (fit && field.tagName === 'TEXTAREA' && field.rows === 1 && field.isConnected) {
      field.style.height = 'auto';
      if (field.value) field.style.height = `${field.scrollHeight}px`;
    }
    if (folder) {
      if (!field.value) box.classList.remove('field-folded');
      const folded = box.classList.contains('field-folded');
      const tall = field.isConnected && field.scrollHeight > FOLD_EM * parseFloat(getComputedStyle(field).fontSize);
      folder.hidden = !field.value || (!folded && !tall);
      folder.setAttribute('aria-pressed', String(folded));
      const label = folded ? tr("Развернуть поле") : tr("Свернуть поле");
      folder.setAttribute('aria-label', label);
      folder.title = label;
      folder.replaceChildren(icon(folded ? 'expand' : 'collapse'));
    }
    painted?.();
  };
  // запись value кодом раздела события input не шлёт — значки следят и за ней
  const own = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value');
  Object.defineProperty(field, 'value', { configurable: true, get() { return own.get.call(this); },
    set(next) { own.set.call(this, next); paint(); } });
  field.addEventListener('input', paint);
  paint();
  box.addEventListener('click', (event) => { if (event.target === box) field.focus(); });   // пустое место рамки
  return { box, error };
}

/** Вставка исходного фото в открытом разделе работает и без фокуса в промпте. */
export function imagePaste(input, attach) {
  function paste(event) {
    const data = event.clipboardData;
    if (!data || event.defaultPrevented || !input.isConnected || input.closest('[hidden], [inert]')
      || document.querySelector('dialog[open]')) return;
    const image = [...data.files].find(file => file.type.startsWith('image/'))
      || [...data.items].find(item => item.kind === 'file' && item.type.startsWith('image/'))?.getAsFile();
    if (!image) return;
    event.preventDefault();
    attach(image);
  }
  document.addEventListener('paste', paste);
  return () => document.removeEventListener('paste', paste);
}

/** Сенсорный экран без мыши: на экранной клавиатуре нет Shift+Enter. */
const TOUCH = '(pointer: coarse) and (hover: none)';

/** Enter основного многострочного поля (Чат, Агенты, Фото, Видео): на сенсорном экране — перенос строки, отправка —
 * кнопкой; иначе Enter — `send()`, Shift+Enter — перенос. `enterkeyhint` («Ввод» или «Отправить» на клавише)
 * сверяется при фокусе: планшет мог получить мышь. Однострочные поля (поиск) свой Enter оставляют. */
export function enterSends(field, send) {
  const paint = () => field.setAttribute('enterkeyhint', matchMedia(TOUCH).matches ? 'enter' : 'send');
  paint();
  field.addEventListener('focus', paint);
  field.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || matchMedia(TOUCH).matches) return;
    event.preventDefault();
    send();
  });
}
