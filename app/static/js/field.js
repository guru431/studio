/* Поле ввода со значками (спека 2026-10-27-studio-ui-tidy, 4.4): чат, фото, «Изменить», видео, озвучка, поиск.
 *
 * fieldBox(field, { mic, clear, tools }) → { box, error }: box — `div.field-box`, в нём поле и справа колонка
 * `div.field-tools`: сверху «очистить» (`.field-clear`), затем `tools` (у фото — «EN»), снизу микрофон
 * (attachMic). «Очистить» и `tools` видны, только если в поле есть текст: по событию `input` и при любой
 * записи `field.value` кодом раздела (ответ модели, перевод, пресет, отправка) — иначе значок «очистить»
 * оставался бы у пустого поля. Нажатие «очистить» — пустое поле, событие `input` (раздел пересчитает высоту,
 * счётчик, поиск), фокус в поле. error — место ошибки микрофона под рамкой: его ставит раздел. */
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
  const box = el('div', { class: 'field-box' }, field, column);
  box.addEventListener('click', (event) => { if (event.target === box) field.focus(); });   // пустое место рамки
  return { box, error };
}
