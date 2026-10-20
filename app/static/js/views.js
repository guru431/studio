/* Вид ленты разделов «Фото» и «Видео»: превью (крупно, с промптом), плитка (сетка миниатюр) и
 * таблица (строка на запись). Кнопки — в шапке раздела; выбор живёт на устройстве (localStorage
 * `studio-view-<раздел>`): телефону и компьютеру удобно разное. Вид ставится атрибутом
 * data-view ленты, по нему и раскладывает CSS. */
import { el } from './ui.js';

const VIEWS = [
  ['preview', 'Превью', '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 16l5-5 4 4 3-3 6 6"/>'],
  ['tiles', 'Плитка', '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
    + '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>'],
  ['table', 'Таблица', '<path d="M10 6h10M10 12h10M10 18h10"/><rect x="3.5" y="4.5" width="3" height="3" rx=".8"/>'
    + '<rect x="3.5" y="10.5" width="3" height="3" rx=".8"/><rect x="3.5" y="16.5" width="3" height="3" rx=".8"/>'],
];

/** Кнопки вида раздела `section` → { node, value }; onChange(вид) — после смены. */
export function viewSwitch(section, fallback, onChange) {
  const key = `studio-view-${section}`;
  let value = fallback;
  try {
    const saved = localStorage.getItem(key);
    if (VIEWS.some((v) => v[0] === saved)) value = saved;
  } catch {
    // хранилище закрыто — вид по умолчанию
  }
  const buttons = VIEWS.map(([name, label, path]) => {
    const b = el('button', { type: 'button', class: 'icon-btn', id: `${section}-view-${name}`, 'data-view': name,
      'aria-label': label, title: label });
    b.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;   // константы выше
    b.addEventListener('click', () => {
      if (name === value) return;
      value = name;
      try {
        localStorage.setItem(key, name);
      } catch {
        // не запомнится — вид всё равно сменён
      }
      paint();
      onChange(name);
    });
    return b;
  });
  const paint = () => { for (const b of buttons) b.setAttribute('aria-pressed', String(b.dataset.view === value)); };
  paint();
  return { node: el('div', { class: 'view-switch', role: 'group', 'aria-label': 'Вид ленты' }, ...buttons),
    get value() { return value; } };
}

/** Строка заголовков вида «Таблица» (над лентой, видна только в нём). */
export function tableHead(titles) {
  return el('div', { class: 'feed-head', 'aria-hidden': 'true' }, ...titles.map((t) => el('span', {}, t)));
}

/** Время записи «14:05» (день — в заголовке группы ленты). */
export function timeOf(created) {
  return new Date(created * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
