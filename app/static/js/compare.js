/* Сравнение двух картинок (спека 2026-10-21-studio-photo-presets-compare-series, раздел 6): окно
 * `#compare` во весь экран, как просмотр (`<dialog>` через showModal — поверх просмотра). Две
 * картинки-оригинала в одной рамке (object-fit: contain, одинаковая область), правая обрезана по
 * вертикальной границе (clip-path). Граница — ползунок (клавиши ←/→) и перетаскивание пальцем или
 * мышью по рамке. «Поменять местами»; крестик и Escape закрывают.
 *
 * openCompare(host, left, right): host — корень раздела; left и right — { media_id, label }
 * (подпись — шаг цепочки или модель · seed · сила). Текст подписей — textContent. */
import { button, el } from './ui.js';
import { icon } from './icons.js';

const KEY_STEP = 5;              // ←/→ вне ползунка — на столько процентов

function original(id) {
  return `/api/media/${encodeURIComponent(id)}`;
}

export function openCompare(host, left, right) {
  let sides = [left, right];
  let at = 50;
  const leftImg = el('img', { class: 'compare-img compare-left', alt: 'Слева', draggable: 'false' });
  const rightImg = el('img', { class: 'compare-img compare-right', alt: 'Справа', draggable: 'false' });
  const line = el('span', { class: 'compare-line', 'aria-hidden': 'true' });
  const frame = el('div', { class: 'compare-frame' }, leftImg, rightImg, line);
  const leftLabel = el('span', { class: 'compare-label compare-label-left' });
  const rightLabel = el('span', { class: 'compare-label compare-label-right' });
  const range = el('input', { type: 'range', id: 'compare-range', min: '0', max: '100', step: '1', value: '50',
    'aria-label': 'Граница сравнения' });
  const swap = button('Поменять местами', { class: 'btn btn-sm', id: 'compare-swap' });
  const close = el('button', { type: 'button', class: 'icon-btn viewer-close', id: 'compare-close', 'aria-label': 'Закрыть',
    title: 'Закрыть' }, icon('x'));
  const dialog = el('dialog', { id: 'compare', class: 'viewer compare', 'aria-label': 'Сравнение картинок' },
    el('div', { class: 'viewer-top' }, close), el('div', { class: 'compare-stage' }, frame),
    el('div', { class: 'viewer-bar compare-bar' }, el('div', { class: 'compare-labels' }, leftLabel, rightLabel), range,
      el('div', { class: 'viewer-actions' }, swap)));

  function paint() {
    leftImg.src = original(sides[0].media_id);
    rightImg.src = original(sides[1].media_id);
    leftLabel.textContent = sides[0].label || '';
    rightLabel.textContent = sides[1].label || '';
  }

  function set(percent) {
    at = Math.min(100, Math.max(0, percent));
    range.value = String(Math.round(at));
    rightImg.style.clipPath = `inset(0 0 0 ${at}%)`;
    line.style.left = `${at}%`;
  }

  let dragging = null;
  const follow = (event) => {
    const box = frame.getBoundingClientRect();
    if (box.width > 0) set(((event.clientX - box.left) / box.width) * 100);
  };
  frame.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    dragging = event.pointerId;
    try {
      frame.setPointerCapture(event.pointerId);     // палец ушёл за рамку — граница идёт за ним
    } catch {
      // указатель уже отпущен: граница встанет по нажатию
    }
    follow(event);
  });
  frame.addEventListener('pointermove', (event) => { if (dragging === event.pointerId) follow(event); });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    frame.addEventListener(type, () => { dragging = null; });
  }
  range.addEventListener('input', () => set(Number(range.value)));
  dialog.addEventListener('keydown', (event) => {
    if (event.target === range || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) return;
    event.preventDefault();
    set(at + (event.key === 'ArrowLeft' ? -KEY_STEP : KEY_STEP));
  });
  swap.addEventListener('click', () => {
    sides = [sides[1], sides[0]];
    paint();
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove());

  paint();
  set(at);
  host.append(dialog);
  dialog.showModal();
  range.focus();
  return dialog;
}
