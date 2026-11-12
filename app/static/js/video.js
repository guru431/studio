/* Раздел «Видео» (#/video): лента роликов по дням в одном из видов (views.js), поле промпта снизу
 * (field.js: «очистить», микрофон) с фото-источником над ним и строкой чипов под ним (модель — если в
 * разделе их больше одной, длительность, качество, «плавно») и «Оживить» справа; seed выбирает студия
 * (поля нет, спека 2026-10-27-studio-ui-tidy, 4.5). Превью — карточка с ходом ролика и плеером, у ролика из фото истории —
 * «Промпты» (цепочка, chain.js); плитка и таблица — постер и ход, готовый ролик открывается в
 * просмотре (плеер, цепочка, «Скачать», «Удалить»). Цепочку записи ленты (в ней — только число
 * шагов) догружает GET /api/videos/<id>.
 *
 * Ролик: POST /api/videos (форма: файл `image` с устройства или `source_media_id` — фото, пришедшее
 * из раздела «Фото» адресом #/video?photo=<image_id>, с описанием исходного фото как контекстом;
 * прежний #/video?source=<media_id> тоже принимается). Ведёт ролик наблюдатель службы; идущие
 * ролики ленты (queued, in_progress) страница опрашивает GET /api/videos/<id> раз в POLL_MS — и
 * найденные после перезагрузки тоже. Роликов может идти несколько: очередь — у шлюза.
 * Раздел без модели (ctx.hint): лента и просмотр — как всегда, вместо поля промпта — подсказка.
 * #/video?id=<id> («Открыть» в «В работе», jobs.js): готовый — просмотр, иначе — его карточка; в уже
 * открытом разделе — update, без пересборки (промпт и фото-источник целы); созданный ролик зовёт
 * jobs.kick(). «Повторить» и «Ещё варианты» (2–4) у законченного ролика — POST /api/videos/<id>/repeat:
 * тот же кадр, промпт и параметры, новый seed. Текст сервера — только textContent. */
import { api } from './api.js';
import { icon } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { chainList } from './chain.js';
import { createFeed, pageQuery } from './feed.js';
import { fieldBox } from './field.js';
import { kick } from './jobs.js';
import { saveMedia } from './save.js';
import { createSelection } from './select.js';
import { modelLabel } from './settings.js';
import { button, el, openDialog, words } from './ui.js';
import { tableHead, timeOf, viewSwitch } from './views.js';

const POLL_MS = 3000;
const PAGE = 30;
const PROMPT_MAX = 4000;
const PHOTO_MAX = 20 * 1024 * 1024;
const ACCEPT = 'image/png,image/jpeg,image/webp';
const QUALITIES = [['fast', 'Быстро 480p'], ['standard', 'Обычно 576p'], ['high', 'Высоко 720p']];
const RESOLUTION = { fast: '480p', standard: '576p', high: '720p' };
const SOURCES = { given: 'свой промпт', auto: 'придуман по фото', fallback: 'запасной промпт' };
const RUNNING = new Set(['queued', 'in_progress']);
const MOTION_HINT = 'Опишите движение в кадре; пусто — движение придумает gemma-abl';

