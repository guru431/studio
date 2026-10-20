/* Раздел «Фото» (#/photo): лента картинок по дням, поле промпта снизу с чипами (исходное фото
 * и сила, модель, размер, seed), «Скачать» на превью (save.js), просмотр с действиями, правка кистью
 * (editor.js) и «Описать».
 *
 * Задание: POST /api/images (JSON; с файлом исходника — форма), запись ленты — GET /api/images/<id>.
 * Идущее задание (working, cancelling) ленту опрашивает раз в POLL_MS — и найденное в ленте после
 * перезагрузки страницы тоже. Одно задание за раз у пользователя: пока идёт своё, «Создать» и
 * действия просмотра, начинающие задание («Изменить», «Повторить», «Описать»), закрыты.
 * «Повторить» правки кистью идёт с её маской (mask_media_id).
 * Описание — POST /api/images/descriptions и опрос GET …/descriptions/<id> (текст выдаётся один раз);
 * окно закрыли раньше — описание отменяется. Текст сервера — только textContent. */
import { api } from './api.js';
import { chainList } from './chain.js';
import { openEditor } from './editor.js';
import { createFeed } from './feed.js';
import { saveMedia } from './save.js';
import { button, el, field, openDialog, words } from './ui.js';

const POLL_MS = 1500;
const PAGE = 30;
const PROMPT_MAX = 24000;
const EXTRA_MAX = 8000;
const PHOTO_MAX = 10 * 1024 * 1024;
const SEED_MAX = 4294967295;
const ACCEPT = 'image/png,image/jpeg,image/webp';
const MODELS = [['lustify', 'Lustify'], ['flux', 'FLUX']];
const SIZES = [['portrait', 'Портрет 832×1216', '832x1216'], ['square', 'Квадрат 1024×1024', '1024x1024'],
  ['landscape', 'Альбом 1216×832', '1216x832']];
const NAMES = { lustify: 'Lustify', flux: 'FLUX', 'gemma-abl': 'gemma-abl' };
const RUNNING = new Set(['working', 'cancelling']);
const ICONS = {
  clip: '<path d="M20 11.5l-8.2 8.2a5 5 0 01-7.1-7.1l8.6-8.6a3.4 3.4 0 014.8 4.8l-8.4 8.4a1.8 1.8 0 01-2.6-2.6l7.6-7.6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  save: '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
  x:'<path d="M6 6l12 12M18 6L6 18"/>',
};

let screen = null;

