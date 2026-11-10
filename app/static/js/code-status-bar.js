/* Статус получает половину строки; дополнительные действия уходят в панель у «⋯». */
import { el } from './ui.js';
import { icon } from './icons.js';
import { popupPanel } from './menu.js';

export function statusActions(host, bar, activity, actions, fixed) {
  const more = el('button', { type: 'button', id: 'code-status-more', class: 'icon-btn',
    'aria-label': 'Другие действия чата', title: 'Другие действия чата', hidden: true });
  more.append(icon('more'));
  const panel = el('div', { id: 'code-status-menu', class: 'code-status-menu' });
  const controls = el('div', { class: 'code-status-controls' }, actions, more, fixed);
  bar.replaceChildren(activity, controls);
  host.append(panel);
  const popup = popupPanel(more, panel, { label: 'Действия чата', side: 'above', align: 'end' });
  let collapsed = false;
  let actionWidth = 0;
  let frame = null;
  const fit = () => {
    frame = null;
    if (!bar.isConnected || !bar.clientWidth) return;
    const gap = parseFloat(getComputedStyle(controls).columnGap) || 0;
    if (!collapsed) actionWidth = actions.getBoundingClientRect().width;
    const zoom = Number(getComputedStyle(document.documentElement).zoom) || 1;
    const needed = actionWidth + fixed.getBoundingClientRect().width + gap * zoom;
    const next = needed > bar.getBoundingClientRect().width / 2;
    if (next === collapsed) return;
    collapsed = next;
    popup.close();
    more.hidden = !collapsed;
    if (collapsed) panel.append(actions);
    else controls.prepend(actions);
  };
  const schedule = () => { if (frame === null) frame = requestAnimationFrame(fit); };
  const resize = new ResizeObserver(schedule);
  resize.observe(bar);
  resize.observe(fixed);
  const mutation = new MutationObserver(schedule);
  mutation.observe(actions, { childList: true, subtree: true, attributes: true,
    attributeFilter: ['hidden', 'disabled'] });
  window.addEventListener('resize', schedule);
  schedule();
  return { unmount() {
    if (frame !== null) cancelAnimationFrame(frame);
    resize.disconnect(); mutation.disconnect();
    window.removeEventListener('resize', schedule);
    popup.close(); panel.remove();
  } };
}
