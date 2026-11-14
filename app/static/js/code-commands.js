import { tr } from './i18n.js';
/* Активные Shell/Bash-команды текущего хода: счётчик и раскрываемые шаги в панели статуса. */
import { el } from './ui.js';
import { icon } from './icons.js';
import { popupPanel } from './menu.js';

export function commandsView(host, { stepNode, repaint }) {
  const count = el('span', { class: 'code-active-count', 'aria-hidden': 'true' }, '0');
  const button = el('button', { type: 'button', id: 'code-active-commands', class: 'icon-btn',
    'aria-label': tr("Выполняемые команды: 0"), title: tr("Выполняемые команды: 0") }, icon('code'), count);
  const body = el('div', { class: 'code-command-list' });
  const empty = el('p', { class: 'small muted', role: 'status' }, tr("Нет выполняемых команд"));
  const panel = el('div', { id: 'code-command-menu', class: 'code-command-menu' },
    el('p', { class: 'small muted' }, tr("Выполняемые команды")), empty, body);
  host.append(panel);
  const popup = popupPanel(button, panel, { label: tr("Выполняемые команды"), side: 'above', align: 'end' });
  let models = [];
  const nodes = new Map();
  function render() {
    empty.hidden = models.length > 0;
    const active = new Set(models.map(model => String(model.id)));
    for (const [id, entry] of nodes) if (!active.has(id)) { entry.node.remove(); nodes.delete(id); }
    for (const model of models) {
      const id = String(model.id);
      const previous = nodes.get(id);
      if (!previous || previous.model !== model) {
        const node = stepNode(model);
        nodes.set(id, { model, node });
        if (previous) {
          const focused = previous.node.contains(document.activeElement);
          node.open = previous.node.open;
          previous.node.replaceWith(node);
          if (focused) node.querySelector('summary')?.focus({ preventScroll: true });
        } else body.append(node);
      } else repaint(previous.node);
    }
  }
  panel.addEventListener('beforetoggle', event => { if (event.newState === 'open') render(); });
  button.addEventListener('click', () => { if (popup.isOpen()) body.querySelector('summary')?.focus({ preventScroll: true }); });
  return {
    button,
    update(steps) {
      models = (steps || []).filter(step => step.status === 'running' && ['Shell', 'Bash'].includes(step.tool));
      count.textContent = String(models.length);
      const label = tr("Выполняемые команды: {0}", models.length);
      button.title = label;
      button.setAttribute('aria-label', label);
      if (!models.length) { nodes.clear(); body.replaceChildren(); }
      if (popup.isOpen()) render();
    },
    unmount() { popup.close(); nodes.clear(); panel.remove(); },
  };
}
