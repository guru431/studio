/* Раздел «Видео» (#/video): лента роликов по дням в одном из видов (views.js), поле промпта снизу
 * с фото-источником и чипами (модель — если в разделе их больше одной, длительность, качество,
 * плавность, seed). Превью — карточка с ходом ролика и плеером, у ролика из фото истории —
 * «Промпты» (цепочка, chain.js); плитка и таблица — постер и ход, готовый ролик открывается в
 * просмотре (плеер, цепочка, «Скачать», «Удалить»). Цепочку записи ленты (в ней — только число
 * шагов) догружает GET /api/videos/<id>.
 *
 * Ролик: POST /api/videos (форма: файл `image` с устройства или `source_media_id` — фото, пришедшее
 * из раздела «Фото» адресом #/video?source=<media_id>). Ведёт ролик наблюдатель службы; идущие
 * ролики ленты (queued, in_progress) страница опрашивает GET /api/videos/<id> раз в POLL_MS — и
 * найденные после перезагрузки тоже. Роликов может идти несколько: очередь — у шлюза.
 * Раздел без модели (ctx.hint): лента и просмотр — как всегда, вместо поля промпта — подсказка.
 * Текст сервера — только textContent. */
import { api } from './api.js';
import { chainList } from './chain.js';
import { createFeed, pageQuery } from './feed.js';
import { attachMic } from './mic.js';
import { saveMedia } from './save.js';
import { createSelection } from './select.js';
import { modelLabel } from './settings.js';
import { button, el, openDialog, words } from './ui.js';
import { tableHead, timeOf, viewSwitch } from './views.js';

const POLL_MS = 3000;
const PAGE = 30;
const PROMPT_MAX = 4000;
const PHOTO_MAX = 20 * 1024 * 1024;
const SEED_MAX = 4294967295;
const ACCEPT = 'image/png,image/jpeg,image/webp';
const QUALITIES = [['fast', 'Быстро 480p'], ['standard', 'Обычно 576p'], ['high', 'Высоко 720p']];
const RESOLUTION = { fast: '480p', standard: '576p', high: '720p' };
const SOURCES = { given: 'свой промпт', auto: 'придуман по фото', fallback: 'запасной промпт' };
const RUNNING = new Set(['queued', 'in_progress']);
const ICONS = {
  clip: '<path d="M20 11.5l-8.2 8.2a5 5 0 01-7.1-7.1l8.6-8.6a3.4 3.4 0 014.8 4.8l-8.4 8.4a1.8 1.8 0 01-2.6-2.6l7.6-7.6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  save: '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
};

let screen = null;

function icon(name, cls = 'ico') {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
  return box.firstChild;
}

