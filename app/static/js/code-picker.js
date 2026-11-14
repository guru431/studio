import { tr } from './i18n.js';
/* Отдельный выбор модели, усилия, прав и доставки; select хранит значение и change.
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
    const item = el('button', { type: 'button', class: 'code-pick-option', role: 'menuitemradio',
      'aria-checked': String(option.selected), tabindex: '-1', disabled: option.disabled },
    el('span', { class: 'code-pick-option-text' }, el('span', {}, title),
      ...(detail && detail !== title ? [el('small', {}, detail)] : [])),
    el('span', { class: 'code-pick-check', 'aria-hidden': 'true' }, '✓'));
    item.addEventListener('click', () => {
      if (select.disabled || option.disabled || !select.isConnected) { close(); return; }
      select.value = option.value;
      close(true);
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    return item;
  });
}

export function codePicker(host, label, select) {
  const value = el('span', { class: 'code-pick-value', 'aria-hidden': 'true' });
  const trigger = el('button', { type: 'button', class: 'code-pick-trigger', id: `${select.id}-pick`,
    'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-controls': `${select.id}-menu` }, value);
  const root = el('div', { class: 'code-pick' }, el('span', { class: 'code-pick-name' }, label), trigger, select);
  select.setAttribute('aria-label', label);
  select.setAttribute('aria-hidden', 'true');
  select.tabIndex = -1;
  const menu = el('div', { id: `${select.id}-menu`, class: 'code-pick-menu', popover: 'auto',
    role: 'menu', 'aria-label': label });
  host.append(menu);
  const popup = popupMenu(trigger, menu, { side: 'above', blocked: () => select.disabled || root.hidden, fill: close => {
    const items = optionItems(select, close);
    return [[el('div', { class: 'code-pick-heading', 'aria-hidden': 'true' }, label), ...items], items];
  } });
  const update = () => {
    value.textContent = shown(select);
    trigger.title = `${label}: ${value.textContent}`;
    trigger.setAttribute('aria-label', trigger.title);
    trigger.disabled = select.disabled;
    if (select.disabled || root.hidden) popup.close();
  };
  select.addEventListener('change', update);
  update();
  return { root, update, unmount() {
    popup.unmount();
    select.removeEventListener('change', update);
  } };
}
