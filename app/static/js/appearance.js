/* Оформление. Тема (auto/light/dark) и акцент — в учётке, на всех устройствах (PATCH /api/me); размер текста,
 * масштаб и плотность — этого устройства (localStorage). Меню аватара (app.js) и «Настройки → Оформление»
 * (settings.js) — два вида одного состояния: выбор в одном сразу виден в другом (событие `studio-appearance`
 * на document). Ранний theme.js ставит сохранённые здесь тему, акцент, шрифт, масштаб и плотность до профиля.
 *
 * Сохранение — последовательными PATCH: медленный ответ старого выбора не перезапишет новый. Отказ виден под
 * выбором с «Повторить сохранение»; несохранённый выбор действует в этом окне и не откатывается перечитанным
 * профилем (возвращение на экран, `online` повторяет сохранение). */
import { api, loadMe, me } from './api.js';
import { el } from './ui.js';

/** Акцент учётки — как profile.ACCENTS; первый — умолчание (без data-accent на <html>). */
export const ACCENTS = [['orange', 'Оранжевый'], ['blue', 'Синий'], ['violet', 'Фиолетовый'], ['rose', 'Малиновый']];
export const THEME_NAMES = { auto: 'Как на устройстве', light: 'Бумага · светлая', dark: 'Графит · тёмная' };
const THEME_KEY = 'studio-theme';
const ACCENT_KEY = 'studio-accent';
const DENSITY_KEY = 'studio-density';
const THEME_COLORS = { light: '#fbfaf7', dark: '#1b1b1b' };
const SCALES = [50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100];
const darkMedia = matchMedia('(prefers-color-scheme: dark)');

const mine = { theme: null, accent: null };   // выбор этого окна; null — как в профиле
const revisions = { theme: 0, accent: 0 };
let pending = {};                              // выбранное, ещё не подтверждённое сервером
let saving = false;
let state = '';                                // '', 'saving', 'saved', 'failed'

function changed() {
  document.dispatchEvent(new Event('studio-appearance'));
}

function store(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // хранилище закрыто — выбор всё равно применён
  }
}

function read(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

/** Тема и акцент, которые действуют в этом окне. */
export function current() {
  return { theme: mine.theme ?? me.theme, accent: mine.accent ?? me.accent ?? ACCENTS[0][0] };
}

export function visibleTheme() {
  const { theme } = current();
  if (theme === 'dark' || theme === 'light') return theme;
  return darkMedia.matches ? 'dark' : 'light';
}

export function applyTheme(theme) {
  const root = document.documentElement;
  const fixed = theme === 'dark' || theme === 'light';
  if (fixed) root.dataset.theme = theme;
  else delete root.dataset.theme;
  store(THEME_KEY, fixed ? theme : null);
  const [light, dark] = document.querySelectorAll('meta[name="theme-color"]');
  if (light) light.content = fixed ? THEME_COLORS[theme] : THEME_COLORS.light;
  if (dark) dark.content = fixed ? THEME_COLORS[theme] : THEME_COLORS.dark;
  const scheme = (name) => (fixed ? (theme === name ? 'all' : 'not all') : `(prefers-color-scheme: ${name})`);
  const paper = document.getElementById('hl-paper');
  const graphite = document.getElementById('hl-graphite');
  if (paper) paper.media = scheme('light');
  if (graphite) graphite.media = scheme('dark');
}

export function applyAccent(accent) {
  const known = ACCENTS.some(([id]) => id === accent) && accent !== ACCENTS[0][0];
  if (known) document.documentElement.dataset.accent = accent;
  else delete document.documentElement.dataset.accent;
  store(ACCENT_KEY, known ? accent : null);
}

/** Состояние сохранения для строки под выбором: { text, error, retry }. */
export function saveState() {
  if (state === 'saving') return { text: 'Сохраняем оформление…', error: false, retry: false };
  if (state === 'failed') return { text: 'Не удалось сохранить оформление. Выбор действует в этом окне.', error: true, retry: true };
  if (state === 'saved') return { text: 'Оформление сохранено для учётки.', error: false, retry: false };
  return { text: '', error: false, retry: false };
}

export async function retrySave() {
  if (saving || !Object.keys(pending).length) return;
  saving = true;
  state = 'saving';
  changed();
  while (Object.keys(pending).length) {
    const body = { ...pending };
    try {
      await api('PATCH', '/api/me', body);
    } catch {
      if (Object.entries(body).some(([key, value]) => pending[key] !== value)) continue;   // выбрали заново — шлём новое
      saving = false;
      state = 'failed';
      changed();
      return;
    }
    for (const [key, value] of Object.entries(body)) if (pending[key] === value) delete pending[key];
  }
  saving = false;
  state = 'saved';
  changed();
}

/** Выбор темы или акцента: сразу на экране, затем в учётку. */
export function choose(field, value) {
  mine[field] = me[field] = pending[field] = value;
  revisions[field]++;
  if (field === 'theme') applyTheme(value);
  else applyAccent(value);
  changed();
  retrySave();
}

/** Один раз после профиля: тема и акцент учётки на экран, повтор сохранения при сети, перечитывание профиля при
 * возвращении на экран (тему и акцент могли сменить на другом устройстве; выбранное здесь и ещё не сохранённое —
 * главнее перечитанного). */
export function setupAppearance() {
  mine.theme = me.theme;
  mine.accent = me.accent;
  applyTheme(mine.theme);
  applyAccent(mine.accent);
  window.addEventListener('online', retrySave);
  darkMedia.addEventListener('change', changed);
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    const before = { ...revisions };
    const unsaved = new Set(Object.keys(pending));
    try {
      await loadMe();
    } catch {
      return;              // нет связи — остаётся прежнее; сессия кончилась — api.js увёл на вход
    }
    for (const field of ['theme', 'accent']) {
      if (unsaved.has(field) || field in pending || before[field] !== revisions[field]) me[field] = mine[field];
      else mine[field] = me[field];
    }
    applyTheme(mine.theme);
    applyAccent(mine.accent);
    changed();
    document.dispatchEvent(new Event('studio-me'));   // открытый раздел (чат: ключ Exa) перерисует своё
  });
}