function clock(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function cap(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
}

function media(id, query = '') {
  return `/api/media/${encodeURIComponent(id)}${query}`;
}

function build(view, ctx) {
  let quality = 'standard';
  let smooth = true;
  let source = null;             // { file, url } с устройства или { mediaId, url } из раздела «Фото»
  let submitting = false;
  const running = new Map();     // id → { item, at, timer } — идущие ролики ленты
  let alive = true;

  /* разметка */
  const feedBox = el('div', { id: 'video-feed', class: 'video-feed' });
  const selection = createSelection({ section: 'video', nouns: ['ролик', 'ролика', 'роликов'],
    selectable: (item) => !RUNNING.has(item.state), mediaOf: (item) => (item.state === 'done' && item.media_id) || null,
    remove: (item) => remove(item) });
  const layout = viewSwitch('video', 'preview', (name) => {
    feedBox.dataset.view = name;
    feed.redraw();
  });
  feedBox.dataset.view = layout.value;
  const head = el('header', { class: 'head' }, el('h1', {}, 'Видео'), selection.tools, el('span', { class: 'grow' }),
    selection.button, layout.node);

  const file = el('input', { type: 'file', id: 'video-file', class: 'sr', accept: ACCEPT });
  const pick = el('label', { class: 'video-pick', id: 'video-pick' }, icon('clip'),
    el('span', {}, 'Фото для ролика — выбрать с устройства'), file);
  const sourceImg = el('img', { alt: 'Фото для ролика' });
  const sourceName = el('span', { class: 'video-source-name' });
  const sourceClear = el('button', { type: 'button', class: 'icon-btn', id: 'video-source-clear', 'aria-label': 'Убрать фото',
    title: 'Убрать фото' }, icon('x'));
  const chosen = el('div', { class: 'video-chosen', hidden: true }, sourceImg, sourceName, sourceClear);
  const sourceBox = el('div', { id: 'video-source', class: 'video-source' }, pick, chosen);
  const prompt = el('textarea', { id: 'video-prompt', rows: '1', maxlength: String(PROMPT_MAX), 'aria-label': 'Промпт движения',
    placeholder: 'Опишите движение в кадре; пусто — движение придумает gemma-abl' });
  const seconds = el('input', { id: 'video-seconds', inputmode: 'numeric', maxlength: '2', value: '5',
    'aria-label': 'Длительность, секунд: от 1 до 30', autocomplete: 'off' });
  const chipSeconds = el('label', { class: 'chip chip-num', id: 'chip-seconds', title: 'Длительность: 1–30 с' }, seconds,
    el('span', {}, 'с'));
  const qualityName = el('span', { class: 'chip-name' });
  const chipQuality = el('button', { type: 'button', class: 'chip', id: 'chip-quality', 'aria-haspopup': 'true',
    'aria-expanded': 'false', 'aria-controls': 'chip-quality-menu' }, qualityName, icon('down', 'ico caret'));
  const qualityMenu = el('div', { class: 'chip-menu', id: 'chip-quality-menu', role: 'menu', hidden: true },
    ...QUALITIES.map(([value, label]) => el('button', { type: 'button', class: 'chip-option', role: 'menuitemradio',
      'data-value': value }, label)));
  const chipSmooth = el('button', { type: 'button', class: 'chip', id: 'chip-smooth', 'aria-pressed': 'true',
    title: 'Дорисовка кадров до 32 к/с; выключена — 16 к/с' }, 'плавность 32 к/с');
  const seed = el('input', { id: 'video-seed', inputmode: 'numeric', maxlength: '10', placeholder: 'случайный',
    'aria-label': 'Seed, пусто — случайный', autocomplete: 'off' });
  const chipSeed = el('label', { class: 'chip chip-seed', id: 'chip-vseed' }, el('span', {}, 'seed'), seed);
  // модель — из отмеченных раздела «Видео»; выбор показывается, только если вариантов больше одного
  const modelPick = el('select', { class: 'chip', id: 'video-model', 'aria-label': 'Модель', hidden: true });
  const go = el('button', { type: 'submit', id: 'video-go', class: 'btn btn-accent video-go' }, 'Оживить');
  const error = el('p', { id: 'video-error', class: 'error video-error', 'aria-live': 'polite' });
  const micNote = el('div', { class: 'mic-note' });
  const form = el('form', { class: 'composer', id: 'video-composer' }, sourceBox, prompt, micNote,
    el('div', { class: 'row' }, modelPick, chipSeconds, el('div', { class: 'chip-wrap' }, chipQuality, qualityMenu), chipSmooth, chipSeed,
      el('span', { class: 'grow' }), attachMic(prompt, micNote), go));
  const root = el('div', { class: 'video' }, head, feedBox, error, ctx.hint ? ctx.hint('hint-bar') : form,
    selection.bar);
  view.append(root);

  const feed = createFeed(feedBox, {
    key: (item) => item.id,
    render: card,
    load: async (last) => (await api('GET', `/api/videos?limit=${PAGE}${pageQuery(last)}`)).items,
    empty: ctx.hint ? 'Здесь появятся ваши ролики.' : 'Здесь появятся ваши ролики. Выберите фото внизу и нажмите «Оживить».',
    head: tableHead(['', 'Промпт', 'Ролик', 'Seed', 'Время', '']),
    onChange: () => selection.sync(),
    dayTools: selection.dayTools,
  });
  selection.attach({ root, head, feedBox, feed, error });

  /* чип качества */
  function paintQuality() {
    qualityName.textContent = QUALITIES.find((q) => q[0] === quality)[1];
    for (const b of qualityMenu.children) b.setAttribute('aria-checked', String(b.dataset.value === quality));
  }

  function openQuality(open) {
    qualityMenu.hidden = !open;
    chipQuality.setAttribute('aria-expanded', String(open));
  }

  /* фото-источник */
  function setSource(next) {
    if (source && source.file) URL.revokeObjectURL(source.url);
    source = next;
    pick.hidden = !!source;
    chosen.hidden = !source;
    if (source) {
      sourceImg.src = source.url;
      sourceName.textContent = source.file ? 'Фото с устройства' : 'Фото из ленты';
    } else sourceImg.removeAttribute('src');
  }

  /* карточки ленты */
  function watch(item) {
    const entry = running.get(item.id);
    if (RUNNING.has(item.state)) {
      if (entry) {
        entry.item = item;
        entry.at = Date.now();
      } else running.set(item.id, { item, at: Date.now(), timer: setTimeout(() => poll(item.id), POLL_MS) });
    } else if (entry) {
      clearTimeout(entry.timer);
      running.delete(item.id);
    }
  }

  function forget(id) {
    const entry = running.get(id);
    if (entry) clearTimeout(entry.timer);
    running.delete(id);
  }

  async function poll(id) {
    if (!running.has(id) || !alive) return;
    try {
      const item = await api('GET', `/api/videos/${id}`);
      if (!alive) return;
      feed.update(item);
      watch(item);
    } catch (err) {
      if (!alive) return;
      if (err.code === 'gone') {
        forget(id);
        feed.remove(id);
        return;
      }
    }
    const next = running.get(id);
    if (next) next.timer = setTimeout(() => poll(id), POLL_MS);
  }

  function busyText(entry) {
    const item = entry.item;
    const spent = clock(item.elapsed + (Date.now() - entry.at) / 1000);
    return item.state === 'in_progress' ? `${item.progress}% · ${spent}` : spent;
  }

  function clipParams(item) {
    return [`${item.seconds} с`, RESOLUTION[item.quality] || item.quality, item.smooth ? '32 к/с' : '16 к/с'].join(' · ');
  }

  function seedText(item) {
    return item.seed !== null && item.seed !== undefined ? `seed ${item.seed}` : '';
  }

  function metaText(item) {
    return [clipParams(item), SOURCES[item.prompt_source], seedText(item)].filter(Boolean).join(' · ');
  }

  function posterOf(item) {
    return item.source_media_id ? media(item.source_media_id, '?thumb=1') : null;
  }

  function still(item) {
    const src = posterOf(item);
    return src ? el('img', { src, alt: '', loading: 'lazy', decoding: 'async' }) : null;
  }

  function failText(item) {
    return item.state === 'failed' ? cap(item.detail) || 'Ролик не получился' : cap(item.detail) || 'Отменено';
  }

  /** Ход идущего ролика или итог неудачного — поверх постера. */
  function overlay(item) {
    const over = el('div', { class: 'video-over' });
    if (RUNNING.has(item.state)) {
      const bar = el('span');
      bar.style.width = `${item.progress}%`;
      over.append(el('span', { class: 'ring', 'aria-hidden': 'true' }),
        el('p', { class: 'video-state' }, cap(item.detail) || 'В очереди'),
        el('div', { class: 'video-bar', role: 'progressbar', 'aria-label': 'Ход ролика', 'aria-valuemin': '0',
          'aria-valuemax': '100', 'aria-valuenow': String(item.progress) }, bar),
        el('b', { class: 'card-time' }, busyText(running.get(item.id))));
    } else over.append(el('p', { class: item.state === 'failed' ? 'video-fail' : '' }, failText(item)));
    return over;
  }

  /** Готовый ролик плитки и таблицы: постер со значком, нажатие — просмотр. */
  function openButton(item, ...inside) {
    const open = el('button', { type: 'button', class: 'video-open', 'aria-label': 'Открыть ролик' }, still(item),
      el('span', { class: 'video-play', 'aria-hidden': 'true' }, icon('play')), ...inside);
    open.addEventListener('click', () => openViewer(item));
    return open;
  }

  function cancelButton(item, cls) {
    return button('Отменить', { class: `${cls} card-cancel` }, (event) => cancel(item, event.currentTarget));
  }

  function deleteButton(item, cls) {
    return button('Удалить', { class: `${cls} card-delete` }, () => remove(item).catch((err) => { error.textContent = words(err); }));
  }

  /** «Промпты»: цепочки в записи ленты нет — она из GET /api/videos/<id>. */
  function chainButton(item) {
    return button('Промпты', { class: 'btn btn-sm card-chain' }, async (event) => {
      const b = event.currentTarget;
      b.disabled = true;
      try {
        const full = await api('GET', `/api/videos/${encodeURIComponent(item.id)}`);
        if (alive) openDialog(root, { id: 'chain-dialog', title: 'Как получен ролик', cancelText: 'Закрыть',
          body: [chainList(full.chain, { current: null, onOpen: () => ctx.navigate('photo') })] });
      } catch (err) {
        error.textContent = words(err);
      } finally {
        b.disabled = false;
      }
    });
  }

  /** Вид «Превью»: плеер или ход ролика, под ним — итог, параметры, промпт и действия. */
  function previewCard(item, node) {
    const acts = el('div', { class: 'video-acts' });
    const body = el('div', { class: 'video-body' });
    if (item.state === 'done' && item.media_id) {
      node.append(el('video', { class: 'video-player', controls: true, playsinline: true, preload: 'metadata',
        poster: posterOf(item), src: media(item.media_id) }));
      body.append(el('p', { class: 'video-line' }, `${cap(item.detail) || 'Готово'} · ${clock(item.elapsed)}`));
      acts.append(button('Скачать', { class: 'btn btn-sm card-download' }, () => saveMedia(item.media_id, root)),
        button('Удалить', { class: 'btn btn-sm btn-danger card-delete' }, () => confirmDelete(item)));
    } else {
      acts.append(RUNNING.has(item.state) ? cancelButton(item, 'btn btn-sm') : deleteButton(item, 'btn btn-sm btn-danger'));
      node.append(el('div', { class: 'video-stage' }, still(item), overlay(item)));
    }
    body.append(el('p', { class: 'video-meta' }, metaText(item)));
    if (item.prompt) body.append(el('p', { class: 'video-prompt', title: item.prompt }, item.prompt));
    if (item.steps > 1) acts.prepend(chainButton(item));
    body.append(acts);
    node.append(body);
    return node;
  }

  /** Вид «Плитка»: только кадр; действия идущего и неудачного — ссылками поверх. */
  function tileCard(item, node) {
    if (item.state === 'done' && item.media_id) {
      node.append(openButton(item, el('span', { class: 'card-cap' }, `${item.seconds} с`)));
      return node;
    }
    const over = overlay(item);
    over.append(RUNNING.has(item.state) ? cancelButton(item, 'btn-link') : deleteButton(item, 'btn-link'));
    node.append(el('div', { class: 'video-stage' }, still(item), over));
    return node;
  }

  /** Вид «Таблица»: постер, промпт, параметры, seed, время или ход, действие. */
  function row(item, node) {
    const done = item.state === 'done' && item.media_id;
    const busy = RUNNING.has(item.state);
    const when = done ? el('span', {}, `${timeOf(item.created)} · ${clock(item.elapsed)}`)
      : busy ? el('b', { class: 'card-time', title: cap(item.detail) }, busyText(running.get(item.id)))
        : el('span', { class: item.state === 'failed' ? 'error' : 'muted' }, failText(item));
    const save = el('button', { type: 'button', class: 'icon-btn card-download', 'aria-label': 'Скачать', title: 'Скачать' },
      icon('save'));
    save.addEventListener('click', () => saveMedia(item.media_id, root));
    node.append(done ? openButton(item)
      : el('span', { class: 'row-thumb' }, still(item), busy ? el('span', { class: 'ring', 'aria-hidden': 'true' }) : null),
    el('p', { class: 'cell-prompt', title: item.prompt || '' }, item.prompt || SOURCES[item.prompt_source] || ''),
    el('span', { class: 'cell-meta' }, el('span', {}, clipParams(item)), el('span', {}, seedText(item)),
      el('span', { class: 'cell-when' }, when)),
    el('span', { class: 'cell-act' }, done ? save : busy ? cancelButton(item, 'btn-link') : deleteButton(item, 'btn-link')));
    return node;
  }

  function card(item) {
    watch(item);
    const node = el('article', { class: 'card video-card', 'data-id': item.id, 'data-state': item.state });
    if (layout.value === 'table') return selection.decorate(row(item, node), item);
    return selection.decorate(layout.value === 'tiles' ? tileCard(item, node) : previewCard(item, node), item);
  }

  /* просмотр ролика (плитка и таблица): плеер, параметры, цепочка, «Скачать», «Удалить» */
  function openViewer(item) {
    const close = el('button', { type: 'button', class: 'icon-btn viewer-close', 'aria-label': 'Закрыть', title: 'Закрыть' },
      icon('x'));
    const player = el('video', { class: 'viewer-video', controls: true, autoplay: true, playsinline: true, preload: 'metadata',
      poster: posterOf(item), src: media(item.media_id) });
    const dialog = el('dialog', { id: 'video-viewer', class: 'viewer', 'aria-label': 'Просмотр ролика' });
    const act = (text, cls, fn) => button(text, { class: `btn btn-sm ${cls}` }, fn);
    const missing = el('p', { class: 'error small', 'aria-live': 'polite' });
    let chain = el('ol', { class: 'viewer-chain' });
    dialog.append(el('div', { class: 'viewer-top' }, close), el('div', { class: 'viewer-stage' }, player),
      el('div', { class: 'viewer-bar' }, el('p', { class: 'viewer-meta' }, metaText(item)), chain, missing,
        el('div', { class: 'viewer-actions' }, act('Скачать', 'act-download', () => saveMedia(item.media_id, root)),
          act('Удалить', 'btn-danger act-delete', () => confirmDelete(item, dialog)))));
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      player.pause();
      dialog.remove();
    });
    api('GET', `/api/videos/${encodeURIComponent(item.id)}`).then((full) => {
      if (!dialog.open) return;
      const next = chainList(full.chain, { current: null, onOpen: () => {
        dialog.close();
        ctx.navigate('photo');
      } });
      chain.replaceWith(next);
      chain = next;
    }, (err) => { missing.textContent = err.code === 'gone' ? 'Ролик удалён' : words(err); });
    root.append(dialog);
    dialog.showModal();
  }

  api('GET', '/api/sections').then(({ sections }) => {
    const section = sections.find((s) => s.id === 'video');
    const options = section.links.flatMap(({ connection: c, models }) =>
      models.map((m) => [`${c.id}:${m}`, `${modelLabel(m)} · ${c.name}`]));
    modelPick.replaceChildren(...options.map(([value, label]) => el('option', { value }, label)));
    if (section.default) modelPick.value = `${section.default.connection_id}:${section.default.model}`;
    modelPick.hidden = options.length < 2;
  }, () => {});

  const tick = setInterval(() => {
    for (const [id, entry] of running) {
      const time = feedBox.querySelector(`.video-card[data-id="${id}"] .card-time`);
      if (time) time.textContent = busyText(entry);
    }
  }, 1000);

  async function cancel(item, control) {
    control.disabled = true;
    error.textContent = '';
    try {
      const out = await api('POST', `/api/videos/${item.id}/cancel`);
      if (!alive) return;
      feed.update(out);
      watch(out);
    } catch (err) {
      error.textContent = words(err);
      control.disabled = false;
    }
  }

  async function remove(item, dialogs = []) {
    try {
      await api('DELETE', `/api/videos/${item.id}`);
    } catch (err) {
      if (err.code !== 'gone') throw err;
    }
    for (const d of dialogs) if (d) d.close();
    forget(item.id);
    feed.remove(item.id);
  }

  function confirmDelete(item, viewer = null) {
    const fail = el('p', { class: 'error' });
    const confirm = button('Удалить', { class: 'btn btn-danger solid', id: 'video-delete-confirm' });
    const dialog = openDialog(root, { id: 'video-delete-dialog', title: 'Удалить ролик?',
      body: [el('p', {}, 'Ролик удалится из истории вместе с файлом.'), fail], actions: [confirm] });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await remove(item, [dialog, viewer]);
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
      }
    });
  }

  /* ролик */
  function compose() {
    if (!source) throw new Error('Выберите фото для ролика');
    const sec = seconds.value.trim();
    if (!/^\d+$/.test(sec) || Number(sec) < 1 || Number(sec) > 30) throw new Error('Длительность — целое число секунд от 1 до 30');
    const seedText = seed.value.trim();
    if (seedText !== '' && (!/^\d+$/.test(seedText) || Number(seedText) > SEED_MAX)) {
      throw new Error(`Seed — целое число от 0 до ${SEED_MAX}; пусто — случайный`);
    }
    if (source.file && source.file.size > PHOTO_MAX) throw new Error('Фото больше 20 МиБ');
    const body = new FormData();
    if (source.file) body.append('image', source.file, source.file.name);
    else body.append('source_media_id', source.mediaId);
    body.append('prompt', prompt.value.trim());
    body.append('seconds', String(Number(sec)));
    body.append('quality', quality);
    body.append('smooth', String(smooth));
    if (seedText !== '') body.append('seed', seedText);
    if (!modelPick.hidden && modelPick.value) {
      const [connId, ...rest] = modelPick.value.split(':');
      body.append('connection_id', connId);
      body.append('model', rest.join(':'));
    }
    return body;
  }

  async function submit() {
    if (submitting) return;
    error.textContent = '';
    let body;
    try {
      body = compose();
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    submitting = true;
    go.disabled = true;
    try {
      const out = await api('POST', '/api/videos', body);
      if (alive) feed.prepend(out);
    } catch (err) {
      error.textContent = words(err);
    } finally {
      submitting = false;
      go.disabled = false;
    }
  }

  function autosize() {
    prompt.style.height = 'auto';
    prompt.style.height = `${prompt.scrollHeight}px`;
  }

  /* события; нажатие вне меню — в фазе захвата: в режиме выделения лента глушит нажатие по карточке */
  const onDocClick = (event) => {
    if (!qualityMenu.hidden && !qualityMenu.contains(event.target) && !chipQuality.contains(event.target)) openQuality(false);
  };
  const onKey = (event) => {
    if (event.key !== 'Escape' || qualityMenu.hidden) return;
    openQuality(false);
    chipQuality.focus();
  };
  document.addEventListener('click', onDocClick, true);
  document.addEventListener('keydown', onKey);

  chipQuality.addEventListener('click', () => openQuality(qualityMenu.hidden));
  for (const option of qualityMenu.children) {
    option.addEventListener('click', () => {
      quality = option.dataset.value;
      paintQuality();
      openQuality(false);
      chipQuality.focus();
    });
  }
  chipSmooth.addEventListener('click', () => {
    smooth = !smooth;
    chipSmooth.setAttribute('aria-pressed', String(smooth));
  });
  file.addEventListener('change', () => {
    const f = file.files[0];
    if (f) setSource({ file: f, url: URL.createObjectURL(f) });
  });
  sourceClear.addEventListener('click', () => {
    file.value = '';
    setSource(null);
  });
  prompt.addEventListener('input', autosize);
  prompt.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit();
  });

  paintQuality();
  const fromPhoto = ctx.query && ctx.query.get('source');
  setSource(fromPhoto ? { mediaId: fromPhoto, url: media(fromPhoto, '?thumb=1') } : null);

  return {
    destroy() {
      alive = false;
      selection.destroy();
      document.removeEventListener('click', onDocClick, true);
      document.removeEventListener('keydown', onKey);
      clearInterval(tick);
      for (const entry of running.values()) clearTimeout(entry.timer);
      running.clear();
      if (source && source.file) URL.revokeObjectURL(source.url);
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
