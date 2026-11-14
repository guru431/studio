import { tr } from './i18n.js';
/* Серия картинок (спека 2026-10-21-studio-photo-presets-compare-series, раздел 7): 1–4 варианта
 * одним запросом, меняется seed или сила. Общая для поля «Фото» (чип «×1», панель — в его меню) и
 * окна «Изменить» (панель в окне).
 *
 * seriesControl({ prefix, canStrength, canSeed, from, onChange }): prefix — начало id; canStrength() —
 * можно ли менять силу (Lustify с исходным фото); canSeed() — получает ли модель seed (шлюз srvllm;
 * внешнему сервису серия — N независимых картинок: чип «×N» без строки «меняется», в запросе — vary
 * seed, seed сервер ему не шлёт); from() — сила «от» (ползунок раздела или поле окна);
 * onChange() — после каждой смены. Ответ — { panel, label(), value(), reset(), refresh() }: value() —
 * поле `series` запроса или null (×1); refresh() — перерисовать (сменились модель, фото, сила «от»).
 * Силы считаются так же, как на сервере (images.series_strengths): целыми двадцатыми. */
import { el } from './ui.js';

export const SERIES_MAX = 4;

/** count сил поровну от from до to включительно, округлённые до 0.05; повтор после округления — null. */
export function seriesStrengths(from, to, count) {
  const steps = [];
  for (let i = 0; i < count; i += 1) steps.push(Math.floor((from + (to - from) * i / (count - 1)) * 20 + 0.5));
  return new Set(steps).size === count ? steps.map((n) => n / 20) : null;
}

export function seriesControl({ prefix, canStrength, canSeed = () => true, from, onChange = () => {} }) {
  let count = 1;
  let vary = 'seed';
  // панель — форма, а не меню: выбор числа и того, что меняется, — группы переключателей (role=radio, стрелки)
  const option = (text, attrs) => el('button', { type: 'button', class: 'chip-option', role: 'radio', ...attrs }, text);
  const counts = Array.from({ length: SERIES_MAX }, (_, i) => option(`×${i + 1}`, { 'data-count': String(i + 1) }));
  const bySeed = option('seed', { 'data-vary': 'seed' });
  const byStrength = option(tr("сила"), { 'data-vary': 'strength' });
  const varyName = el('span', { class: 'muted small', id: `${prefix}-vary-name` }, tr("меняется:"));
  const varyBox = el('div', { class: 'series-vary', id: `${prefix}-vary`, role: 'radiogroup', 'aria-labelledby': varyName.id },
    varyName, bySeed, byStrength);
  const to = el('input', { type: 'range', id: `${prefix}-to`, min: '0.05', max: '1', step: '0.05', value: '0.75',
    'aria-label': tr("Сила до") });
  const toValue = el('output', { for: `${prefix}-to` }, '0.75');
  const toLabel = el('label', { class: 'strength', id: `${prefix}-to-label` }, tr("до"), to, toValue);
  const values = el('p', { class: 'series-values small', id: `${prefix}-values`, 'aria-live': 'polite' });
  const panel = el('div', { class: 'series-panel', id: `${prefix}-panel` },
    el('div', { class: 'series-counts', role: 'radiogroup', 'aria-label': tr("Сколько вариантов") }, ...counts), varyBox, toLabel, values);

  const strengthWanted = () => count > 1 && vary === 'strength' && canStrength();

  function paint() {
    for (const b of counts) b.setAttribute('aria-checked', String(Number(b.dataset.count) === count));
    for (const b of [...counts, bySeed, byStrength]) b.tabIndex = -1;
    varyBox.hidden = count < 2 || !canSeed();
    byStrength.hidden = !canStrength();
    bySeed.setAttribute('aria-checked', String(!strengthWanted()));
    byStrength.setAttribute('aria-checked', String(strengthWanted()));
    toLabel.hidden = values.hidden = !strengthWanted();
    for (const b of [...counts, bySeed, byStrength]) if (b.getAttribute('aria-checked') === 'true') b.tabIndex = 0;
    if (strengthWanted()) {
      const list = seriesStrengths(Number(from()), Number(to.value), count);
      values.textContent = list ? list.map((v) => v.toFixed(2)).join(' · ') : tr("слишком узкий диапазон для {0} вариантов", count);
      values.classList.toggle('error', !list);
    }
  }

  function refresh() {
    paint();
    onChange();
  }

  for (const b of counts) {
    b.addEventListener('click', () => {
      count = Number(b.dataset.count);
      refresh();
    });
  }
  for (const b of [bySeed, byStrength]) {
    b.addEventListener('click', () => {
      vary = b.dataset.vary;
      refresh();
    });
  }
  // стрелки внутри группы выбирают соседний вариант, Tab — в группу один раз (на выбранный)
  for (const group of [counts, [bySeed, byStrength]]) {
    for (const b of group) {
      b.addEventListener('keydown', (event) => {
        const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        const shown = group.filter((x) => !x.hidden);
        const next = shown[(shown.indexOf(b) + step + shown.length) % shown.length];
        next.click();
        next.focus();
      });
    }
  }
  to.addEventListener('input', () => {
    toValue.textContent = Number(to.value).toFixed(2);
    refresh();
  });
  paint();

  return {
    panel,
    refresh,
    label: () => {
      if (count < 2) return '×1';
      if (strengthWanted()) return tr("×{0} · сила", count);
      return canSeed() ? `×${count} · seed` : `×${count}`;
    },
    value: () => {
      if (count < 2) return null;
      return strengthWanted() ? { count, vary: 'strength', strength_to: Number(to.value) } : { count, vary: 'seed' };
    },
    reset: () => {
      count = 1;
      vary = 'seed';
      refresh();
    },
  };
}
