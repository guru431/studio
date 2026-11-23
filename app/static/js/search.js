import { tr } from './i18n.js';
/* «Поиск» (спека 2026-10-21-studio-history-search-design): своя история по словам и фильтрам.
 *
 * Поле (пауза SEARCH_PAUSE_MS после ввода, Enter — сразу; «очистить» и голосовой ввод — field.js), чипы раздела,
 * модель (GET /api/search/models — модели своей истории), даты и быстрые периоды. Запрос —
 * POST /api/search телом: адрес пишет журнал Apache и видно в истории браузера, поэтому слова и
 * фильтры живут только в памяти этого модуля (`memory`) — переживают уход в раздел и возврат, но не
 * перезагрузку; ни в адресе, ни в localStorage их нет.
 *
 * Выдача — новые сверху, по дням (как ленты), «Показать ещё» — курсором последней строки, если запрос
 * тот же, что у выдачи (иначе — новый поиск: ввод ещё ждал паузы). Строка —
 * значок раздела и доступная миниатюра, заголовок, кусок текста с найденным (маркеры \u0002…\u0003 → <mark> узлами, не
 * HTML), модель (подпись — models.js, как в разделах) и время; у найденного в документе диалога — имя документа своей строкой (`attachment`); нажатие —
 * ctx.navigate(href) (адрес проверяет HREF jobs.js или MESSAGE_HREF: строка диалога — `chat/<id>?m=<сообщение>`,
 * chat.js прокручивает к нему и подсвечивает). Номер запроса:
 * запоздалый ответ прежнего не ляжет. Состояние выдачи — #search-results[data-state]: idle | loading |
 * ready | error (по нему ждут тесты); #search-status (aria-live) называет число найденного.
 *
 * Чип «Агенты» — история на компьютерах: POST /api/code/search (мост 0.47.0, `chat_search_all`) — каждому своему
 * компьютеру на связи одна просьба по всем проектам; выдача — группами по компьютеру, строка ведёт в чат, закрытый или
 * недоступный компьютер — строкой в группе. Фильтры модели и дат к ней не относятся и скрыты. Поиск на компьютере
 * идёт до 15 с: новый запрос ждёт конца прежнего (мост держит один поиск), последний ответ — в памяти модуля. */
import { api } from './api.js';
import { ENGINE_NAMES } from './code-options.js';
import { calendarRange, timeOnly } from './time.js';
import { dayKey, dayTitle } from './feed.js';
import { fieldBox } from './field.js';
import { HREF } from './jobs.js';
import { svg } from './icons.js';
import { historyPairs, NAMES } from './sections-meta.js';
import { el, words } from './ui.js';
import { modelLabel } from './models.js';

export const SEARCH_PAUSE_MS = 300;
const CODE_PAUSE_MS = 800;     // «Агенты»: каждый запрос — поиск на компьютерах, реже
const LIMIT = 30;
/** Фильтр разделов: разделы с историей в Студии в порядке рельса, затем «Агенты» (история на компьютерах — своим
 * запросом, не индексом Студии; «Open WebUI» — на его сервере: поиск его не видит). */
const SECTIONS = [['', tr("Все")], ...historyPairs(), ['code', NAMES.code]];
const AGENT_STATES = {
  offline: tr("Компьютер не на связи"),
  closed: tr("Компьютер закрыт — откройте его PIN в «Агентах»"),
  unsupported: tr("Обновите мост на компьютере до 0.47.0"),
  timeout: tr("Компьютер не ответил вовремя"),
  busy: tr("На компьютере уже идёт поиск — повторите позже"),
  error: tr("Поиск на компьютере не удался"),
};
/** Разделы, где заголовок — тот же текст, что найден: кусок с выделением встаёт на его место. */
const SAME_TEXT = new Set(['photo', 'video', 'speech']);
const PERIODS = [[1, tr("Сегодня")], [7, tr("Неделя")], [30, tr("Месяц")]];
const HAS_WORD = /[\p{L}\p{N}]/u;
/** Строка диалога ведёт к своему сообщению: в адресе — только id (`?m=`), не слова поиска. */
const MESSAGE_HREF = /^chat\/[\w-]+\?m=[\w-]+$/;