/** Подписка узла на смену оформления; отписывается сама, когда узла уже нет на странице. */
export function follow(node, paint) {
  const listener = () => {
    if (!node.isConnected) document.removeEventListener('studio-appearance', listener);
    else paint();
  };
  document.addEventListener('studio-appearance', listener);
  paint();
}

/** Строка состояния сохранения с «Повторить сохранение» — для «Настроек» (у меню — свои узлы в index.html). */
function saveLine(id) {
  const note = el('p', { class: 'status', id, role: 'status', 'aria-live': 'polite' });
  const retry = el('button', { class: 'btn-link', type: 'button', id: `${id}-retry` }, 'Повторить сохранение');
  retry.addEventListener('click', retrySave);
  const box = el('div', { class: 'appearance-save' }, note, retry);
  follow(box, () => paintSave(note, retry));
  return box;
}

export function paintSave(note, retry) {
  const { text, error, retry: again } = saveState();
  note.textContent = text;
  note.classList.toggle('error', error);
  retry.hidden = !again;
}

/* ---- «Настройки → Оформление» --------------------------------------------------------------------------------- */

function sample() {
  // образец: своё сообщение, ответ, строка кода, главная и обычная кнопки — цвета берутся из токенов карточки
  const code = el('code', {}, el('span', { class: 'theme-sample-kw' }, 'const'), ' answer = ',
    el('span', { class: 'theme-sample-str' }, "'да'"), ';');
  return el('span', { class: 'theme-sample', 'aria-hidden': 'true' },
    el('span', { class: 'theme-sample-own' }, 'Привет! Покажи пример'),
    el('span', { class: 'theme-sample-reply' }, 'Вот так:'),
    el('span', { class: 'theme-sample-code' }, code),
    el('span', { class: 'theme-sample-buttons' }, el('span', { class: 'theme-sample-primary' }, 'Отправить'),
      el('span', { class: 'theme-sample-plain' }, 'Отмена')));
}

