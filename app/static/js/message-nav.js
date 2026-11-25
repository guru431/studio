import { tr } from './i18n.js';
/* Стрелки ленты «Агентов» и «Чата»: к предыдущему и следующему своему сообщению, «Развернуть все» шаги.
 * `node` — `nav.code-message-nav[hidden]` с кнопками `#${prefix}-message-up`, `#${prefix}-toggle-steps`,
 * `#${prefix}-message-down`; ставит его хозяин ленты. `earlier` — `{ has(), busy(), load() }` догрузки начала ленты
 * или `null` (история целиком); `act()` — любое нажатие; `follow(false)` — прыжок к сообщению, `follow(true)` — в
 * конец; `change(apply)` — правка высоты ленты (лента внизу остаётся внизу). Слушатели `scroll` ленты и `resize`
 * окна — свои, снимает `unmount()`. */
import { el } from './ui.js';
import { icon } from './icons.js';

export function messageNav({ scroller, feed, prefix = 'code', own = '.code-msg.user', steps = 'details.code-step', expandLabels,
  stuck, ready = () => true, earlier = null, act = () => {}, follow = () => {}, change = (apply) => apply() }) {
  const [expandLabel, collapseLabel] = expandLabels || [tr("Развернуть все команды"), tr("Свернуть все команды")];
  const previous = el('button', { type: 'button', id: `${prefix}-message-up`, class: 'icon-btn',
    'aria-label': tr("Предыдущее моё сообщение"), title: tr("Предыдущее моё сообщение"), disabled: true });
  const next = el('button', { type: 'button', id: `${prefix}-message-down`, class: 'icon-btn',
    'aria-label': tr("Следующее моё сообщение"), title: tr("Следующее моё сообщение"), disabled: true });
  const allSteps = el('button', { type: 'button', id: `${prefix}-toggle-steps`, class: 'icon-btn',
    'aria-label': expandLabel, title: expandLabel, 'aria-pressed': 'false', disabled: true });
  previous.append(icon('upward'));
  next.append(icon('downward'));
  allSteps.append(icon('expand'));
  const node = el('nav', { class: 'code-message-nav', 'aria-label': tr("Навигация по чату"), hidden: true }, previous, allSteps, next);
  let alive = true;
  let messageTarget = null;
  let messageScrollTop = 0;
  let expandSteps = false;
  const hasEarlier = () => !!earlier?.has();

  /** Якорь — верх ленты. Сохраняем выбранное сообщение даже у конца прокрутки. */
  function messageNeighbours() {
    const users = [...feed.querySelectorAll(own)];
    if (messageTarget && (!feed.contains(messageTarget) || Math.abs(scroller.scrollTop - messageScrollTop) > 2)) {
      messageTarget = null;
    }
    const index = users.indexOf(messageTarget);
    if (index >= 0) return { before: users[index - 1], after: users[index + 1] };
    if (stuck()) return { before: users.at(-1), after: null };
    const top = messageTop();
    return { before: users.findLast((item) => item.getBoundingClientRect().top < top - 2),
      after: users.find((item) => item.getBoundingClientRect().top > top + 2) };
  }

  function messageTop() {
    return scroller.getBoundingClientRect().top;
  }

  function scrollByViewportPixels(delta) {
    const scale = Number(document.documentElement.style.getPropertyValue('--ui-scale')) || 1;
    scroller.scrollTop += delta / scale;
  }

  function paint() {
    if (!scroller || !ready()) return;
    const { before, after } = messageNeighbours();
    const list = [...feed.querySelectorAll(steps)];
    const expanded = list.length > 0 && list.every((step) => step.open);
    const stepsLabel = expanded ? collapseLabel : expandLabel;
    allSteps.disabled = list.length === 0;
    allSteps.setAttribute('aria-label', stepsLabel);
    allSteps.setAttribute('aria-pressed', String(expanded));
    allSteps.title = stepsLabel;
    allSteps.querySelector('path').setAttribute('d', expanded ? 'm8 5 4 4 4-4m-8 14 4-4 4 4' : 'm8 9 4-4 4 4m-8 6 4 4 4-4');
    node.hidden = !feed.querySelector(own) && !list.length && !hasEarlier();
    previous.disabled = !!earlier?.busy() || (!before && !hasEarlier());
    next.disabled = !after && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    const nextLabel = after ? tr("Следующее моё сообщение") : tr("В конец чата");
    next.setAttribute('aria-label', nextLabel);
    next.title = nextLabel;
  }

  async function jumpMessage(direction) {
    act();
    let target = messageNeighbours()[direction < 0 ? 'before' : 'after'];
    while (!target && direction < 0 && hasEarlier()) {
      const first = feed.querySelector(own);
      if (!await earlier.load() || !alive) return;
      const users = [...feed.querySelectorAll(own)];
      target = first ? users[users.indexOf(first) - 1] : users.at(-1);
    }
    if (!scroller) return;
    if (!target) {
      if (direction > 0) {
        messageTarget = null;
        follow(true);
        scroller.scrollTop = scroller.scrollHeight;
        paint();
      }
      return;
    }
    messageTarget = target;
    follow(false);
    scrollByViewportPixels(target.getBoundingClientRect().top - messageTop());
    messageScrollTop = scroller.scrollTop;
    paint();
  }

  previous.addEventListener('click', () => jumpMessage(-1));
  allSteps.addEventListener('click', () => {
    act();
    const list = [...feed.querySelectorAll(steps)];
    expandSteps = list.some((step) => !step.open);
    change(() => { for (const step of list) step.open = expandSteps; });
    paint();
  });
  next.addEventListener('click', () => jumpMessage(1));
  scroller?.addEventListener('scroll', paint, { passive: true });
  window.addEventListener('resize', paint);

  return {
    node,
    paint,
    target: () => messageTarget,
    drop() { messageTarget = null; },
    pin() { messageScrollTop = scroller.scrollTop; },
    expanded: () => expandSteps,
    scrollBy: scrollByViewportPixels,
    unmount() {
      alive = false;
      scroller?.removeEventListener('scroll', paint);
      window.removeEventListener('resize', paint);
    },
  };
}
