/* Compact session choices; the select remains the source of values and change events.
 * codePicker — one choice (model, effort, access, delivery); codeChoices — on a narrow screen one button
 * «model · effort · access» with a menu of three groups over the same selects (CSS shows one or the other).
 * Меню — общий компонент menu.js (popupMenu). */
import { el } from './ui.js';
import { popupMenu } from './menu.js';

const EFFORT_NAMES = { minimal: 'Минимальное', low: 'Низкое', medium: 'Среднее', high: 'Высокое',
  xhigh: 'Очень высокое', max: 'Максимальное', ultra: 'Предельное', auto: 'Автоматически', adaptive: 'Адаптивное' };

const shown = (select) => select.selectedOptions[0]?.textContent || 'По умолчанию';

/** Пункты меню одного select: выбор пишет значение и шлёт change, как родной select. */
function optionItems(select, close) {
  return [...select.options].map(option => {
    const namedEffort = select.id === 'code-effort' && EFFORT_NAMES[option.value];
    const title = option.value === '' ? 'По умолчанию' : namedEffort || option.textContent;
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

/** groups — [[название, select], …]: кнопка показывает их значения через « · », меню — группы с теми же пунктами. */
export function codeChoices(host, label, groups) {
  const value = el('span', { class: 'code-pick-value', 'aria-hidden': 'true' });
  const trigger = el('button', { type: 'button', class: 'code-pick-trigger', id: 'code-settings-pick',
    'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-controls': 'code-settings-menu' }, value);
  const root = el('div', { class: 'code-pick code-settings-pick' }, trigger);
  const menu = el('div', { id: 'code-settings-menu', class: 'code-pick-menu', popover: 'auto',
    role: 'menu', 'aria-label': label });
  host.append(menu);
  const disabled = () => groups.every(([, select]) => select.disabled);
  const popup = popupMenu(trigger, menu, { side: 'above', blocked: () => disabled() || root.hidden, fill: close => {
    const all = [];
    const nodes = groups.map(([name, select]) => {
      const id = `code-settings-${select.id}`;
      const items = optionItems(select, close);
      if (select.disabled) for (const item of items) item.disabled = true;
      all.push(...items);
      return el('div', { role: 'group', class: 'code-settings-group', 'aria-labelledby': id },
        el('div', { class: 'code-pick-heading', id }, name), ...items);
    });
    return [nodes, all];
  } });
  const update = () => {
    const values = groups.map(([, select]) => shown(select));
    value.textContent = new Set(values).size === 1 ? values[0] : values.join(' · ');    // «Загрузка…» — один раз
    trigger.title = `${label}: ${groups.map(([name, select]) => `${name} — ${shown(select)}`).join(', ')}`;
    trigger.setAttribute('aria-label', trigger.title);
    trigger.disabled = disabled();
    if (trigger.disabled || root.hidden) popup.close();
  };
  for (const [, select] of groups) select.addEventListener('change', update);
  update();
  return { root, update, unmount() {
    popup.unmount();
    for (const [, select] of groups) select.removeEventListener('change', update);
  } };
}
