/* Раздел «Фото» (#/photo): лента картинок по дням в одном из видов (превью, плитка, таблица —
 * views.js), поле промпта снизу (field.js: «очистить», «EN» — перевод промпта, микрофон) и строка чипов под
 * ним (исходное фото и сила, модель, размер, серия, пресеты) с «Создать» справа; seed студия выбирает сама
 * (поля нет, спека 2026-10-27-studio-ui-tidy, 4.5). «Скачать» на карточке (save.js), просмотр с действиями,
 * правка кистью (editor.js) и «Описать».
 *
 * Задание: POST /api/images (JSON; с файлом исходника — форма), запись ленты — GET /api/images/<id>
 * (у неё цепочка промптов; в ленте — только число шагов, цепочку просмотр догружает). Идущее задание
 * (working, cancelling) ленту опрашивает раз в POLL_MS — и найденное в ленте после перезагрузки
 * страницы тоже. Одно задание за раз у пользователя: пока идёт своё, «Создать», «EN» и действия
 * просмотра, начинающие задание («Изменить», «Повторить», «Описать»), закрыты.
 * «Повторить» правки кистью идёт с её маской (mask_media_id).
 * Описание — POST /api/images/descriptions и опрос GET …/descriptions/<id>: потерянный ответ опрос
 * повторяет, а текст, вставленный в поле, подтверждается DELETE …/descriptions/<id> — сервер его
 * стирает; окно закрыли раньше — описание отменяется. Перевод — так же через …/translations: ответ
 * заменяет промпт в поле, генерация не начинается; «EN» во время перевода его отменяет.
 * Текст сервера — только textContent.
 * Чип «модель» — отмеченные модели раздела «Фото» (GET /api/sections), по умолчанию — умолчание
 * раздела; задание несёт пару connection_id + model. Сила и кисть — только у Lustify / шлюза srvllm.
 * Раздел без модели (ctx.hint): лента, просмотр, «Скачать» и «Удалить» — как всегда, вместо поля
 * промпта — подсказка, в просмотре нет «Изменить» и «Повторить».
 * #/photo?id=<id> («Открыть» в «В работе», jobs.js): готовая — просмотр, иначе — её карточка; в уже
 * открытом разделе — update, без пересборки (промпт, исходник, серия, выделение целы); запуск
 * задания зовёт jobs.kick().
 *
 * Спека 2026-10-21-studio-photo-presets-compare-series: чип «★» — пресеты (presets.js); чип «×1» —
 * серия (series.js; ответ несёт ids — лента берёт все строки сразу, строка `queued` — «в очереди
 * серии» без таймера, с «Отменить»); сравнение двух картинок (compare.js) — «Сравнить с исходником»
 * в просмотре и «Сравнить» в режиме выделения при двух отмеченных; «Варианты» в просмотре —
 * GET /api/images/<id>/variants. Параметры по модели — chain.js: силы нет у FLUX, seed — у внешнего
 * сервиса («Повторить с тем же seed» — просто «Повторить»). */
import { api } from './api.js';
import { icon, iconButton } from './icons.js';
import { menuItem, popupMenu, popupPanel } from './menu.js';
import { chainList, isExternal, NAMES, paramParts, seedOf } from './chain.js';
import { openCompare } from './compare.js';
import { openEditor } from './editor.js';
import { createFeed, pageQuery } from './feed.js';
import { enterSends, fieldBox } from './field.js';
import { sectionDraft } from './drafts.js';
import { kick } from './jobs.js';
import { presetChip } from './presets.js';
import { saveMedia } from './save.js';
import { createSelection } from './select.js';
import { seriesControl } from './series.js';
import { modelLabel } from './settings.js';
import { button, el, field, openDialog, words } from './ui.js';
import { tableHead, timeOf, viewSwitch } from './views.js';

const POLL_MS = 1500;
const TRANSLATE_FAILS = 40;      // отказов опроса перевода подряд (~минута) — дальше перевод бросается
const THUMB_SIDE = 384;          // длинная сторона миниатюры (media.THUMB_SIDE)
const PAGE = 30;
const PROMPT_MAX = 24000;
const EXTRA_MAX = 8000;
const PHOTO_MAX = 10 * 1024 * 1024;
const ACCEPT = 'image/png,image/jpeg,image/webp';
const SIZES = [['portrait', 'Портрет 832×1216', '832x1216'], ['square', 'Квадрат 1024×1024', '1024x1024'],
  ['landscape', 'Альбом 1216×832', '1216x832']];
