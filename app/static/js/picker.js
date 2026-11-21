import { tr } from './i18n.js';
/* Выбор «как у Агентов»: компактная кнопка с подписью значения и меню вместо системного списка. Настоящий select
 * остаётся под кнопкой (скрыт, 16 px — iOS не увеличивает страницу) и хранит значение и change.
 * Меню — общий компонент menu.js (popupMenu). */
import { el } from './ui.js';
import { popupMenu } from './menu.js';

const EFFORT_NAMES = { minimal: tr("Минимальное"), low: tr("Низкое"), medium: tr("Среднее"), high: tr("Высокое"),
  xhigh: tr("Очень высокое"), max: tr("Максимальное"), ultra: tr("Предельное"), auto: tr("Автоматически"), adaptive: tr("Адаптивное") };

const shown = (select) => select.selectedOptions[0]?.textContent || tr("По умолчанию");

/** Пункты меню одного select: выбор пишет значение и шлёт change, как родной select. */
function optionItems(select, close) {
  return [...select.options].map(option => {
    const namedEffort = select.id === 'code-effort' && EFFORT_NAMES[option.value];
    const title = option.value === '' ? tr("По умолчанию") : namedEffort || option.textContent;
    const detail = option.value === '' ? option.textContent : namedEffort ? option.value : '';
    const item = el('button', { type: 'button', class: 'pick-option', role: 'menuitemradio',
      'aria-checked': String(option.selected), tabindex: '-1', disabled: option.disabled },
    el('span', { class: 'pick-option-text' }, el('span', {}, title),
      ...(detail && detail !== title ? [el('small', {}, detail)] : [])),
    el('span', { class: 'pick-check', 'aria-hidden': 'true' }, '✓'));
    item.addEventListener('click', () => {
      if (select.disabled || option.disabled || !select.isConnected) { close(); return; }
      select.value = option.value;
      close(true);
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    return item;
  });
}

/** picker(host, label, select) → { wrap, trigger, sync(), unmount() }: wrap — обёртка .pick (кнопка и select),
 *  меню — в host; sync() перерисовывает подпись после программной смены значения, опций или disabled. */
export function picker(host, label, select) {
  const value = el('span', { class: 'pick-value', 'aria-hidden': 'true' });
  const trigger = el('button', { type: 'button', class: 'pick-trigger', id: `${select.id}-pick`,
    'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-controls': `${select.id}-menu` }, value);
  const wrap = el('div', { class: 'pick' }, el('span', { class: 'pick-name' }, label), trigger, select);
  select.setAttribute('aria-label', label);
  select.setAttribute('aria-hidden', 'true');
  select.tabIndex = -1;
  const menu = el('div', { id: `${select.id}-menu`, class: 'pick-menu', popover: 'auto',
    role: 'menu', 'aria-label': label });
  host.append(menu);
  const popup = popupMenu(trigger, menu, { side: 'above', blocked: () => select.disabled || wrap.hidden, fill: close => {
    const items = optionItems(select, close);
    // группы select (optgroup) — подзаголовками перед первым своим пунктом
    const nodes = items.flatMap((item, n) => {
      const group = select.options[n].parentElement;
      const first = group.tagName === 'OPTGROUP' && group.querySelector('option') === select.options[n];
      return first ? [el('div', { class: 'pick-heading pick-group', 'aria-hidden': 'true' }, group.label), item] : [item];
    });
    return [[el('div', { class: 'pick-heading', 'aria-hidden': 'true' }, label), ...nodes], items];
  } });
  const sync = () => {
    value.textContent = shown(select);
    trigger.title = `${label}: ${value.textContent}`;
    trigger.setAttribute('aria-label', trigger.title);
    trigger.disabled = select.disabled;
    if (select.disabled || wrap.hidden) popup.close();
  };
  select.addEventListener('change', sync);
  sync();
  return { wrap, trigger, sync, unmount() {
    popup.unmount();
    select.removeEventListener('change', sync);
  } };
}
