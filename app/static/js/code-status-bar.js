/* Статусу — 40% строки; кнопки сохраняют размер и по одной уходят в «⋯». */
import { el } from './ui.js';
import { icon } from './icons.js';
import { popupPanel } from './menu.js';

export function statusActions(host, bar, activity, actions, fixed) {
  const more = el('button', { type: 'button', id: 'code-status-more', class: 'icon-btn',
    'aria-label': 'Другие действия чата', title: 'Другие действия чата', hidden: true }, icon('more'));
  const overflow = el('div', { class: 'code-chat-actions' });
  const panel = el('div', { id: 'code-status-menu', class: 'code-status-menu' }, overflow);
  const controls = el('div', { class: 'code-status-controls' }, actions, more, fixed);
  const items = [...actions.children].filter(node => node.tagName === 'BUTTON').map(button => {
    const label = el('span', { class: 'code-status-action-label', hidden: true });
    button.append(label);
    const controlled = button.getAttribute('aria-controls');
    const menu = controlled ? actions.querySelector('#' + CSS.escape(controlled)) || document.getElementById(controlled) : null;
    return { button, label, menu: menu?.hasAttribute('popover') ? menu : null };
  });
  bar.replaceChildren(activity, controls);
  host.append(panel);
  const popup = popupPanel(more, panel, { label: 'Действия чата', side: 'above', align: 'end' });
  let frame = null;

  // Отдельное измерение обычного ряда не меняет фокус и состояние открытых popover.
  function measure(buttons) {
    const row = el('div', { class: 'code-status-controls code-status-measure', 'aria-hidden': 'true', inert: '' });
    const zoom = Number(getComputedStyle(document.documentElement).zoom) || 1;
    const clones = buttons.map(button => {
      const copy = button.cloneNode(true);
      for (const node of [copy, ...copy.querySelectorAll('[id]')]) node.removeAttribute('id');
      copy.hidden = false;
      for (const key of ['width', 'height', 'min-width', 'font-size']) copy.style.removeProperty(key);
      copy.querySelector('.code-status-action-label')?.setAttribute('hidden', '');
      return copy;
    });
    row.append(...clones);
    bar.append(row);
    const sizes = clones.map(node => ({ width: node.offsetWidth, height: node.offsetHeight,
      font: parseFloat(getComputedStyle(node).fontSize), text: Math.max(0,
        ...[...node.children].filter(child => ['SPAN', 'B'].includes(child.tagName) && !child.hidden)
          .map(child => child.getBoundingClientRect().width / zoom)) }));
    row.remove();
    return sizes;
  }

  const fit = () => {
    frame = null;
    if (!bar.isConnected || !bar.clientWidth) return;
    const available = items.filter(({ button }) => !button.hidden);
    const pinned = [...fixed.children].filter(node => node.tagName === 'BUTTON' && !node.hidden);
    const sizes = measure([...available.map(item => item.button), ...pinned, more]);
    const preferred = sizes.slice(available.length, -1);
    controls.style.removeProperty('column-gap');
    fixed.style.removeProperty('column-gap');
    let gap = parseFloat(getComputedStyle(controls).columnGap) || 0;
    let fixedGap = parseFloat(getComputedStyle(fixed).columnGap) || 0;
    const actionGap = parseFloat(getComputedStyle(actions).columnGap) || 0;
    const budget = bar.clientWidth * 0.6;
    const naturalFixed = preferred.reduce((sum, size) => sum + size.width, 0) + fixedGap * (pinned.length - 1);
    const inlineWidth = count => sizes.slice(available.length - count, available.length)
      .reduce((sum, size) => sum + size.width, 0) + Math.max(0, count - 1) * actionGap;
    let shown = available.length;
    let moreWidth = sizes.at(-1).width;
    if (shown > 0 && naturalFixed + inlineWidth(shown) + gap > budget) {
      shown -= 1;
      while (shown > 0 && naturalFixed + inlineWidth(shown) + moreWidth + 2 * gap > budget) shown -= 1;
    }
    const hidden = available.length - shown;
    const minimum = pinned.map((button, index) => Math.min(preferred[index].width,
      button.classList.contains('code-cache') || button.classList.contains('code-usage') ? 32
        : button.classList.contains('code-context') ? 28 : 24));
    if (minimum.reduce((sum, width) => sum + width, 0) + fixedGap * (pinned.length - 1)
        + (hidden ? 24 + gap : 0) > budget) {
      gap = fixedGap = 1;
      controls.style.columnGap = fixed.style.columnGap = '1px';
      for (const [index, button] of pinned.entries()) {
        minimum[index] = Math.min(minimum[index], button.classList.contains('code-cache') || button.classList.contains('code-usage')
          ? 28 : button.classList.contains('code-context') ? 24 : 20);
      }
    }
    const compact = minimum.reduce((sum, width) => sum + width, 0) + fixedGap * (pinned.length - 1)
      + (hidden ? 20 + gap : 0) > budget;
    controls.dataset.compact = String(compact);
    if (compact) {
      gap = fixedGap = 0;
      controls.style.columnGap = fixed.style.columnGap = '0px';
      for (const [index, button] of pinned.entries()) minimum[index] = Math.min(minimum[index],
        button.classList.contains('code-cache') ? 26 : button.classList.contains('code-usage') ? 24
          : button.classList.contains('code-context') ? 22 : 18);
    }
    if (hidden) moreWidth = Math.min(moreWidth, Math.max(20,
      budget - minimum.reduce((sum, width) => sum + width, 0) - fixedGap * (pinned.length - 1) - gap));
    const fixedRoom = budget - inlineWidth(shown) - (hidden ? moreWidth : 0) - gap * (Number(shown > 0) + Number(hidden > 0));
    const minimumSum = minimum.reduce((sum, width) => sum + width, 0);
    const growth = preferred.reduce((sum, size, index) => sum + size.width - minimum[index], 0);
    const ratio = growth ? Math.max(0, Math.min(1, (fixedRoom - fixedGap * (pinned.length - 1) - minimumSum) / growth)) : 1;
    for (const [index, button] of pinned.entries()) {
      const size = preferred[index];
      const width = minimum[index] + (size.width - minimum[index]) * ratio;
      button.style.minWidth = '0px';
      button.style.width = `${width}px`;
      button.style.height = `${button.classList.contains('code-context') ? width : size.height}px`;
      button.style.fontSize = `${Math.max(9, Math.min(size.font, size.text ? size.font * (width - 2) / size.text : size.font))}px`;
    }
    more.style.width = `${moreWidth}px`;
    more.hidden = !hidden;
    for (const [index, item] of available.entries()) {
      const inMenu = index < hidden;
      const target = inMenu ? overflow : actions;
      if (item.button.parentElement !== target) {
        popup.close();
        const next = available.slice(index + 1).find(other => other.button.parentElement === target)?.button || null;
        if (item.menu) target.insertBefore(item.menu, next);
        target.insertBefore(item.button, next);
      }
      if (item.label.hidden === inMenu) item.label.hidden = !inMenu;
      const label = (item.button.getAttribute('aria-label') || item.button.title || '').replace(/:\s*≥?\d+$/, '');
      if (item.label.textContent !== label) item.label.textContent = label;
    }
    if (!hidden) popup.close();
  };
  const schedule = () => { if (frame === null) frame = requestAnimationFrame(fit); };
  const resize = new ResizeObserver(schedule);
  resize.observe(bar);
  const mutation = new MutationObserver(schedule);
  for (const node of [actions, overflow, fixed]) mutation.observe(node, { childList: true, characterData: true,
    subtree: true, attributes: true, attributeFilter: ['hidden', 'aria-label'] });
  window.addEventListener('resize', schedule);
  schedule();
  return { unmount() {
    if (frame !== null) cancelAnimationFrame(frame);
    resize.disconnect(); mutation.disconnect();
    window.removeEventListener('resize', schedule);
    popup.close(); panel.remove();
  } };
}