let screen = null;

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
  let loadingSource = false;
  let sourceVersion = 0;         // смена фото и уход из раздела делают ответ переноса устаревшим
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
  const pick = el('label', { class: 'icon-btn attach', id: 'video-pick', title: 'Фото для ролика — выбрать с устройства' }, icon('clip'),
    el('span', { class: 'sr' }, 'Фото для ролика — выбрать с устройства'), file);
  const sourceImg = el('img', { alt: 'Фото для ролика' });
  const sourceName = el('span', { class: 'video-source-name' });
  const sourceClear = el('button', { type: 'button', class: 'icon-btn', id: 'video-source-clear', 'aria-label': 'Убрать фото',
    title: 'Убрать фото' }, icon('x'));
  const chosen = el('div', { class: 'video-chosen', hidden: true }, sourceImg, sourceName, sourceClear);
  const sourceBox = el('div', { id: 'video-source', class: 'video-source', hidden: true }, chosen);
  const photoPrompt = el('textarea', { id: 'video-photo-prompt', class: 'input', rows: '3',
    'aria-label': 'Описание исходного фото', maxlength: '24000' });
  const photoContext = el('details', { id: 'video-photo-context', hidden: true },
    el('summary', { class: 'small muted' }, 'Описание исходного фото'),
    el('p', { class: 'small muted' }, 'Передаётся вместе с описанием движения. Можно изменить или очистить.'), photoPrompt);
  const prompt = el('textarea', { id: 'video-prompt', rows: '1', maxlength: String(PROMPT_MAX), 'aria-label': 'Промпт движения',
    placeholder: MOTION_HINT });
  const seconds = el('input', { id: 'video-seconds', inputmode: 'numeric', maxlength: '2', value: '5',
    'aria-label': 'Длительность, секунд: от 1 до 30', autocomplete: 'off' });
  const chipSeconds = el('label', { class: 'chip chip-num', id: 'chip-seconds', title: 'Длительность: 1–30 с' }, seconds,
    el('span', {}, 'с'));
  const qualityName = el('span', { class: 'chip-name' });
  const chipQuality = el('button', { type: 'button', class: 'chip', id: 'chip-quality' }, qualityName, icon('down', 'ico caret'));
  const qualityMenu = el('div', { class: 'chip-menu', id: 'chip-quality-menu', role: 'menu', 'aria-label': 'Качество' },
    ...QUALITIES.map(([value, label]) => menuItem(label, { radio: true, attrs: { 'data-value': value }, onSelect: () => {
      quality = value;
      paintQuality();
      qualityPopup.close(true);
    } })));
  const qualityPopup = popupMenu(chipQuality, qualityMenu, { side: 'above' });
  const chipSmooth = el('button', { type: 'button', class: 'chip', id: 'chip-smooth', 'aria-pressed': 'true',
    title: 'Плавно: дорисовка кадров до 32 к/с; выключено — 16 к/с' }, 'плавно');
  // модель — из отмеченных раздела «Видео»; выбор показывается, только если вариантов больше одного
  const modelPick = el('select', { class: 'chip', id: 'video-model', 'aria-label': 'Модель', hidden: true });
  const go = el('button', { type: 'submit', id: 'video-go', class: 'btn btn-accent video-go composer-go' }, 'Оживить');
  const error = el('p', { id: 'video-error', class: 'error video-error', 'aria-live': 'polite' });
  const promptBox = fieldBox(prompt, { attach: pick });
  const form = el('form', { class: 'composer', id: 'video-composer' }, sourceBox, photoContext, promptBox.box, promptBox.error,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, modelPick, chipSeconds,
      el('div', { class: 'chip-wrap' }, chipQuality, qualityMenu), chipSmooth), go));
  const root = el('div', { class: 'video' }, head, feedBox, error, ctx.hint ? ctx.hint('hint-bar') : form,
    selection.bar);
  view.append(root);

  let focusId = null;            // запись из «В работе», которую ещё ждёт лента
  const feed = createFeed(feedBox, {
    key: (item) => item.id,
    render: card,
    quiet: ['elapsed', 'progress', 'detail', 'chain'],      // ход, слова и время — таймер tick; цепочка — не в карточке
    load: async (last) => (await api('GET', `/api/videos?limit=${PAGE}${pageQuery(last)}`)).items,
    empty: ctx.hint ? 'Здесь появятся ваши ролики.' : 'Здесь появятся ваши ролики. Выберите фото внизу и нажмите «Оживить».',
    head: tableHead(['', 'Промпт', 'Ролик', 'Seed', 'Время', '']),
    onChange: () => {
      selection.sync();
      focusCard();
    },
    dayTools: selection.dayTools,
  });
  selection.attach({ root, head, feedBox, feed, error });

  /* переход к записи из «В работе» (#/video?id=…): готовый — просмотр, иначе — его карточка в ленте;
   * ?id из адреса убирается: повторное «Открыть» снова сменит адрес */
  function focusCard() {
    const node = focusId && feedBox.querySelector(`.video-card[data-id="${CSS.escape(focusId)}"]`);
    if (!node) return;
    focusId = null;
    for (const old of feedBox.querySelectorAll('.jobs-focus')) old.classList.remove('jobs-focus');
    node.classList.add('jobs-focus');
    node.scrollIntoView({ block: 'center' });
  }

  /** ?id адреса — при сборке и при переходе внутри раздела (update): прочее состояние не трогает. */
  function follow(wanted) {
    if (!wanted) return;
    history.replaceState(history.state, '', '#/video');
    api('GET', `/api/videos/${encodeURIComponent(wanted)}`).then((item) => {
      if (!alive) return;
      if (item.state === 'done' && item.media_id) {
        const shown = root.querySelector('#video-viewer');
        if (shown) shown.close();                       // просмотр один: прежний уступает
        openViewer(item);
      } else {
        focusId = item.id;
        focusCard();
      }
    }, (err) => { if (alive) error.textContent = words(err); });
  }
  follow(ctx.query.get('id'));

  /* чип качества: в чипе — «576p», полные слова — в меню, в title и aria-label */
  function paintQuality() {
    qualityName.textContent = RESOLUTION[quality];
    const named = `Качество: ${QUALITIES.find(([value]) => value === quality)[1]}`;   // «576p» без слова — непонятно, что это
    chipQuality.setAttribute('aria-label', named);
    chipQuality.title = named;
    for (const b of qualityMenu.children) b.setAttribute('aria-checked', String(b.dataset.value === quality));
  }

  /* фото-источник */
  function setSource(next) {
    sourceVersion += 1;
    loadingSource = false;
    go.disabled = submitting;
    go.textContent = 'Оживить';
    error.textContent = '';
    if (source && source.file) URL.revokeObjectURL(source.url);
    source = next;
    photoPrompt.value = source?.photoPrompt || '';
    photoContext.hidden = !photoPrompt.value;
    photoContext.open = false;
    paintPromptHint();
    pick.classList.toggle('on', !!source);
    sourceBox.hidden = !source;
    chosen.hidden = !source;
    if (source) {
      sourceImg.src = source.url;
      sourceName.textContent = source.file ? 'Фото с устройства' : 'Фото из ленты';
    } else sourceImg.removeAttribute('src');
  }

  function paintPromptHint() {
    prompt.placeholder = photoPrompt.value.trim() ? 'Опишите движение в кадре; описание фото уже добавлено' : MOTION_HINT;
  }

  /** Перенос читает только свою запись через обычный API; тексты остаются в памяти страницы,
   * URL содержит лишь id. Ошибка и смена источника не оставляют прежнее фото для отправки. */
  async function transfer(query) {
    const photoId = query.get('photo');
    const mediaId = query.get('source');
    if (!photoId && !mediaId) return;
    history.replaceState(history.state, '', '#/video');
    setSource(null);
    if (!photoId) {
      setSource({ mediaId, url: media(mediaId, '?thumb=1') });
      return;
    }
    loadingSource = true;
    go.disabled = true;
    go.textContent = 'Перенос фото…';
    const version = sourceVersion;
    try {
      const item = await api('GET', `/api/images/${encodeURIComponent(photoId)}`);
      if (!alive || version !== sourceVersion) return;
      if (item.state !== 'done' || !item.media_id) throw new Error('Фото ещё не готово для видео');
      setSource({ mediaId: item.media_id, url: media(item.media_id, '?thumb=1'), photoPrompt: item.prompt });
    } catch (err) {
      if (alive && version === sourceVersion) error.textContent = err.code === 'gone' ? 'Фото удалено или недоступно' : words(err);
    } finally {
      if (alive && version === sourceVersion) {
        loadingSource = false;
        go.disabled = submitting;
        go.textContent = 'Оживить';
      }
    }
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

  /* Одна цепочка опросов на ролик (как в photo.js): ответ опроса, ушедшего до «Отменить» или удаления,
   * устарел — он выбрасывается; запись сменилась (отмена кончила ролик) — цепочка не продолжается. */
  async function poll(id) {
    const entry = running.get(id);
    if (!entry || !alive) return;
    clearTimeout(entry.timer);
    if (entry.inflight) {
      entry.again = true;
      return;
    }
    entry.inflight = true;
    entry.again = false;
    let item = null;
    try {
      item = await api('GET', `/api/videos/${id}`);
    } catch (err) {
      if (alive && err.code === 'gone' && running.get(id) === entry) {
        forget(id);
        feed.remove(id);
        return;
      }
    } finally {
      entry.inflight = false;
    }
    if (!alive || running.get(id) !== entry) return;
    if (item && !entry.again) {
      feed.update(item);
      watch(item);
    }
    if (running.get(id) === entry) entry.timer = setTimeout(() => poll(id), entry.again ? 0 : POLL_MS);
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

  /* «Повторить» и «Ещё варианты»: тот же кадр, промпт, длительность и качество, seed — новый у каждого
   * (POST /api/videos/<id>/repeat). Новые ролики — в начало ленты; отказ после первого — словами. */
  async function repeat(item, count, control, after = () => {}) {
    control.disabled = true;
    error.textContent = '';
    try {
      const out = await api('POST', `/api/videos/${encodeURIComponent(item.id)}/repeat`, { count });
      kick();
      if (!alive) return;
      for (const made of out.items) feed.prepend(made);
      if (out.message) error.textContent = `Сделано ${out.items.length} из ${count}: ${out.message}`;
      after();
    } catch (err) {
      if (alive) error.textContent = words(err);
      throw err;
    } finally {
      control.disabled = false;
    }
  }

  function repeatButton(item, cls, after) {
    return button('Повторить', { class: `${cls} card-repeat`, title: 'Тот же кадр, промпт и параметры, новый seed' },
      (event) => repeat(item, 1, event.currentTarget, after).catch(() => {}));
  }

  function variantsButton(item, cls, after) {
    return button('Ещё варианты', { class: `${cls} card-variants` }, () => chooseVariants(item, after));
  }

  function chooseVariants(item, after = () => {}) {
    const count = el('select', { id: 'video-variants-count', 'aria-label': 'Сколько вариантов' },
      ...[2, 3, 4].map((n) => el('option', { value: String(n) }, String(n))));
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const go = button('Сделать', { class: 'btn btn-accent', id: 'video-variants-go' });
    const dialog = openDialog(root, { id: 'video-variants-dialog', title: 'Ещё варианты',
      body: [el('p', {}, 'Тот же кадр, промпт, длительность и качество; seed у каждого ролика — новый.'),
        el('label', {}, 'Роликов: ', count), fail], actions: [go] });
    go.addEventListener('click', () => {
      fail.textContent = '';
      repeat(item, Number(count.value), go, () => {
        dialog.close();
        after();
      }).catch((err) => { fail.textContent = words(err); });
    });
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
        repeatButton(item, 'btn btn-sm'), variantsButton(item, 'btn btn-sm'),
        button('Удалить', { class: 'btn btn-sm btn-danger card-delete' }, () => confirmDelete(item)));
    } else if (RUNNING.has(item.state)) {
      acts.append(cancelButton(item, 'btn btn-sm'));
      node.append(el('div', { class: 'video-stage' }, still(item), overlay(item)));
    } else {
      acts.append(repeatButton(item, 'btn btn-sm'), deleteButton(item, 'btn btn-sm btn-danger'));
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
    if (RUNNING.has(item.state)) over.append(cancelButton(item, 'btn-link'));
    else over.append(repeatButton(item, 'btn-link'), deleteButton(item, 'btn-link'));
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
    el('span', { class: 'cell-act' }, ...(done ? [save] : busy ? [cancelButton(item, 'btn-link')]
      : [repeatButton(item, 'btn-link'), deleteButton(item, 'btn-link')])));
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
          repeatButton(item, 'btn btn-sm act-repeat', () => dialog.close()),
          variantsButton(item, 'btn btn-sm act-variants', () => dialog.close()),
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

  // ход, слова и время идущего — на месте: опрос карточку с ними не пересоздаёт (quiet ленты)
  const tick = setInterval(() => {
    for (const [id, entry] of running) {
      const card = feedBox.querySelector(`.video-card[data-id="${id}"]`);
      if (!card) continue;
      const { item } = entry;
      const time = card.querySelector('.card-time');
      if (time) {
        time.textContent = busyText(entry);
        if (time.closest('.cell-when')) time.title = cap(item.detail);           // вид «Таблица»
      }
      const state = card.querySelector('.video-state');
      if (state) state.textContent = cap(item.detail) || 'В очереди';
      const bar = card.querySelector('.video-bar');
      if (bar) {
        bar.setAttribute('aria-valuenow', String(item.progress));
        bar.firstElementChild.style.width = `${item.progress}%`;
      }
    }
  }, 1000);

  async function cancel(item, control) {
    control.disabled = true;
    error.textContent = '';
    try {
      const out = await api('POST', `/api/videos/${item.id}/cancel`);
      if (!alive) return;
      const entry = running.get(item.id);
      if (entry && entry.inflight) entry.again = true;      // опрос в пути ушёл до отмены — его ответ устарел
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
    if (source.file && source.file.size > PHOTO_MAX) throw new Error('Фото больше 20 МиБ');
    const body = new FormData();
    if (source.file) body.append('image', source.file, source.file.name);
    else body.append('source_media_id', source.mediaId);
    const context = photoPrompt.value.trim();
    const motion = prompt.value.trim();
    const text = context ? `Описание исходного фото:\n${context}\n\nДвижение в видео:\n${motion || 'Естественное движение в кадре.'}` : motion;
    if (text.length > PROMPT_MAX) throw new Error('Описание фото и движение вместе — до 4000 знаков. Сократите описание исходного фото.');
    body.append('prompt', text);
    body.append('seconds', String(Number(sec)));
    body.append('quality', quality);
    body.append('smooth', String(smooth));
    if (!modelPick.hidden && modelPick.value) {
      const [connId, ...rest] = modelPick.value.split(':');
      body.append('connection_id', connId);
      body.append('model', rest.join(':'));
    }
    return body;
  }

  async function submit() {
    if (submitting || loadingSource) return;
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
      kick();
      if (alive) feed.prepend(out);
    } catch (err) {
      error.textContent = words(err);
    } finally {
      submitting = false;
      go.disabled = loadingSource;
    }
  }

  function autosize() {
    prompt.style.height = 'auto';
    // Пустое поле сохраняет три строки из общих стилей, непустое растёт по содержимому.
    if (prompt.value) prompt.style.height = `${prompt.scrollHeight}px`;
  }

  /* события (меню качества закрывает себя само: menu.js) */
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
  photoPrompt.addEventListener('input', paintPromptHint);
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
  setSource(null);
  transfer(ctx.query);

  return {
    follow,
    transfer,
    destroy() {
      alive = false;
      sourceVersion += 1;
      selection.destroy();
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

/** Переход внутри открытого раздела (#/video?id=…): только ?id — промпт, фото-источник и выделение целы. */
export function update(ctx) {
  if (screen) {
    screen.follow(ctx.query.get('id'));
    screen.transfer(ctx.query);
  }
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
