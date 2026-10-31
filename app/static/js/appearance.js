/* Размер текста и масштаб — предпочтения этого устройства, применяются без перезагрузки. */
import { el } from './ui.js';

export function setupAppearance() {
  const host = document.getElementById('appearance-controls');
  if (!host) return;
  const root = document.documentElement;
  const font = el('input', { id: 'appearance-font', type: 'range', min: '85', max: '150', step: '5', value: '100' });
  const scale = el('select', { id: 'appearance-scale' }, ...[50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100].map((n) =>
    el('option', { value: n }, `${n}%`)));
  const fontValue = el('output', { for: 'appearance-font' });
  const reset = el('button', { type: 'button', class: 'btn-link' }, 'Сбросить');
  function read(key, fallback) {
    try { return Number(localStorage.getItem(key)) || fallback; } catch { return fallback; }
  }
  font.value = String(Math.max(85, Math.min(150, read('studio-font-size', 100))));
  scale.value = String(read('studio-ui-scale', 100));
  if (!scale.value) scale.value = '100';
  function apply() {
    root.style.setProperty('--font-scale', Number(font.value) / 100);
    root.style.setProperty('--ui-scale', Number(scale.value) / 100);
    root.style.zoom = String(Number(scale.value) / 100);
    fontValue.textContent = `${font.value}%`;
    try {
      localStorage.setItem('studio-font-size', font.value);
      localStorage.setItem('studio-ui-scale', scale.value);
    } catch { /* закрытое хранилище — выбор действует до перезагрузки */ }
  }
  font.addEventListener('input', apply);
  scale.addEventListener('change', apply);
  reset.addEventListener('click', () => { font.value = scale.value = '100'; apply(); });
  host.append(el('label', { for: font.id }, 'Размер шрифта', fontValue), font,
    el('label', { for: scale.id }, 'Масштаб интерфейса'), scale, reset);
  apply();
}