const memory = { q: '', section: '', model: '', since: '', until: '' };
const codeFound = { q: '', groups: null };   // последний ответ «Агентов»: возврат из чата не ищет заново
let returnTo = null;
let screen = null;
document.addEventListener('studio-locked', () => { returnTo = null; codeFound.groups = null; });
// срок частичной защиты истёк (lock.js): выдача с кусками закрытых разделов уходит, тот же запрос — заново
document.addEventListener('studio-sections-locked', () => {
  returnTo = null;
  codeFound.groups = null;
  if (screen) screen.forget();
});

function icon(section) {
  const box = el('span', { class: 'search-ico', 'aria-hidden': 'true' });
  box.innerHTML = svg(section);   // константы icons.js
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

function preview(item) {
  if (!item.thumbnail_id) return icon(item.section);
  const img = el('img', { class: 'search-thumb', src: `/api/media/${encodeURIComponent(item.thumbnail_id)}?thumb=1`,
    alt: '', loading: 'lazy', decoding: 'async' });
  const box = el('span', { class: 'search-preview' }, img, icon(item.section));
  img.addEventListener('error', () => box.replaceWith(icon(item.section)), { once: true });
  return box;
}

function row(item, go) {
  const same = SAME_TEXT.has(item.section) && item.snippet;
  const title = el('b', { class: 'search-title' }, ...(same ? marked(item.snippet) : [item.title]));
  // найдено в документе диалога — его имя своей строкой, отдельно от куска текста
  const doc = item.attachment ? el('span', { class: 'search-doc', title: item.attachment }, `📄 ${item.attachment}`) : null;
  const snippet = !same && item.snippet ? el('span', { class: 'search-snippet' }, ...marked(item.snippet)) : null;
  const meta = el('span', { class: 'search-meta' }, [item.model && modelLabel(item.model), when(item.created)]
    .filter(Boolean).join(' · '));
  const node = el('button', { type: 'button', class: 'search-row', 'data-section': item.section, 'data-id': item.id,
    'data-part': item.part },
    preview(item), el('span', { class: 'sr' }, NAMES[item.section]),
    el('span', { class: 'search-text' }, title, doc, snippet, meta));
  node.addEventListener('click', () => { if (HREF.test(item.href) || MESSAGE_HREF.test(item.href)) go(item, node); });
  return el('li', {}, node);
}

/** Строка «Агентов»: название чата, кусок с найденным (текст как есть), проект и помощник; нажатие — в чат. */
function codeRow(aid, item, go) {
  const snippet = item.snippets[0]?.text;
  const node = el('button', { type: 'button', class: 'search-row', 'data-section': 'code', 'data-id': item.chat },
    icon('code'), el('span', { class: 'sr' }, NAMES.code),
    el('span', { class: 'search-text' }, el('b', { class: 'search-title' }, item.title || tr("Без названия")),
      snippet ? el('span', { class: 'search-snippet' }, snippet) : null,
      el('span', { class: 'search-meta' }, [item.project_name, ENGINE_NAMES[item.engine]].filter(Boolean).join(' · '))));
  node.addEventListener('click', () => {
    if (/^[0-9a-f]{16}$/.test(item.project)) go(`code/${aid}/${item.project}/${encodeURIComponent(item.chat)}`);
  });
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
    enterkeyhint: 'search', placeholder: tr("Слова промпта, сообщения или записи"), 'aria-label': tr("Что искать") });
  q.value = memory.q;
  const field = fieldBox(q);
  const chips = el('div', { class: 'search-chips', role: 'group', 'aria-label': tr("Раздел") },
    ...SECTIONS.map(([value, label]) => el('button', { type: 'button', class: 'chip', 'data-section': value,
      'aria-pressed': String(memory.section === value) }, label)));
  const model = el('select', { id: 'search-model', class: 'chip', 'aria-label': tr("Модель") });
  const since = el('input', { id: 'search-since', class: 'input search-date', type: 'date', 'aria-label': tr("С какого дня") });
  const until = el('input', { id: 'search-until', class: 'input search-date', type: 'date', 'aria-label': tr("По какой день") });
  since.value = memory.since;
  until.value = memory.until;
  const periods = el('div', { class: 'search-periods', role: 'group', 'aria-label': tr("Период") },
    ...PERIODS.map(([days, label]) => el('button', { type: 'button', class: 'chip',
      'data-days': days, 'aria-pressed': 'false' }, label)));
  const reset = el('button', { type: 'button', id: 'search-reset', class: 'btn-link' }, tr("сбросить"));
  const status = el('p', { id: 'search-status', class: 'muted small', 'aria-live': 'polite' });
  const results = el('div', { id: 'search-results', 'data-state': 'idle' });
  const moreBtn = el('button', { type: 'button', id: 'search-more', class: 'btn', hidden: true }, tr("Показать ещё"));

  const filters = el('div', { class: 'search-filters' }, model, el('label', { for: since.id }, tr("с")), since,
    el('label', { for: until.id }, tr("по")), until, reset);
  view.append(el('header', { class: 'head' }, el('h1', {}, NAMES.search)),
    el('div', { class: 'page search-page' },
      field.box, field.error, chips, filters, periods,
      status, results, moreBtn));
  let stopped = false;
  let codeBusy = false;        // поиск «Агентов» в пути: следующий — после него (мост держит один поиск)
  let codeAgain = false;

  function paintCode(groups) {
    const total = groups.reduce((n, g) => n + g.items.length, 0);
    const go = (href) => ctx.navigate(href);
    results.replaceChildren(...groups.map((g) => el('section', { class: 'search-day search-agent', 'data-aid': g.aid },
      el('h2', { class: 'feed-day-title' }, g.name),
      g.state !== 'ok' ? el('p', { class: 'muted small search-agent-state' }, AGENT_STATES[g.state] || AGENT_STATES.error)
        : g.items.length ? el('ul', { class: 'search-list' }, ...g.items.map((item) => codeRow(g.aid, item, go)))
          : el('p', { class: 'muted small search-agent-state' }, tr("Ничего не нашлось")),
      g.state === 'ok' && !g.complete ? el('p', { class: 'muted small' }, tr("Просмотрены не все чаты — уточните запрос")) : null)));
    status.textContent = !groups.length ? tr("Компьютеров пока нет.") : !total ? tr("Ничего не нашлось") : tr("Найдено: {0}", total);
    results.dataset.state = 'ready';
  }

  async function runCode() {
    const query = memory.q.trim();
    items = [];
    shown = '';
    more = false;
    moreBtn.hidden = true;
    if (!HAS_WORD.test(query)) {
      results.replaceChildren();
      status.textContent = tr("Введите слова — поиск идёт по чатам компьютеров на связи");
      results.dataset.state = 'idle';
      return;
    }
    if (codeFound.groups && codeFound.q === query) {
      paintCode(codeFound.groups);
      return;
    }
    if (codeBusy) {
      codeAgain = true;
      return;
    }
    codeBusy = true;
    const mine = asked;
    results.dataset.state = 'loading';
    status.textContent = tr("Ищу в чатах компьютеров…");
    try {
      const out = await api('POST', '/api/code/search', { query });
      Object.assign(codeFound, { q: query, groups: out.items });
      if (mine === asked && !stopped) paintCode(out.items);
    } catch (err) {
      if (mine === asked && !stopped) {
        status.textContent = words(err);
        results.dataset.state = 'error';
      }
    } finally {
      codeBusy = false;
      if (codeAgain && !stopped) {
        codeAgain = false;
        if (memory.section === 'code') run();
      }
    }
  }

  function paintModels() {
    const known = models || {};
    let names = memory.section ? known[memory.section] || [] : [...new Set(Object.values(known).flat())].sort();
    if (memory.model && !names.includes(memory.model)) {
      if (models) memory.model = '';
      else names = [memory.model];
    }
    model.replaceChildren(el('option', { value: '' }, tr("Все модели")), ...names.map((n) => el('option', { value: n }, modelLabel(n))));
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
      days[days.length - 1].list.append(row(item, openResult));
    }
    results.replaceChildren(...days.map((d) => el('section', { class: 'search-day' }, d.title, d.list)));
    moreBtn.hidden = !more;
  }

  function wanted() {
    const body = { limit: LIMIT };
    for (const key of ['q', 'section', 'model', 'since', 'until']) if (memory[key]) body[key] = memory[key];
    return body;
  }

  function paintPeriods() {
    for (const b of periods.children) {
      const range = calendarRange(Number(b.dataset.days));
      b.setAttribute('aria-pressed', String(range.since === memory.since && range.until === memory.until));
    }
  }

  function setPeriod(from, to) {
    since.value = memory.since = from;
    until.value = memory.until = to;
    paintPeriods();
    run();
  }

  function openResult(item, node) {
    if (shown === JSON.stringify(wanted())) {
      returnTo = { key: shown, section: item.section, id: String(item.id), part: node.dataset.part,
        count: items.length, offset: node.getBoundingClientRect().top - view.getBoundingClientRect().top,
        scroll: view.scrollTop };
    }
    ctx.navigate(item.href);
  }

  async function run(append = false, restore = null) {
    clearTimeout(timer);
    filters.hidden = periods.hidden = memory.section === 'code';
    if (memory.section === 'code') {
      asked += 1;
      returnTo = null;
      runCode();
      return;
    }
    const mine = ++asked;
    const body = wanted();
    const key = JSON.stringify(body);
    if (returnTo?.key !== key) returnTo = null;
    // новый запрос ещё ждал паузы после ввода — «Показать ещё» начинает его заново, а не дописывает
    // его страницу (курсором прежнего) к прежнему списку
    if (key !== shown) append = false;
    if (!HAS_WORD.test(memory.q) && !memory.section && !memory.model && !memory.since && !memory.until) {
      items = [];
      shown = '';
      more = false;
      paint();
      status.textContent = tr("Введите слова или выберите раздел, модель или период");
      results.dataset.state = 'idle';
      return;
    }
    results.dataset.state = 'loading';
    moreBtn.disabled = true;
    try {
      const next = append ? [...items] : [];
      let out;
      do {
        const last = next.at(-1);
        const before = last ? { created: last.created, item: last.id, part: last.part } : undefined;
        out = await api('POST', '/api/search', { ...body, before });
        if (mine !== asked) return;
        next.push(...out.items);
      } while (restore && returnTo === restore && out.more && out.items.length && next.length < restore.count);
      items = next;
      shown = key;
      more = out.more;
      paint();
      if (restore && returnTo === restore) {
        const node = [...results.querySelectorAll('.search-row')].find(({ dataset }) =>
          dataset.section === restore.section && dataset.id === restore.id && dataset.part === restore.part);
        view.scrollTop = node
          ? view.scrollTop + node.getBoundingClientRect().top - view.getBoundingClientRect().top - restore.offset
          : restore.scroll;
        node?.focus({ preventScroll: true });
      }
      // число — и диктору (aria-live), а не только пустая выдача
      status.textContent = !items.length ? tr("Ничего не нашлось")
        : more ? tr("Найдено: {0}, есть ещё", items.length) : tr("Найдено: {0}", items.length);
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
    timer = setTimeout(run, memory.section === 'code' ? CODE_PAUSE_MS : SEARCH_PAUSE_MS);
  }

  q.addEventListener('input', () => {
    memory.q = q.value;
    if (returnTo?.key !== JSON.stringify(wanted())) returnTo = null;
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
      paintPeriods();
      run();
    });
  }
  periods.addEventListener('click', (event) => {
    const chip = event.target.closest('button[data-days]');
    if (!chip) return;
    const range = calendarRange(Number(chip.dataset.days));
    setPeriod(range.since, range.until);
  });
  reset.addEventListener('click', () => setPeriod('', ''));
  moreBtn.addEventListener('click', () => run(true));

  paintModels();
  paintPeriods();
  api('GET', '/api/search/models').then((out) => {
    if (!screen || screen.results !== results) return;
    models = out.models;
    paintModels();
  }, () => {});
  const restore = returnTo?.key === JSON.stringify(wanted()) ? returnTo : null;
  run(false, restore);
  if (!restore && matchMedia('(hover: hover)').matches) q.focus();
  return {
    results,
    stop() { clearTimeout(timer); asked += 1; stopped = true; },
    forget() {
      items = [];
      shown = '';
      more = false;
      paint();
      run();
    },
  };
}

export function mount(view, ctx) {
  screen = mountScreen(view, ctx);
}

export function unmount() {
  if (screen) screen.stop();
  screen = null;
}
