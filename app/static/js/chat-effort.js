/* Уровень рассуждения диалога «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 40; сервер —
 * profiles.effort_params): чип #effort-chip с меню #effort-menu (menu.js) — «Как у модели», «Низкий», «Средний»,
 * «Высокий». Виден только у подключений, чей провайдер принимает уровень (`effort_ok` диалога, `effort`
 * подключения нового). Выбор отдаёт `choose(level | null)` — раздел пишет его в диалог (PATCH {effort}) или держит
 * черновиком нового. */
import { tr } from './i18n.js';
import { icon } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { el } from './ui.js';

const LEVELS = () => [[null, tr("Как у модели")], ['low', tr("Низкий")], ['medium', tr("Средний")], ['high', tr("Высокий")]];

/** `current()` — 'low' | 'medium' | 'high' | null; `offered()` — уровень выбирается; `choose(level)` — Promise;
 * `failed(err)` — отказ. Ответ — { chip, menu, paint(), close() }. */
export function effortChooser({ current, offered, choose, failed = () => {} }) {
  const level = el('span', { class: 'effort-level' });
  const chip = el('button', { type: 'button', id: 'effort-chip', class: 'chip ctl-chip effort-chip', 'aria-haspopup': 'menu',
    title: tr("Уровень рассуждения"), hidden: true }, icon('bulb'), el('span', { class: 'toggle-word' }, tr("Рассуждение")), level);
  const menu = el('div', { id: 'effort-menu', class: 'effort-menu', role: 'menu', 'aria-label': tr("Уровень рассуждения") });
  const popup = popupMenu(chip, menu, { side: 'above', fill: (close) => {
    const chosen = current() || null;
    const items = LEVELS().map(([value, name]) => menuItem(name, { radio: true, checked: chosen === value,
      attrs: { class: 'effort-option', 'data-effort': value || 'default' }, onSelect: async () => {
        close(true);
        try {
          await choose(value);
        } catch (err) {
          failed(err);
        }
        paint();
      } }));
    return [items, items];
  } });

  function paint() {
    chip.hidden = !offered();
    const now = current() || null;
    const name = LEVELS().find(([value]) => value === now)[1];
    level.textContent = name;
    chip.setAttribute('aria-label', tr("Уровень рассуждения: {0}", name));
    chip.setAttribute('aria-pressed', String(!!now));
    if (chip.hidden) popup.close();
  }

  paint();
  return { chip, menu, paint, close: () => popup.close() };
}
