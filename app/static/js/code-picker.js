/* Compact session choices; the select remains the source of values and change events. */
import { el } from './ui.js';

const EFFORT_NAMES = { minimal: 'Минимальное', low: 'Низкое', medium: 'Среднее', high: 'Высокое',
  xhigh: 'Очень высокое', max: 'Максимальное', ultra: 'Предельное', auto: 'Автоматически', adaptive: 'Адаптивное' };

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
  let items = [];
  const close = (focus = false) => {
    if (menu.matches(':popover-open')) menu.hidePopover();
    trigger.setAttribute('aria-expanded', 'false');
    if (focus && trigger.isConnected && !trigger.disabled) trigger.focus({ preventScroll: true });
  };
  const focusItem = item => {
    if (!item) return;
    item.focus({ preventScroll: true });
    if (item.offsetTop < menu.scrollTop) menu.scrollTop = item.offsetTop;
    else if (item.offsetTop + item.offsetHeight > menu.scrollTop + menu.clientHeight) {
      menu.scrollTop = item.offsetTop + item.offsetHeight - menu.clientHeight;
    }
  };
  const update = () => {
    value.textContent = select.selectedOptions[0]?.textContent || 'По умолчанию';
    trigger.title = `${label}: ${value.textContent}`;
    trigger.setAttribute('aria-label', trigger.title);
    trigger.disabled = select.disabled;
    if (select.disabled || root.hidden) close();
  };
  const open = (last = false) => {
    if (select.disabled || root.hidden || menu.matches(':popover-open')) return;
    items = [...select.options].map(option => {
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
    menu.replaceChildren(el('div', { class: 'code-pick-heading', 'aria-hidden': 'true' }, label), ...items);
    const zoom = Number(getComputedStyle(document.documentElement).zoom) || 1;
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
    menu.style.maxWidth = `${(width - 16) / zoom}px`;
    menu.style.maxHeight = `${(height - 16) / zoom}px`;
    menu.style.left = `${(left + 8) / zoom}px`;
    menu.style.top = `${(top + 8) / zoom}px`;
    menu.showPopover();
    const box = trigger.getBoundingClientRect(), size = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(left + 8, Math.min(box.left, left + width - size.width - 8)) / zoom}px`;
    menu.style.top = `${Math.max(top + 8, box.top - size.height - 6 >= top + 8
      ? box.top - size.height - 6 : Math.min(box.bottom + 6, top + height - size.height - 8)) / zoom}px`;
    trigger.setAttribute('aria-expanded', 'true');
    const available = items.filter(item => !item.disabled);
    focusItem(last ? available.at(-1) : available.find(item => item.getAttribute('aria-checked') === 'true') || available[0]);
  };
  trigger.addEventListener('click', () => menu.matches(':popover-open') ? close() : open());
  trigger.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); open(event.key === 'ArrowUp');
    }
  });
  menu.addEventListener('keydown', event => {
    const available = items.filter(item => !item.disabled), current = available.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (current + 1) % available.length;
    else if (event.key === 'ArrowUp') next = (current + available.length - 1) % available.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = available.length - 1;
    else if (event.key === 'Escape' || event.key === 'Tab') close(true);
    else return;
    if (event.key !== 'Tab') event.preventDefault();
    if (next !== undefined) focusItem(available[next]);
  });
  menu.addEventListener('toggle', () => trigger.setAttribute('aria-expanded', String(menu.matches(':popover-open'))));
  const scrolled = event => { if (!menu.contains(event.target)) close(); };
  const hidden = () => { if (document.hidden) close(); };
  const resized = () => close();
  select.addEventListener('change', update);
  window.addEventListener('resize', resized);
  window.addEventListener('scroll', scrolled, true);
  window.visualViewport?.addEventListener('resize', resized);
  document.addEventListener('visibilitychange', hidden);
  update();
  return { root, update, unmount() {
    close(); menu.remove();
    select.removeEventListener('change', update);
    window.removeEventListener('resize', resized);
    window.removeEventListener('scroll', scrolled, true);
    window.visualViewport?.removeEventListener('resize', resized);
    document.removeEventListener('visibilitychange', hidden);
  } };
}