function icon(name, cls = 'ico') {
  const box = document.createElement('span');
  box.innerHTML = `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;   // константы выше
  return box.firstChild;
}

function iconButton(name, label, attrs = {}) {
  return el('button', { type: 'button', class: 'icon-btn', 'aria-label': label, title: label, ...attrs }, icon(name));
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
  let model = 'lustify';
  let size = 'portrait';
  let source = null;             // { file, url } — исходное фото с устройства
  let submitting = false;
  let describing = false;
  const running = new Map();     // id → { item, at, timer } — идущие задания ленты
  const gated = new Set();       // кнопки открытого просмотра, начинающие задание
  const popups = [];             // { chip, menu } — открывающиеся меню
  let alive = true;

  /* разметка */
  const status = el('p', { id: 'photo-status', class: 'status photo-status', 'aria-live': 'polite' });
  const describeFile = el('input', { type: 'file', id: 'describe-file', class: 'sr', accept: ACCEPT });
  const menuItems = [el('label', { class: 'menu-item', id: 'photo-describe-pick', role: 'menuitem' },
    'Описать фото с устройства…', describeFile)];
  if (ctx.me.is_admin) menuItems.push(button('Освободить карту', { class: 'menu-item', id: 'photo-release', role: 'menuitem' }, release));
  const menu = el('div', { id: 'photo-menu', class: 'photo-menu', role: 'menu', hidden: true }, ...menuItems);
  const toggle = iconButton('more', 'Ещё', { id: 'photo-menu-toggle', 'aria-haspopup': 'true', 'aria-expanded': 'false',
    'aria-controls': 'photo-menu' });
  const head = el('header', { class: 'head' }, el('h1', {}, 'Фото'), el('span', { class: 'grow' }),
    el('div', { class: 'photo-menu-wrap' }, toggle, menu));
  const feedBox = el('div', { id: 'photo-feed', class: 'photo-feed' });

  const sourceImg = el('img', { alt: 'Исходное фото' });
  const strength = el('input', { type: 'range', id: 'photo-strength', min: '0.05', max: '1', step: '0.05', value: '0.25' });
  const strengthValue = el('output', { for: 'photo-strength' }, '0.25');
  const strengthLabel = el('label', { id: 'photo-strength-label', class: 'strength' }, 'Сила изменения', strength, strengthValue);
  const sourceClear = iconButton('x', 'Убрать фото', { id: 'photo-source-clear' });
  const sourceBox = el('div', { id: 'photo-source', class: 'photo-source', hidden: true }, sourceImg, strengthLabel, sourceClear);
  const prompt = el('textarea', { id: 'photo-prompt', rows: '1', maxlength: String(PROMPT_MAX), 'aria-label': 'Промпт',
    placeholder: 'Опишите картинку: кто, где, свет, ракурс, стиль' });
  const file = el('input', { type: 'file', id: 'photo-file', class: 'sr', accept: ACCEPT });
  const chipPhoto = el('label', { class: 'chip', id: 'chip-photo', title: 'Исходное фото' }, icon('clip'),
    el('span', {}, 'фото'), file);
  const chipModel = chipMenu('chip-model', MODELS, () => model, (value) => { model = value; paintSource(); });
  const chipSize = chipMenu('chip-size', SIZES, () => size, (value) => { size = value; });
  const seed = el('input', { id: 'photo-seed', inputmode: 'numeric', maxlength: '10', placeholder: 'случайный',
    'aria-label': 'Seed, пусто — случайный', autocomplete: 'off' });
  const chipSeed = el('label', { class: 'chip chip-seed', id: 'chip-seed' }, el('span', {}, 'seed'), seed);
  const go = el('button', { type: 'submit', id: 'photo-go', class: 'btn btn-accent photo-go' }, 'Создать');
  const error = el('p', { id: 'photo-error', class: 'error photo-error', 'aria-live': 'polite' });
  const form = el('form', { class: 'composer', id: 'photo-composer' }, sourceBox, prompt,
    el('div', { class: 'row' }, chipPhoto, chipModel.wrap, chipSize.wrap, chipSeed, el('span', { class: 'grow' }), go));
  const root = el('div', { class: 'photo' }, head, status, feedBox, error, form);
  view.append(root);

  const feed = createFeed(feedBox, {
    key: (item) => item.id,
    render: card,
    load: async (before) => (await api('GET', `/api/images?limit=${PAGE}${before === undefined ? '' : `&before=${before}`}`)).items,
    empty: 'Здесь появятся ваши картинки. Опишите картинку внизу и нажмите «Создать».',
  });

  /* меню чипов и раздела */
  function chipMenu(id, options, get, set) {
    const name = el('span', { class: 'chip-name' });
    const chip = el('button', { type: 'button', class: 'chip', id, 'aria-haspopup': 'true', 'aria-expanded': 'false',
      'aria-controls': `${id}-menu` }, name, icon('down', 'ico caret'));
    const list = el('div', { class: 'chip-menu', id: `${id}-menu`, role: 'menu', hidden: true });
    const paint = () => {
      name.textContent = (options.find((o) => o[0] === get()) || options[0])[1];
      for (const b of list.children) b.setAttribute('aria-checked', String(b.dataset.value === get()));
    };
    for (const [value, label] of options) {
      const option = el('button', { type: 'button', class: 'chip-option', role: 'menuitemradio', 'data-value': value }, label);
      option.addEventListener('click', () => {
        set(value);
        paint();
        popup(chip, list, false);
        chip.focus();
      });
      list.append(option);
    }
    chip.addEventListener('click', () => popup(chip, list, list.hidden));
    popups.push({ chip, menu: list });
    paint();
    return { wrap: el('div', { class: 'chip-wrap' }, chip, list) };
  }

  function popup(chip, list, open) {
    for (const p of popups) {
      p.menu.hidden = p.menu !== list || !open;
      p.chip.setAttribute('aria-expanded', String(!p.menu.hidden));
    }
  }
  popups.push({ chip: toggle, menu });
  toggle.addEventListener('click', () => popup(toggle, menu, menu.hidden));

  function say(text, bad) {
    status.textContent = text;
    status.className = `status photo-status${bad ? ' error' : ''}`;
  }

  /* исходное фото */
  function setSource(f) {
    if (source) URL.revokeObjectURL(source.url);
    source = f ? { file: f, url: URL.createObjectURL(f) } : null;
    if (source) sourceImg.src = source.url;
    else sourceImg.removeAttribute('src');
    chipPhoto.classList.toggle('on', !!source);
    paintSource();
  }

  function paintSource() {
    sourceBox.hidden = !source;
    strengthLabel.hidden = model !== 'lustify';
  }

  /* занятость: одно задание за раз */
  function syncBusy() {
    const busy = submitting || describing || running.size > 0;
    for (const b of [go, ...gated]) {
      b.disabled = busy;
      b.title = busy ? 'Дождитесь конца текущего задания' : '';
    }
    describeFile.disabled = busy;                    // «Описать фото с устройства…» меню раздела
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
    syncBusy();
  }

  function forget(id) {
    const entry = running.get(id);
    if (entry) clearTimeout(entry.timer);
    running.delete(id);
    syncBusy();
  }

  /* Одна цепочка опросов на задание: позвали, пока опрос в пути (отмена), — его ответ устарел,
   * он выбрасывается, и тот же опрос сразу спрашивает заново. */
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
      item = await api('GET', `/api/images/${id}`);
    } catch (err) {
      if (alive && err.code === 'gone') {
        forget(id);
        feed.remove(id);
        return;
      }
    } finally {
      entry.inflight = false;
    }
    if (!alive) return;
    if (item && !entry.again) {
      feed.update(item);
      watch(item);
    }
    if (running.get(id) === entry) entry.timer = setTimeout(() => poll(id), entry.again ? 0 : POLL_MS);
  }

  function busyText(entry) {
    const item = entry.item;
    const spent = clock(item.elapsed + (Date.now() - entry.at) / 1000);
    return `${item.state === 'cancelling' ? 'Отменяю' : 'Генерирую'} · ${spent}`;
  }

  function card(item) {
    watch(item);
    const node = el('div', { class: 'card photo-card', 'data-id': item.id, 'data-state': item.state });
    if (item.state === 'done' && item.media_id) {
      const open = el('button', { type: 'button', class: 'card-open', 'aria-label': 'Открыть картинку' },
        el('img', { src: media(item.media_id, '?thumb=1'), alt: '', loading: 'lazy', decoding: 'async' }),
        el('span', { class: 'card-cap' }, NAMES[item.model] || item.model));
      open.addEventListener('click', () => openViewer(item));
      // рядом с .card-open, а не внутри: кнопка в кнопке недопустима
      const save = el('button', { type: 'button', class: 'card-save', 'aria-label': 'Скачать', title: 'Скачать' }, icon('save'));
      save.addEventListener('click', (event) => {
        event.stopPropagation();
        saveMedia(item.media_id, root);
      });
      node.append(open, save);
    } else if (RUNNING.has(item.state)) {
      const box = el('div', { class: 'card-busy' }, el('span', { class: 'ring', 'aria-hidden': 'true' }),
        el('b', { class: 'card-time' }, busyText(running.get(item.id))));
      if (item.state === 'working') {
        box.append(button('Отменить', { class: 'btn-link card-cancel' }, async (event) => {
          event.currentTarget.disabled = true;
          try {
            await api('POST', `/api/images/${item.id}/cancel`);
          } catch (err) {
            say(words(err), true);
          }
          poll(item.id);
        }));
      } else box.append(el('p', { class: 'small' }, cap(item.detail)));
      node.append(box);
    } else {
      const text = item.state === 'cancelled' ? cap(item.detail) || 'Отменено' : `Ошибка: ${item.detail || 'картинка не получена'}`;
      node.append(el('div', { class: `card-note${item.state === 'error' ? ' error' : ''}` }, el('p', {}, text),
        button('Удалить', { class: 'btn-link card-delete' }, () => remove(item).catch((err) => say(words(err), true)))));
    }
    return node;
  }

  const tick = setInterval(() => {
    for (const [id, entry] of running) {
      const time = feedBox.querySelector(`.photo-card[data-id="${id}"] .card-time`);
      if (time) time.textContent = busyText(entry);
    }
  }, 1000);

  /* задание */
  async function adopt(id) {
    try {
      feed.prepend(await api('GET', `/api/images/${id}`));
    } catch {
      feed.reload();
    }
  }

  async function create(body) {
    if (submitting) return false;
    submitting = true;
    error.textContent = '';
    syncBusy();
    try {
      await adopt((await api('POST', '/api/images', body)).id);
      return true;
    } catch (err) {
      error.textContent = words(err);
      if (err.code === 'busy') feed.reload();         // задание с другой вкладки или устройства
      return false;
    } finally {
      submitting = false;
      syncBusy();
    }
  }

  /** Тело задания из поля и чипов; withSource — с исходным фото, если оно выбрано. */
  function compose(text, withSource) {
    const seedText = seed.value.trim();
    if (seedText !== '' && (!/^\d+$/.test(seedText) || Number(seedText) > SEED_MAX)) {
      throw new Error(`Seed — целое число от 0 до ${SEED_MAX}; пусто — случайный`);
    }
    if (!withSource || !source) return { prompt: text, size, model, seed: seedText === '' ? null : Number(seedText) };
    if (source.file.size > PHOTO_MAX) throw new Error('Фото больше 10 МиБ');
    const body = new FormData();
    for (const [k, v] of Object.entries({ prompt: text, size, model, seed: seedText })) {
      if (v !== '') body.append(k, v);
    }
    if (model === 'lustify') body.append('strength', strength.value);
    body.append('image', source.file, source.file.name);
    return body;
  }

  function submit(withSource = true) {
    const text = prompt.value.trim();
    if (!text) {
      error.textContent = 'Напишите промпт';
      return;
    }
    let body;
    try {
      body = compose(text, withSource);
    } catch (err) {
      error.textContent = words(err);
      return;
    }
    create(body);
  }

  function repeatBody(item) {
    const known = SIZES.find((s) => s[2] === item.size);
    const body = { prompt: item.prompt, model: item.model, size: known ? known[0] : 'original', seed: item.seed };
    if (item.source_media_id) {
      body.source_media_id = item.source_media_id;
      if (item.strength !== null && item.strength !== undefined) body.strength = item.strength;
      if (item.mask_media_id) body.mask_media_id = item.mask_media_id;      // правка кистью — с той же маской
    }
    return body;
  }

  async function remove(item, dialogs = []) {
    try {
      await api('DELETE', `/api/images/${item.id}`);
    } catch (err) {
      if (err.code !== 'gone') throw err;
    }
    for (const d of dialogs) d.close();
    forget(item.id);
    feed.remove(item.id);
  }

  /* просмотр */
  function openViewer(item) {
    const img = el('img', { src: media(item.media_id), alt: 'Картинка' });
    const stage = el('div', { class: 'viewer-stage' }, img);
    const close = iconButton('x', 'Закрыть', { class: 'icon-btn viewer-close' });
    const act = (text, cls, fn) => button(text, { class: `btn btn-sm ${cls}` }, fn);
    const dialog = el('dialog', { id: 'viewer', class: 'viewer', 'aria-label': 'Просмотр картинки' });
    const starts = [
      act('Изменить', 'act-edit', () => {
        dialog.close();
        openEditor(item, model, (created) => adopt(created.id));
      }),
      act('Повторить с тем же seed', 'act-repeat', () => {
        dialog.close();
        create(repeatBody(item));
      }),
      act('Описать', 'act-describe', () => {
        dialog.close();
        describe({ mediaId: item.media_id });
      })];
    const actions = el('div', { class: 'viewer-actions' }, starts[0],
      act('Оживить', 'act-animate', () => {
        dialog.close();
        ctx.navigate(`video?source=${encodeURIComponent(item.media_id)}`);
      }),
      starts[1], starts[2],
      act('Скачать', 'act-download', () => saveMedia(item.media_id, root)),
      act('Удалить', 'btn-danger act-delete', () => confirmDelete(item, dialog)));
    for (const b of starts) gated.add(b);
    syncBusy();
    const missing = el('p', { class: 'error small', 'aria-live': 'polite' });
    const onOpen = async (id) => {
      try {
        const step = await api('GET', `/api/images/${encodeURIComponent(id)}`);
        dialog.close();
        openViewer(step);
      } catch (err) {
        missing.textContent = err.code === 'gone' ? 'Картинка удалена' : words(err);
      }
    };
    dialog.append(el('div', { class: 'viewer-top' }, close), stage,
      el('div', { class: 'viewer-bar' }, chainList(item.chain, { current: item.id, onOpen }), missing, actions));
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      for (const b of starts) gated.delete(b);
      dialog.remove();
    });
    // 1:1 — точка под пальцем или курсором остаётся на месте; клик мимо картинки — закрыть
    img.addEventListener('click', (event) => {
      const fx = event.offsetX / img.clientWidth;
      const fy = event.offsetY / img.clientHeight;
      if (!dialog.classList.toggle('full')) return;
      stage.scrollLeft = fx * img.offsetWidth - event.clientX;
      stage.scrollTop = fy * img.offsetHeight - event.clientY;
    });
    stage.addEventListener('click', (event) => { if (event.target === stage) dialog.close(); });
    root.append(dialog);
    dialog.showModal();
  }

  function confirmDelete(item, viewer) {
    const fail = el('p', { class: 'error' });
    const confirm = button('Удалить', { class: 'btn btn-danger solid', id: 'photo-delete-confirm' });
    const dialog = openDialog(root, { id: 'photo-delete-dialog', title: 'Удалить картинку?',
      body: [el('p', {}, 'Картинка удалится из истории вместе с файлом.'), fail], actions: [confirm] });
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

  /* «Описать»: gemma-abl, текст можно править, «Создать по описанию» — без исходного фото */
  function describe(from) {
    const preview = el('img', { id: 'describe-photo', alt: 'Фото для описания' });
    const url = from.file ? URL.createObjectURL(from.file) : null;
    preview.src = url || media(from.mediaId, '?thumb=1');
    const wait = el('p', { id: 'describe-status', class: 'muted small', 'aria-live': 'polite' }, 'gemma-abl описывает фото · 0:00');
    const text = el('textarea', { id: 'describe-text', class: 'input', rows: '10', maxlength: String(PROMPT_MAX), disabled: true });
    const extra = el('textarea', { id: 'describe-extra', class: 'input', rows: '2', maxlength: String(EXTRA_MAX),
      placeholder: 'Например: другое освещение, цвет одежды или дополнительные детали' });
    const counter = el('p', { id: 'describe-counter', class: 'muted small' });
    const fail = el('p', { id: 'describe-error', class: 'error', role: 'alert' });
    const make = button('Создать по описанию', { class: 'btn btn-accent', id: 'describe-create', disabled: true });
    const dialog = openDialog(root, { id: 'describe-dialog', title: 'Описание картинки', actions: [make], body: [preview,
      el('p', { class: 'muted small' }, 'gemma-abl описывает фото на карте студии. Описание на английском подготовлено как промпт для генерации; его можно править.'),
      wait, field('Подробное описание', text, 'text'), field('Ваш дополнительный текст', extra, 'extra'), counter, fail] });
    const started = Date.now();
    let jobId = null;
    let finished = false;
    let open = true;
    let timer = null;
    const clockTick = setInterval(() => { wait.textContent = `gemma-abl описывает фото · ${clock((Date.now() - started) / 1000)}`; }, 1000);
    const combined = () => {
      const more = extra.value.trim();
      return text.value.trim() + (more ? `\n\nAdditional instructions:\n${more}` : '');
    };
    const count = () => {
      counter.textContent = `${combined().length} / ${PROMPT_MAX} знаков. Генерация пойдёт по тексту, без исходного фото.`;
    };
    const stop = () => {
      finished = true;
      describing = false;
      clearInterval(clockTick);
      wait.hidden = true;
      syncBusy();
    };
    text.addEventListener('input', count);
    extra.addEventListener('input', count);
    dialog.addEventListener('close', () => {
      open = false;
      clearTimeout(timer);
      if (url) URL.revokeObjectURL(url);
      if (jobId && !finished) api('POST', `/api/images/${jobId}/cancel`).catch(() => {});
      if (!finished) stop();
    });
    make.addEventListener('click', () => {
      const prompted = combined();
      if (!text.value.trim() || prompted.length > PROMPT_MAX) {
        fail.textContent = `Нужно непустое описание; вместе с дополнением — не больше ${PROMPT_MAX} знаков.`;
        return;
      }
      prompt.value = prompted;
      autosize();
      dialog.close();
      submit(false);
    });

    async function poll() {
      if (!open || !alive) return;
      try {
        const r = await api('GET', `/api/images/descriptions/${jobId}`);
        if (!open) return;
        if (r.state === 'done') {
          stop();
          text.value = r.text;
          text.disabled = false;
          make.disabled = false;
          if (r.truncated) fail.textContent = 'Достигнут лимит ответа модели; описание можно дополнить вручную.';
          count();
          text.focus();
          return;
        }
        if (r.state === 'error' || r.state === 'cancelled') {
          stop();
          fail.textContent = r.state === 'error' ? `Ошибка: ${r.detail}` : cap(r.detail) || 'Отменено';
          return;
        }
      } catch (err) {
        if (!open) return;
        if (err.code === 'gone') {
          stop();
          fail.textContent = words(err);
          return;
        }
      }
      timer = setTimeout(poll, POLL_MS);
    }

    const body = new FormData();
    if (from.file) body.append('image', from.file, from.file.name);
    else body.append('source_media_id', from.mediaId);
    describing = true;
    syncBusy();
    api('POST', '/api/images/descriptions', body).then((out) => {
      jobId = out.id;
      if (!open) {
        api('POST', `/api/images/${jobId}/cancel`).catch(() => {});
        return;
      }
      timer = setTimeout(poll, POLL_MS);
    }, (err) => {
      stop();
      if (open) fail.textContent = words(err);
    });
  }

  /* освобождение карты (администратор) */
  async function release() {
    popup(toggle, menu, false);
    say('', false);
    try {
      const { results } = await api('POST', '/api/images/release');
      const names = Object.keys(results);
      say(names.map((k) => `${NAMES[k] || k} — ${results[k].message}`).join('; '),
        names.some((k) => results[k].state === 'error'));
    } catch (err) {
      say(words(err), true);
    }
  }

  function autosize() {
    prompt.style.height = 'auto';
    prompt.style.height = `${prompt.scrollHeight}px`;
  }

  /* события */
  const onDocClick = (event) => {
    for (const p of popups) {
      if (!p.menu.hidden && !p.menu.contains(event.target) && !p.chip.contains(event.target)) popup(p.chip, p.menu, false);
    }
  };
  const onKey = (event) => {
    if (event.key !== 'Escape') return;
    const shown = popups.find((p) => !p.menu.hidden);
    if (shown) {
      popup(shown.chip, shown.menu, false);
      shown.chip.focus();
    }
  };
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKey);

  file.addEventListener('change', () => setSource(file.files[0] || null));
  sourceClear.addEventListener('click', () => {
    file.value = '';
    setSource(null);
  });
  strength.addEventListener('input', () => { strengthValue.textContent = Number(strength.value).toFixed(2); });
  describeFile.addEventListener('change', () => {
    const f = describeFile.files[0];
    describeFile.value = '';
    popup(toggle, menu, false);
    if (f) describe({ file: f });
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
    if (!go.disabled) submit(true);
  });
  syncBusy();

  return {
    destroy() {
      alive = false;
      document.removeEventListener('click', onDocClick);
      document.removeEventListener('keydown', onKey);
      clearInterval(tick);
      for (const entry of running.values()) clearTimeout(entry.timer);
      running.clear();
      if (source) URL.revokeObjectURL(source.url);
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
