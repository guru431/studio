/* «Поиск» (спека 2026-10-21-studio-history-search-design): своя история по словам и фильтрам.
 *
 * Поле (пауза SEARCH_PAUSE_MS после ввода, Enter — сразу; «очистить» и голосовой ввод — field.js), чипы раздела,
 * модель (GET /api/search/models — модели своей истории), период «с … по …» и «сбросить». Запрос —
 * POST /api/search телом: адрес пишет журнал Apache и видно в истории браузера, поэтому слова и
 * фильтры живут только в памяти этого модуля (`memory`) — переживают уход в раздел и возврат, но не
 * перезагрузку; ни в адресе, ни в localStorage их нет.
 *
 * Выдача — новые сверху, по дням (как ленты), «Показать ещё» — курсором последней строки, если запрос
 * тот же, что у выдачи (иначе — новый поиск: ввод ещё ждал паузы). Строка —
 * значок раздела, заголовок, кусок текста с найденным (маркеры \u0002…\u0003 → <mark> узлами, не
 * HTML), модель и время; нажатие — ctx.navigate(href) (адрес проверяет HREF jobs.js). Номер запроса:
 * запоздалый ответ прежнего не ляжет. Состояние выдачи — #search-results[data-state]: idle | loading |
 * ready | error (по нему ждут тесты). */
import { api } from './api.js';
import { timeOnly } from './time.js';
import { dayKey, dayTitle } from './feed.js';
import { fieldBox } from './field.js';
import { HREF, ICONS } from './jobs.js';
import { el, words } from './ui.js';

export const SEARCH_PAUSE_MS = 300;
const LIMIT = 30;
const SECTIONS = [['', 'Все'], ['chat', 'Чат'], ['photo', 'Фото'], ['video', 'Видео'], ['speech', 'Речь'],
  ['transcribe', 'Разговоры'], ['ocr', 'Распознавание'], ['translate', 'Перевод']];
/** Разделы, где заголовок — тот же текст, что найден: кусок с выделением встаёт на его место. */
const SAME_TEXT = new Set(['photo', 'video', 'speech']);
const HAS_WORD = /[\p{L}\p{N}]/u;

const memory = { q: '', section: '', model: '', since: '', until: '' };
let screen = null;

function icon(section) {
  const box = el('span', { class: 'search-ico', 'aria-hidden': 'true' });
  box.innerHTML = `<svg class="ico" viewBox="0 0 24 24">${ICONS[section] || ''}</svg>`;   // константы jobs.js
  return box;
}

/** Текст с маркерами FTS5 — узлы: найденное — <mark>, остальное — текстом. */
function marked(text) {
  const out = [];
  let on = false;
  for (const part of String(text).split(/([\u0002\u0003])/)) {
    if (part === '\u0002') on = true;
    else if (part === '\u0003') on = false;
    else if (part) out.push(on ? el('mark', {}, part) : part);
  }
  return out;
}

function when(created) {
  return timeOnly(created);
}

function row(item, go) {
  const same = SAME_TEXT.has(item.section) && item.snippet;
  const title = el('b', { class: 'search-title' }, ...(same ? marked(item.snippet) : [item.title]));
  const snippet = !same && item.snippet ? el('span', { class: 'search-snippet' }, ...marked(item.snippet)) : null;
  const meta = el('span', { class: 'search-meta' }, [item.model, when(item.created)].filter(Boolean).join(' · '));
  const node = el('button', { type: 'button', class: 'search-row', 'data-section': item.section, 'data-id': item.id },
    icon(item.section), el('span', { class: 'search-text' }, title, snippet, meta));
  node.addEventListener('click', () => { if (HREF.test(item.href)) go(item.href); });
  return el('li', {}, node);
}