/** Тема и акцент учётки: карточки с образцом и образцы акцента — радиокнопки (стрелки переключают). */
export function accountLookBlock() {
  const cards = el('div', { class: 'theme-cards', role: 'radiogroup', 'aria-labelledby': 'appearance-theme-title' },
    ...['light', 'dark', 'auto'].map((theme) => {
      const input = el('input', { type: 'radio', class: 'sr', name: 'appearance-theme', value: theme,
        id: `appearance-theme-${theme}` });
      input.addEventListener('change', () => { if (input.checked) choose('theme', theme); });
      return el('label', { class: 'theme-card', 'data-preview': theme }, input, sample(),
        el('span', { class: 'theme-card-name' }, THEME_NAMES[theme]));
    }));
  const accents = el('div', { class: 'accent-choices', role: 'radiogroup', 'aria-labelledby': 'appearance-accent-title' },
    ...ACCENTS.map(([accent, name]) => {
      const input = el('input', { type: 'radio', class: 'sr', name: 'appearance-accent', value: accent,
        id: `appearance-accent-${accent}` });
      input.addEventListener('change', () => { if (input.checked) choose('accent', accent); });
      return el('label', { class: 'accent-choice', 'data-accent': accent }, input,
        el('span', { class: 'accent-swatch', 'aria-hidden': 'true' }), el('span', {}, name));
    }));
  const block = el('section', { class: 'block appearance-account' },
    el('h2', { id: 'appearance-theme-title' }, 'Тема'),
    el('p', { class: 'muted small scope-note' }, 'Вся учётка — на всех ваших устройствах.'),
    cards,
    el('h3', { id: 'appearance-accent-title' }, 'Акцент'),
    el('p', { class: 'muted small' }, 'Цвет главных кнопок, ссылок и выбранного; каждый вариант проверен на контраст в обеих темах.'),
    accents, saveLine('appearance-save-status'));
  follow(block, () => {
    const { theme, accent } = current();
    for (const input of block.querySelectorAll('input[name="appearance-theme"]')) input.checked = input.value === theme;
    for (const input of block.querySelectorAll('input[name="appearance-accent"]')) input.checked = input.value === accent;
  });
  return block;
}

function segment(name, legend, options, value, onChange) {
  const box = el('fieldset', { class: 'segment', id: `${name}-choice` }, el('legend', {}, legend));
  for (const [id, text] of options) {
    const input = el('input', { type: 'radio', class: 'sr', name, value: id, id: `${name}-${id}`, checked: id === value });
    input.addEventListener('change', () => { if (input.checked) onChange(id); });
    box.append(el('label', { class: 'segment-option' }, input, el('span', {}, text)));
  }
  return box;
}

/** Размер текста, масштаб и плотность — только этого устройства; применяются без перезагрузки. */
export function deviceLookBlock() {
  const root = document.documentElement;
  const font = el('input', { id: 'appearance-font', type: 'range', min: '85', max: '150', step: '5', value: '100' });
  // сенсорный экран — не мельче 75%: цели касания и подписи иначе меньше пальца; мышь — до 50% (theme.js — тот же предел)
  const floor = matchMedia('(pointer: coarse)').matches ? 75 : 50;
  const scale = el('select', { id: 'appearance-scale', class: 'input' }, ...SCALES.map((n) =>
    el('option', { value: n, disabled: n < floor }, `${n}%`)));
  const fontValue = el('output', { for: 'appearance-font' });
  const density = segment('appearance-density', 'Плотность', [['normal', 'Обычно'], ['compact', 'Компактно']],
    read(DENSITY_KEY) === 'compact' ? 'compact' : 'normal', (value) => {
      if (value === 'compact') root.dataset.density = 'compact';
      else delete root.dataset.density;
      store(DENSITY_KEY, value === 'compact' ? 'compact' : null);
    });
  const reset = el('button', { type: 'button', class: 'btn-link', id: 'appearance-reset' }, 'Сбросить шрифт и масштаб');
  font.value = String(Math.max(85, Math.min(150, Number(read('studio-font-size')) || 100)));
  scale.value = String(Math.max(floor, Number(read('studio-ui-scale')) || 100));
  if (!scale.value) scale.value = '100';
  function apply() {
    root.style.setProperty('--font-scale', Number(font.value) / 100);
    root.style.setProperty('--ui-scale', Number(scale.value) / 100);
    root.style.zoom = String(Number(scale.value) / 100);
    fontValue.textContent = `${font.value}%`;
    store('studio-font-size', font.value);
    store('studio-ui-scale', scale.value);
  }
  font.addEventListener('input', apply);
  scale.addEventListener('change', apply);
  reset.addEventListener('click', () => { font.value = scale.value = '100'; apply(); });
  fontValue.textContent = `${font.value}%`;
  return el('section', { class: 'block appearance-device' }, el('h2', {}, 'Вид на этом устройстве'),
    el('p', { class: 'muted small scope-note' }, 'Это устройство — в других браузерах и на других устройствах свой выбор.'),
    el('div', { class: 'appearance-controls', id: 'appearance-controls' },
      el('label', { for: font.id }, 'Размер шрифта', fontValue), font,
      el('label', { for: scale.id }, 'Масштаб интерфейса'), scale, reset),
    density,
    el('p', { class: 'muted small' }, '«Компактно» — ниже кнопки и меньше просветы: больше строк в «Агентах» и лентах. '
      + 'Цели касания на сенсорном экране не меньше 44 px.'));
}
