/* Поле ввода со значками (спека 2026-10-27-studio-ui-tidy, 4.4): чат, фото, «Изменить», видео, озвучка, поиск.
 *
 * fieldBox(field, { mic, clear, tools }) → { box, error }: box — `div.field-box`, в нём поле и справа значки
 * `div.field-tools`: «очистить» (`.field-clear`), затем `tools` (у фото — «EN»), последним микрофон
 * (attachMic). У многострочного поля (озвучка, «Изменить») значки — колонкой сверху вниз; у однострочного
 * (rows=1: чат, фото, видео; класс `one-line`) и у input поиска — рядом, прижаты к низу, чтобы высота рамки
 * не менялась с появлением «очистить». «Очистить» и `tools` видны, только если в поле есть текст: по событию
 * `input` и при любой записи `field.value` кодом раздела (ответ модели, перевод, пресет, отправка) — иначе
 * значок «очистить» оставался бы у пустого поля. Нажатие «очистить» — пустое поле, событие `input` (раздел
 * пересчитает высоту, счётчик, поиск), фокус в поле; у поля только для чтения (перевод «EN») — ничего.
 * error — место ошибки микрофона под рамкой: его ставит раздел. */
import { attachMic } from './mic.js';
import { el } from './ui.js';

const CLEAR = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

export function fieldBox(field, { mic = true, clear = true, tools = [] } = {}) {
  const error = el('div', { class: 'field-error' });
  const column = el('div', { class: 'field-tools' });
  const shown = [...tools];
  if (clear) {
    const button = el('button', { type: 'button', class: 'icon-btn field-clear', 'aria-label': 'Очистить поле',
      title: 'Очистить поле' });
    button.innerHTML = CLEAR;                                       // константа выше
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
  column.append(...tools);
  if (mic) column.append(attachMic(field, error));
  const paint = () => {
    for (const node of shown) node.hidden = !field.value;
  };
  // запись value кодом раздела события input не шлёт — значки следят и за ней
  const own = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value');
  Object.defineProperty(field, 'value', { configurable: true, get() { return own.get.call(this); },
    set(next) { own.set.call(this, next); paint(); } });
  field.addEventListener('input', paint);
  paint();
  // однострочное поле (чат, фото, видео) — значки в ряд, иначе колонка выше поля и рамка прыгала бы на первой букве
  const box = el('div', { class: field.rows === 1 ? 'field-box one-line' : 'field-box' }, field, column);
  box.addEventListener('click', (event) => { if (event.target === box) field.focus(); });   // пустое место рамки
  return { box, error };
}