function mountScreen(view, ctx) {
  let asked = 0;               // номер запроса: запоздалый ответ прежнего не ляжет
  let timer = null;
  let items = [];
  let shown = '';              // запрос выдачи (без курсора): «Показать ещё» дописывает только к нему
  let more = false;
  let models = null;           // модели своей истории по разделам; пока не пришли — выбор не сбрасывается

  const q = el('input', { id: 'search-q', class: 'input', type: 'search', maxlength: '200', autocomplete: 'off',
    enterkeyhint: 'search', placeholder: 'Слова промпта, сообщения или записи', 'aria-label': 'Что искать' });
  q.value = memory.q;
  const field = fieldBox(q);
  const chips = el('div', { class: 'search-chips', role: 'group', 'aria-label': 'Раздел' },
    ...SECTIONS.map(([value, label]) => el('button', { type: 'button', class: 'chip', 'data-section': value,
      'aria-pressed': String(memory.section === value) }, label)));
  const model = el('select', { id: 'search-model', class: 'chip', 'aria-label': 'Модель' });
  const since = el('input', { id: 'search-since', class: 'input search-date', type: 'date', 'aria-label': 'С какого дня' });
  const until = el('input', { id: 'search-until', class: 'input search-date', type: 'date', 'aria-label': 'По какой день' });
  since.value = memory.since;
  until.value = memory.until;
  const reset = el('button', { type: 'button', id: 'search-reset', class: 'btn-link' }, 'сбросить');
  const status = el('p', { id: 'search-status', class: 'muted small', 'aria-live': 'polite' });
  const results = el('div', { id: 'search-results', 'data-state': 'idle' });
  const moreBtn = el('button', { type: 'button', id: 'search-more', class: 'btn', hidden: true }, 'Показать ещё');

  view.append(el('header', { class: 'head' }, el('h1', {}, 'Поиск')),
    el('div', { class: 'page search-page' },
      field.box, field.error, chips,
      el('div', { class: 'search-filters' }, model, el('label', { for: since.id }, 'с'), since,
        el('label', { for: until.id }, 'по'), until, reset),
      status, results, moreBtn));

  function paintModels() {
    const known = models || {};
    let names = memory.section ? known[memory.section] || [] : [...new Set(Object.values(known).flat())].sort();
    if (memory.model && !names.includes(memory.model)) {
      if (models) memory.model = '';
      else names = [memory.model];
    }
    model.replaceChildren(el('option', { value: '' }, 'Все модели'), ...names.map((n) => el('option', { value: n }, n)));
    model.value = memory.model;
    model.hidden = !names.length;
  }

  function paint() {
    const days = [];
    for (const item of items) {
      const k = dayKey(item.created);
      if (!days.length || days[days.length - 1].key !== k) {
        days.push({ key: k, list: el('ul', { class: 'search-list' }),
          title: el('h2', { class: 'feed-day-title' }, dayTitle(item.created)) });
      }
      days[days.length - 1].list.append(row(item, ctx.navigate));
    }
    results.replaceChildren(...days.map((d) => el('section', { class: 'search-day' }, d.title, d.list)));
    moreBtn.hidden = !more;
  }

  function wanted() {
    const body = { limit: LIMIT };
    for (const key of ['q', 'section', 'model', 'since', 'until']) if (memory[key]) body[key] = memory[key];
    return body;
  }

  async function run(append = false) {
    clearTimeout(timer);
    const mine = ++asked;
    const body = wanted();
    const key = JSON.stringify(body);
    // новый запрос ещё ждал паузы после ввода — «Показать ещё» начинает его заново, а не дописывает
    // его страницу (курсором прежнего) к прежнему списку
    if (key !== shown) append = false;
    if (!HAS_WORD.test(memory.q) && !memory.section && !memory.model && !memory.since && !memory.until) {
      items = [];
      shown = '';
      more = false;
      paint();
      status.textContent = 'Введите слова или выберите раздел, модель или период';
      results.dataset.state = 'idle';
      return;
    }
    if (append && items.length) {
      const last = items[items.length - 1];
      body.before = { created: last.created, item: last.id, part: last.part };
    }
    results.dataset.state = 'loading';
    moreBtn.disabled = true;
    try {
      const out = await api('POST', '/api/search', body);
      if (mine !== asked) return;
      items = append ? [...items, ...out.items] : out.items;
      shown = key;
      more = out.more;
      paint();
      status.textContent = items.length ? '' : 'Ничего не нашлось';
      results.dataset.state = 'ready';
    } catch (err) {
      if (mine !== asked) return;
      status.textContent = words(err);
      results.dataset.state = 'error';
    } finally {
      if (mine === asked) moreBtn.disabled = false;
    }
  }

  function later() {
    clearTimeout(timer);
    timer = setTimeout(run, SEARCH_PAUSE_MS);
  }

  q.addEventListener('input', () => {
    memory.q = q.value;
    later();
  });
  q.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.isComposing) return;
    event.preventDefault();
    memory.q = q.value;
    run();
  });
  chips.addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    memory.section = chip.dataset.section;
    for (const c of chips.children) c.setAttribute('aria-pressed', String(c === chip));
    paintModels();
    run();
  });
  model.addEventListener('change', () => {
    memory.model = model.value;
    run();
  });
  for (const [input, key] of [[since, 'since'], [until, 'until']]) {
    input.addEventListener('change', () => {
      memory[key] = input.value;
      run();
    });
  }
  reset.addEventListener('click', () => {
    memory.since = '';
    memory.until = '';
    since.value = '';
    until.value = '';
    run();
  });
  moreBtn.addEventListener('click', () => run(true));

  paintModels();
  api('GET', '/api/search/models').then((out) => {
    if (!screen || screen.results !== results) return;
    models = out.models;
    paintModels();
  }, () => {});
  run();
  if (matchMedia('(hover: hover)').matches) q.focus();
  return { results, stop() { clearTimeout(timer); asked += 1; } };
}

export function mount(view, ctx) {
  screen = mountScreen(view, ctx);
}

export function unmount() {
  if (screen) screen.stop();
  screen = null;
}