const RUNNING = new Set(['working', 'cancelling', 'queued']);
const CANCELLABLE = new Set(['working', 'queued']);

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
  let choice = null;             // { connection_id, model, kind } — модель из чипа; null — умолчание раздела
  let choices = [];              // варианты чипа: [ключ, подпись, выбор]
  let size = 'portrait';
  let source = null;             // { file, url } — исходное фото с устройства
  let submitting = false;
  let describing = false;
  let translating = null;        // { id, started, timer } — идущий перевод промпта
  let shutDescribe = null;       // уборка открытого окна «Описать»: отмена задания, часы, опрос, превью
  const running = new Map();     // id → { item, at, timer } — идущие задания ленты
  const gated = new Set();       // кнопки открытого просмотра, начинающие задание
  let alive = true;

  /* разметка */
  const status = el('p', { id: 'photo-status', class: 'status photo-status', 'aria-live': 'polite' });
  const describeFile = el('input', { type: 'file', id: 'describe-file', class: 'sr', accept: ACCEPT });
  // выбор файла — кнопкой меню: поле файла вне меню, закрытое меню его не прячет
  const menuItems = [menuItem('Описать фото с устройства…', { attrs: { id: 'photo-describe-pick' },
    onSelect: () => { menuPopup.close(true); describeFile.click(); } })];
  if (ctx.me.is_admin) menuItems.push(menuItem('Освободить карту', { attrs: { id: 'photo-release' }, onSelect: release }));
  const menu = el('div', { id: 'photo-menu', class: 'photo-menu', role: 'menu', 'aria-label': 'Ещё' }, ...menuItems);
  const toggle = iconButton('more', 'Ещё', { id: 'photo-menu-toggle' });
  const menuPopup = popupMenu(toggle, menu, { align: 'end' });
  const feedBox = el('div', { id: 'photo-feed', class: 'photo-feed' });
  const selection = createSelection({ section: 'photo', nouns: ['картинку', 'картинки', 'картинок'],
    selectable: (item) => !RUNNING.has(item.state), mediaOf: (item) => (item.state === 'done' && item.media_id) || null,
    remove: (item) => remove(item),
    // ровно две отмеченные с файлом — сравнение; режим выделения остаётся
    extraActions: [{ id: 'photo-select-compare', label: 'Сравнить',
      shown: (items) => items.length === 2 && items.every((i) => i.state === 'done' && i.media_id),
      run: ([a, b]) => openCompare(root, { media_id: a.media_id, label: paramParts(a).filter(Boolean).join(' · ') },
        { media_id: b.media_id, label: paramParts(b).filter(Boolean).join(' · ') }) }] });
  const layout = viewSwitch('photo', 'tiles', (name) => {
    feedBox.dataset.view = name;
    feed.redraw();
  });
  feedBox.dataset.view = layout.value;
  const head = el('header', { class: 'head' }, el('h1', {}, 'Фото'), selection.tools, el('span', { class: 'grow' }),
    selection.button, layout.node, el('div', { class: 'photo-menu-wrap' }, toggle, menu, describeFile));

  const sourceImg = el('img', { alt: 'Исходное фото' });
  const strength = el('input', { type: 'range', id: 'photo-strength', min: '0.05', max: '1', step: '0.05', value: '0.25' });
  const strengthValue = el('output', { for: 'photo-strength' }, '0.25');
  const strengthLabel = el('label', { id: 'photo-strength-label', class: 'strength' }, 'Сила изменения', strength, strengthValue);
  const sourceClear = iconButton('x', 'Убрать фото', { id: 'photo-source-clear' });
  const sourceBox = el('div', { id: 'photo-source', class: 'photo-source', hidden: true }, sourceImg, strengthLabel, sourceClear);
  const prompt = el('textarea', { id: 'photo-prompt', rows: '1', maxlength: String(PROMPT_MAX), 'aria-label': 'Промпт',
    placeholder: 'Опишите картинку: кто, где, свет, ракурс, стиль' });
  const file = el('input', { type: 'file', id: 'photo-file', class: 'sr', accept: ACCEPT });
  const chipPhoto = el('label', { class: 'icon-btn attach', id: 'chip-photo', title: 'Исходное фото' }, icon('clip'),
    el('span', { class: 'sr' }, 'Исходное фото'), file);
  const chipModel = chipMenu('chip-model', [], () => choiceKey(choice), (value) => {
    choice = (choices.find((c) => c[0] === value) || [])[2] || null;
    paintSource();
  });
  // в чипе — только имя формата («Портрет»), размеры — в меню
  const chipSize = chipMenu('chip-size', SIZES, () => size, (value) => { size = value; }, (label) => label.split(' ')[0]);
  const seriesName = el('span', { class: 'chip-name' }, '×1');
  const chipSeries = el('button', { type: 'button', class: 'chip', id: 'chip-series',
    title: 'Серия: несколько вариантов подряд' }, seriesName);
  const series = seriesControl({ prefix: 'photo-series', canStrength: () => isLustify() && !!source,
    canSeed: () => !choice || choice.kind === 'srvllm', from: () => strength.value, onChange: () => {
      seriesName.textContent = series ? series.label() : '×1';
      chipSeries.classList.toggle('on', !!(series && series.value()));
    } });
  // серия — форма (число, что меняется, ползунок): не меню, а панель-диалог у чипа (menu.js popupPanel)
  const seriesMenu = el('div', { class: 'series-menu', id: 'chip-series-menu' }, series.panel);
  popupPanel(chipSeries, seriesMenu, { label: 'Серия картинок', side: 'above' });
  const presets = presetChip({ host: () => root, snapshot, apply: applyPreset, say });
  const chipTranslate = el('button', { type: 'button', class: 'chip', id: 'photo-translate', hidden: true }, 'EN');
  const go = el('button', { type: 'submit', id: 'photo-go', class: 'btn btn-accent photo-go composer-go' }, 'Создать');
  const error = el('p', { id: 'photo-error', class: 'error photo-error', 'aria-live': 'polite' });
  const promptBox = fieldBox(prompt, { attach: chipPhoto, tools: [chipTranslate] });
  const form = el('form', { class: 'composer', id: 'photo-composer' }, sourceBox, promptBox.box, promptBox.error,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, chipModel.wrap, chipSize.wrap, chipTranslate,
      el('div', { class: 'chip-wrap' }, chipSeries, seriesMenu), presets.wrap), go));
  const root = el('div', { class: 'photo' }, head, status, feedBox, error, ctx.hint ? ctx.hint('hint-bar') : form,
    selection.bar);
  view.append(root);
  const draft = sectionDraft('photo', prompt);
  if (prompt.value) autosize();

  let focusId = null;            // запись из «В работе», которую ещё ждёт лента
  const feed = createFeed(feedBox, {
    key: (item) => item.id,
    render: card,
    quiet: ['elapsed', 'chain'],          // время идущей — таймер tick; цепочка — не в карточке
    load: async (last) => (await api('GET', `/api/images?limit=${PAGE}${pageQuery(last)}`)).items,
    empty: ctx.hint ? 'Здесь появятся ваши картинки.' : 'Здесь появятся ваши картинки. Опишите картинку внизу и нажмите «Создать».',
    head: tableHead(['', 'Промпт', 'Модель', 'Размер', 'Seed', 'Время', '']),
    onChange: () => {
      selection.sync();
      focusCard();
    },
    dayTools: selection.dayTools,
  });
  selection.attach({ root, head, feedBox, feed, error });

  /* переход к записи из «В работе» (#/photo?id=…): готовая — просмотр, иначе — её карточка в ленте;
   * ?id из адреса убирается: повторное «Открыть» снова сменит адрес */
  function focusCard() {
    const node = focusId && feedBox.querySelector(`.photo-card[data-id="${CSS.escape(focusId)}"]`);
    if (!node) return;
    focusId = null;
    for (const old of feedBox.querySelectorAll('.jobs-focus')) old.classList.remove('jobs-focus');
    node.classList.add('jobs-focus');
    node.scrollIntoView({ block: 'center' });
  }

  /** ?id адреса — при сборке и при переходе внутри раздела (update): прочее состояние не трогает. */
  function follow(wanted) {
    if (!wanted) return;
    history.replaceState(history.state, '', '#/photo');
    api('GET', `/api/images/${encodeURIComponent(wanted)}`).then((item) => {
      if (!alive) return;
      if (item.state === 'done' && item.media_id) {
        const shown = root.querySelector('#viewer');
        if (shown) shown.close();                       // просмотр один: прежний уступает
        openViewer(item);
      } else {
        focusId = item.id;
        focusCard();
      }
    }, (err) => { if (alive) error.textContent = words(err); });
  }
  follow(ctx.query.get('id'));

  /* меню чипов и раздела */
  function chipMenu(id, initial, get, set, short = (label) => label) {
    const name = el('span', { class: 'chip-name' });
    const chip = el('button', { type: 'button', class: 'chip', id }, name, icon('down', 'ico caret'));
    const list = el('div', { class: 'chip-menu', id: `${id}-menu`, role: 'menu' });
    const chipPopup = popupMenu(chip, list, { side: 'above' });
    let options = [];
    const paint = () => {
      name.textContent = short((options.find((o) => o[0] === get()) || options[0] || [null, 'модель'])[1]);
      for (const b of list.children) b.setAttribute('aria-checked', String(b.dataset.value === get()));
    };
    const fill = (next) => {
      options = next;
      list.replaceChildren(...options.map(([value, label]) => {
        return menuItem(label, { radio: true, attrs: { 'data-value': value }, onSelect: () => {
          set(value);
          paint();
          chipPopup.close(true);
        } });
      }));
      paint();
    };
    fill(initial);
    return { wrap: el('div', { class: 'chip-wrap' }, chip, list), fill, paint };
  }

  function choiceKey(c) {
    return c ? `${c.connection_id}:${c.model}` : null;
  }

  function isLustify() {
    return !!choice && choice.kind === 'srvllm' && choice.model === 'lustify';
  }

  /** Варианты чипа «модель» — отмеченные модели раздела «Фото», выбор — его умолчание. */
  function loadModels(section) {
    const many = section.links.filter((l) => l.models.length).length > 1;
    choices = section.links.flatMap(({ connection: c, models }) => models.map((m) => {
      const pick = { connection_id: c.id, model: m, kind: c.kind };
      return [choiceKey(pick), many ? `${modelLabel(m)} · ${c.name}` : modelLabel(m), pick];
    }));
    const def = section.default && choices.find((c) => c[0] === `${section.default.connection_id}:${section.default.model}`);
    choice = (def || choices[0] || [])[2] || null;
    chipModel.fill(choices.map(([value, label]) => [value, label]));
    paintSource();
  }

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
    strengthLabel.hidden = !isLustify();
    series.refresh();
  }

  /* пресеты: снимок поля и чипов, применение пресета */
  function snapshot() {
    return { model: choice ? choice.model : null, connection_id: choice ? choice.connection_id : null, size,
      strength: Number(strength.value), prompt: prompt.value.trim() || null };
  }

  /** Модель — если пара есть среди отмеченных моделей раздела; формат и сила — всегда; промпт — в пустое
   * поле, в непустое — после вопроса («Оставить свой» ставит всё, кроме промпта). Seed пресета прежнего
   * кода не ставится: поля seed нет. */
  function applyPreset(p) {
    const rest = () => {
      if (p.model) {
        const found = choices.find(([, , c]) => c.model === p.model
          && (p.connection_id === null || p.connection_id === undefined || c.connection_id === p.connection_id));
        if (found) {
          choice = found[2];
          chipModel.paint();
          say('', false);
        } else {
          say(`модели «${modelLabel(p.model)}» нет в разделе — выбрана текущая`, false);
        }
      }
      if (SIZES.some((s) => s[0] === p.size)) {
        size = p.size;
        chipSize.paint();
      }
      if (p.strength !== null && p.strength !== undefined) {
        strength.value = String(p.strength);
        strengthValue.textContent = Number(strength.value).toFixed(2);
      }
      paintSource();
    };
    if (!p.prompt || !prompt.value.trim() || prompt.value.trim() === p.prompt) {
      if (p.prompt) {
        prompt.value = p.prompt;
        autosize();
      }
      rest();
      return;
    }
    const replace = button('Заменить', { class: 'btn btn-accent', id: 'preset-prompt-replace' });
    const keep = button('Оставить свой', { class: 'btn', id: 'preset-prompt-keep' });
    const dialog = openDialog(root, { id: 'preset-prompt-dialog', title: 'Заменить промпт пресетом?',
      body: [el('p', { class: 'muted small' }, 'В поле уже есть промпт. Модель, формат и силу пресет поставит в любом случае.')],
      actions: [keep, replace] });
    replace.addEventListener('click', () => {
      prompt.value = p.prompt;
      autosize();
      dialog.close();
      rest();
    });
    keep.addEventListener('click', () => {
      dialog.close();
      rest();
    });
  }

  /* занятость: одно задание за раз */
  function syncBusy() {
    const busy = submitting || describing || !!translating || running.size > 0;
    for (const b of [go, ...gated]) {
      b.disabled = busy;
      b.title = busy ? 'Дождитесь конца текущего задания' : '';
    }
    describeFile.disabled = busy;                    // «Описать фото с устройства…» меню раздела
    chipTranslate.disabled = busy && !translating;   // во время перевода «EN» его отменяет
    chipTranslate.title = translating ? 'Отменить перевод'
      : busy ? 'Дождитесь конца текущего задания' : 'Перевести промпт на английский (gemma-abl)';
    chipTranslate.setAttribute('aria-label', chipTranslate.title);
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
    if (item.state === 'queued') return cap(item.detail);      // «в очереди серии · 2 из 3» — без таймера
    const spent = clock(item.elapsed + (Date.now() - entry.at) / 1000);
    return `${item.state === 'cancelling' ? 'Отменяю' : 'Генерирую'} · ${spent}`;
  }

  /** Картинка карточки: миниатюра; в превью — и полный файл, если миниатюры мало (srcset). */
  function picture(item) {
    const thumb = media(item.media_id, '?thumb=1');
    const [w, h] = String(item.size).split('x').map(Number);
    const big = layout.value === 'preview' && w > 0 && h > 0;
    return el('img', { src: thumb, alt: '', loading: 'lazy', decoding: 'async',
      srcset: big ? `${thumb} ${Math.round(w * Math.min(1, THUMB_SIDE / Math.max(w, h)))}w, ${media(item.media_id)} ${w}w` : null,
      sizes: big ? '(max-width: 760px) 100vw, 400px' : null });
  }

  function openButton(item, ...inside) {
    const open = el('button', { type: 'button', class: 'card-open', 'aria-label': 'Открыть картинку' }, ...inside);
    open.addEventListener('click', () => openViewer(item));
    return open;
  }

  function saveButton(item) {
    const save = el('button', { type: 'button', class: 'card-save', 'aria-label': 'Скачать', title: 'Скачать' }, icon('save'));
    save.addEventListener('click', (event) => {
      event.stopPropagation();
      saveMedia(item.media_id, root);
    });
    return save;
  }

  function cancelButton(item) {
    return button('Отменить', { class: 'btn-link card-cancel' }, async (event) => {
      event.currentTarget.disabled = true;
      try {
        await api('POST', `/api/images/${item.id}/cancel`);
      } catch (err) {
        say(words(err), true);
      }
      poll(item.id);
    });
  }

  function deleteButton(item) {
    return button('Удалить', { class: 'btn-link card-delete' }, () => remove(item).catch((err) => say(words(err), true)));
  }

  function noteText(item) {
    return item.state === 'cancelled' ? cap(item.detail) || 'Отменено' : `Ошибка: ${item.detail || 'картинка не получена'}`;
  }

  /** Модель, размер и seed — столбцы таблицы (seed внешнего сервиса не показывается). */
  function params(item) {
    const shown = seedOf(item);
    return [NAMES[item.model] || item.model, String(item.size || '').replace('x', '×'), shown !== null ? `seed ${shown}` : ''];
  }

  /** Строка вида «Таблица»: миниатюра, промпт, модель, размер, seed, время или ход, действие. */
  function row(item, node) {
    const done = item.state === 'done' && item.media_id;
    const busy = RUNNING.has(item.state);
    const when = done ? el('span', {}, timeOf(item.created))
      : busy ? el('b', { class: 'card-time' }, busyText(running.get(item.id)))
        : el('span', { class: item.state === 'error' ? 'error' : 'muted' }, noteText(item));
    node.append(done ? openButton(item, picture(item))
      : el('span', { class: 'row-thumb' }, busy ? el('span', { class: 'ring', 'aria-hidden': 'true' }) : null),
    el('p', { class: 'cell-prompt', title: item.prompt }, item.prompt),
    el('span', { class: 'cell-meta' }, ...params(item).map((p) => el('span', {}, p)), el('span', { class: 'cell-when' }, when)),
    el('span', { class: 'cell-act' }, done ? saveButton(item) : busy ? (CANCELLABLE.has(item.state) ? cancelButton(item) : null)
      : deleteButton(item)));
    return node;
  }

  function card(item) {
    watch(item);
    const node = el('div', { class: 'card photo-card', 'data-id': item.id, 'data-state': item.state });
    if (layout.value === 'table') return selection.decorate(row(item, node), item);
    if (item.state === 'done' && item.media_id) {
      // «Скачать» — рядом с .card-open, а не внутри: кнопка в кнопке недопустима
      node.append(openButton(item, picture(item),
        layout.value === 'tiles' ? el('span', { class: 'card-cap' }, NAMES[item.model] || item.model) : null), saveButton(item));
    } else if (RUNNING.has(item.state)) {
      node.append(el('div', { class: 'card-busy' }, el('span', { class: 'ring', 'aria-hidden': 'true' }),
        el('b', { class: 'card-time' }, busyText(running.get(item.id))),
        CANCELLABLE.has(item.state) ? cancelButton(item) : el('p', { class: 'small' }, cap(item.detail))));
    } else {
      node.append(el('div', { class: `card-note${item.state === 'error' ? ' error' : ''}` }, el('p', {}, noteText(item)),
        deleteButton(item)));
    }
    if (layout.value === 'preview') {
      const [w, h] = String(item.size).split('x').map(Number);
      if (w > 0 && h > 0) node.style.setProperty('--ratio', `${w} / ${h}`);
      node.append(el('div', { class: 'card-info' }, el('p', { class: 'card-prompt', title: item.prompt }, item.prompt),
        el('p', { class: 'card-meta' }, [...paramParts(item), timeOf(item.created)].filter(Boolean).join(' · '))));
    }
    return selection.decorate(node, item);
  }

  const tick = setInterval(() => {
    for (const [id, entry] of running) {
      const time = feedBox.querySelector(`.photo-card[data-id="${id}"] .card-time`);
      if (time) time.textContent = busyText(entry);
    }
  }, 1000);

  /* задание */
  /** Новые записи — в ленту сразу все (строки серии — по порядку, первая сверху). */
  async function adopt(ids) {
    try {
      const items = await Promise.all(ids.map((id) => api('GET', `/api/images/${id}`)));
      for (const item of items.reverse()) feed.prepend(item);
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
      const out = await api('POST', '/api/images', body);
      await adopt(out.ids || [out.id]);
      kick();
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

  /** Тело задания из поля и чипов (без seed — его выбирает студия); withSource — с исходным фото, если оно выбрано. */
  function compose(text, withSource) {
    const picked = choice ? { model: choice.model, connection_id: choice.connection_id } : {};
    const many = withSource ? series.value() : null;
    if (!withSource || !source) return { prompt: text, size, ...picked, ...(many ? { series: many } : {}) };
    if (source.file.size > PHOTO_MAX) throw new Error('Фото больше 10 МиБ');
    const body = new FormData();
    for (const [k, v] of Object.entries({ prompt: text, size, ...picked })) body.append(k, String(v));
    if (isLustify()) body.append('strength', strength.value);
    if (many) body.append('series', JSON.stringify(many));
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
    const many = withSource && series.value();
    const value = prompt.value;
    create(body).then((ok) => {
      if (ok) draft.sent(value);                    // отправленный промпт черновиком не возвращается
      if (ok && many) series.reset();               // серия — на один запуск
    });
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
        openEditor(item, choice, (created) => {
          adopt(created.ids || [created.id]);
          kick();
        });
      }),
      act(isExternal(item.model) ? 'Повторить' : 'Повторить с тем же seed', 'act-repeat', () => {
        dialog.close();
        create(repeatBody(item));
      }),
      act('Описать', 'act-describe', () => {
        dialog.close();
        describe({ mediaId: item.media_id });
      })];
    // без модели раздела «Изменить» и «Повторить» не начать — их нет; «Описать» решает шлюз раздела
    const actions = el('div', { class: 'viewer-actions' }, ctx.hint ? null : starts[0],
      act('Создать видео', 'act-animate', () => {
        dialog.close();
        ctx.navigate(`video?photo=${encodeURIComponent(item.id)}`);
      }),
      ctx.hint ? null : starts[1], starts[2],
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
    // цепочка — у записи из GET /api/images/<id>; у записи ленты её нет — догрузить
    let chain = el('ol', { class: 'viewer-chain' });
    const showChain = (steps) => {
      const next = chainList(steps, { current: item.id, onOpen });
      chain.replaceWith(next);
      chain = next;
      // исходник с файлом — предыдущий шаг цепочки (удалённая строка файл не уносит, пока на него ссылаются)
      const at = steps.findIndex((s) => s.image_id === item.id);
      const before = at > 0 ? steps[at - 1] : null;
      if (before && before.media_id) {
        actions.prepend(act('Сравнить с исходником', 'act-compare', () => openCompare(root,
          { media_id: before.media_id, label: stepLabel(before, at) }, { media_id: item.media_id, label: stepLabel(steps[at], at + 1) })));
      }
    };
    if (item.chain) showChain(item.chain);
    else {
      api('GET', `/api/images/${encodeURIComponent(item.id)}`).then((full) => {
        if (dialog.open) showChain(full.chain);
      }, (err) => { missing.textContent = err.code === 'gone' ? 'Картинка удалена' : words(err); });
    }
    const variants = el('div', { class: 'viewer-variants', hidden: true });
    api('GET', `/api/images/${encodeURIComponent(item.id)}/variants`).then(({ siblings, children }) => {
      if (!dialog.open || (!siblings.length && !children.length)) return;
      const strip = (title, list, cls) => (list.length ? el('div', { class: `variants-row ${cls}` },
        el('p', { class: 'variants-title' }, title), el('div', { class: 'variants-list' }, ...list.map((v) => {
          const open = el('button', { type: 'button', class: 'variant-open', 'data-id': v.id,
            'aria-label': 'Открыть вариант', title: paramParts(v).filter(Boolean).join(' · ') },
          el('img', { src: media(v.media_id, '?thumb=1'), alt: '', loading: 'lazy', decoding: 'async' }));
          open.addEventListener('click', () => {
            dialog.close();
            openViewer(v);
          });
          return open;
        }))) : null);
      variants.replaceChildren(strip('Из того же исходника', siblings, 'variants-siblings'),
        strip('Сделаны из этой', children, 'variants-children'));
      variants.hidden = false;
    }, () => {});                                       // варианты — подсказка: без них просмотр тот же
    dialog.append(el('div', { class: 'viewer-top' }, close), stage,
      el('div', { class: 'viewer-bar' }, chain, variants, missing, actions));
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

  /** Подпись картинки в сравнении: номер шага цепочки и модель · seed · сила (загрузка — «фото с устройства»). */
  function stepLabel(step, n) {
    const parts = paramParts(step).filter(Boolean);
    return `${n}. ${parts.length ? parts.join(' · ') : 'Фото с устройства'}`;
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
    /* окно закрыли или ушли из раздела (destroy: удалённый <dialog> события close не шлёт); задание без
     * id отменит .then создания — он видит !open */
    const shut = () => {
      if (!open) return;
      open = false;
      if (shutDescribe === shut) shutDescribe = null;
      clearTimeout(timer);
      if (url) URL.revokeObjectURL(url);
      if (jobId && !finished) api('POST', `/api/images/${jobId}/cancel`).catch(() => {});
      if (!finished) stop();
    };
    shutDescribe = shut;
    dialog.addEventListener('close', shut);
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
          api('DELETE', `/api/images/descriptions/${jobId}`).catch(() => {});    // получено — сервер стирает
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

  /* «EN»: gemma-abl переводит промпт на английский; перевод встаёт в поле вместо него */
  async function translate() {
    if (translating) {                                    // нажали ещё раз — отменить (id ещё нет — после создания)
      translating.cancelled = true;
      if (translating.id) api('POST', `/api/images/${translating.id}/cancel`).catch(() => {});
      return;
    }
    const text = prompt.value.trim();
    if (!text) {
      error.textContent = 'Напишите промпт';
      return;
    }
    error.textContent = '';
    const job = { id: null, started: Date.now(), cancelled: false };
    translating = job;
    prompt.readOnly = true;                               // правка во время перевода пропала бы
    const paint = () => { chipTranslate.textContent = `EN · ${clock((Date.now() - job.started) / 1000)}`; };
    paint();
    const timer = setInterval(paint, 1000);
    syncBusy();
    try {
      job.id = (await api('POST', '/api/images/translations', { prompt: text })).id;
      if (job.cancelled) api('POST', `/api/images/${job.id}/cancel`).catch(() => {});
      let fails = 0;
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        if (!alive) {                                     // ушли из раздела: перевод никто не заберёт
          api('POST', `/api/images/${job.id}/cancel`).catch(() => {});
          return;
        }
        let r;
        try {
          r = await api('GET', `/api/images/translations/${job.id}`);
        } catch (err) {
          if (err.code === 'gone') throw err;
          if (job.cancelled) break;                       // отменили, а ответа нет — отмена уже ушла
          if (++fails >= TRANSLATE_FAILS) {               // не связь мигнула, а отказ надолго — поле вернуть
            api('POST', `/api/images/${job.id}/cancel`).catch(() => {});
            throw new Error(`Перевод не получился: ${err.message}`);
          }
          continue;                                       // связь мигнула — спросить ещё раз
        }
        fails = 0;
        if (r.state === 'done') {
          if (!job.cancelled) {                           // отмена опоздала — перевод не нужен
            prompt.value = r.text;
            autosize();
          }
          api('DELETE', `/api/images/translations/${job.id}`).catch(() => {});   // получен — сервер стирает
          break;
        }
        if (r.state === 'error') throw new Error(`Перевод не получился: ${r.detail}`);
        if (r.state === 'cancelled') break;
      }
    } catch (err) {
      if (alive) error.textContent = words(err);
    } finally {
      clearInterval(timer);
      translating = null;
      if (alive) {
        prompt.readOnly = false;
        chipTranslate.textContent = 'EN';
        syncBusy();
      }
    }
  }

  /* освобождение карты (администратор) */
  async function release() {
    menuPopup.close();
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
    // Пустое поле сохраняет три строки из общих стилей, непустое растёт по содержимому.
    if (prompt.value) prompt.style.height = `${prompt.scrollHeight}px`;
  }

  /* события (меню закрывают сами: menu.js) */

  file.addEventListener('change', () => setSource(file.files[0] || null));
  sourceClear.addEventListener('click', () => {
    file.value = '';
    setSource(null);
  });
  strength.addEventListener('input', () => {
    strengthValue.textContent = Number(strength.value).toFixed(2);
    series.refresh();                                   // сила «от» серии по силе — этот ползунок
  });
  chipTranslate.addEventListener('click', translate);
  describeFile.addEventListener('change', () => {
    const f = describeFile.files[0];
    describeFile.value = '';
    if (f) describe({ file: f });
  });
  prompt.addEventListener('input', autosize);
  enterSends(prompt, () => form.requestSubmit());
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!go.disabled) submit(true);
  });
  syncBusy();
  if (!ctx.hint) presets.load();
  api('GET', '/api/sections').then(({ sections }) => {
    if (alive) loadModels(sections.find((s) => s.id === 'photo'));
  }, (err) => say(words(err), true));

  return {
    follow,
    destroy() {
      draft.save();
      alive = false;
      if (shutDescribe) shutDescribe();
      selection.destroy();
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

/** Переход внутри открытого раздела (#/photo?id=…): только ?id — промпт, исходник, серия и выделение целы. */
export function update(ctx) {
  if (screen) screen.follow(ctx.query.get('id'));
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
