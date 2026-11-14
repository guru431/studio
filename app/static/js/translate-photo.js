import { tr } from './i18n.js';
/* Снимок в «Переводе» — спека 2026-10-28-studio-translator-design, 6.2 «Снимок», 6.5.
 *
 * photoView(record, parts) — узел записи-снимка: переключатель #tl-mode-overlay «Поверх снимка» (по умолчанию) /
 * #tl-mode-text «Текстом», а в режиме наложения — #tl-original «Исходник» (aria-pressed: слой спрятан) и
 * #tl-save «Сохранить снимок». #tl-photo — <img src="/api/media/<media_id>"> и слой: на каждый переведённый
 * блок `.tl-block` в рамке `box` (доли 0…1000 того же повёрнутого PNG, что ушёл в распознавание, — left/top/
 * width/height = box / 10 процентов). Кегль блока — наибольший от MIN_PX до высоты рамки, при котором текст
 * помещается (scrollWidth/scrollHeight ≤ client*); не помещается и на MIN_PX — MIN_PX с обрезкой. Подгонка —
 * при смене размера кадра (ResizeObserver: загрузка картинки, ширина окна, возвращение из «Текстом»), при новых
 * блоках и при возвращении слоя после «Исходника» (спрятанный слой не подгоняется). «Текстом» страница
 * показывает сама (translate.js): режим она узнаёт через `onMode`; `destroy()` — узел больше не нужен.
 *
 * savePng(record, parts) — PNG в натуральном размере на canvas: картинка, подложки и переводы, перенос по
 * словам; кегль каждого блока подгоняется заново на canvas под рамку в натуральном размере (fitCanvas), а не
 * берётся с экрана: на телефоне рамки на экране мельче MIN_PX, и экранный кегль обрезал бы перевод на PNG.
 * Текст сервера — только textContent и canvas. */
import { saveFile } from './save.js';
import { el, words } from './ui.js';

const MIN_PX = 8;
const LINE = 1.15;                       // высота строки к кеглю — та же, что у .tl-block
const PAD_X = 0.2;                       // поля подложки в кеглях — те же, что у .tl-block
const PAD_Y = 0.06;
const BACKING = 'rgba(255, 255, 255, .92)';
const INK = '#111';
const RUNNING = new Set(['queued', 'working', 'cancelling']);

/** Переведённые блоки с рамкой. */
function shown(parts) {
  return parts.filter((p) => p.box && p.state === 'done' && p.text);
}

/** «Вывеска.jpg» → «Вывеска.ru.png». */
function fileName(record) {
  const name = String(record.name || tr("снимок"));
  const dot = name.lastIndexOf('.');
  return `${dot > 0 ? name.slice(0, dot) : name}.${record.result_lang || record.target_lang}.png`;
}

function fits(node) {
  return node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight;
}

/** Наибольший кегль от MIN_PX до высоты рамки, при котором текст помещается; иначе — MIN_PX с обрезкой. */
function fitBlock(node) {
  const height = node.clientHeight;
  if (!height) return;                   // кадр или слой спрятан — подгонка при возвращении
  let lo = MIN_PX;
  let hi = Math.max(MIN_PX, Math.floor(height));
  node.style.fontSize = `${lo}px`;
  if (fits(node)) {
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      node.style.fontSize = `${mid}px`;
      if (fits(node)) lo = mid;
      else hi = mid - 1;
    }
  }
  node.style.fontSize = `${lo}px`;
}

export function photoView(record, parts, { mode = 'overlay', onMode = () => {} } = {}) {
  let rec = record;
  let blocks = parts;
  let key = '';

  const toOverlay = el('button', { type: 'button', id: 'tl-mode-overlay', class: 'chip' }, tr("Поверх снимка"));
  const toText = el('button', { type: 'button', id: 'tl-mode-text', class: 'chip' }, tr("Текстом"));
  const original = el('button', { type: 'button', id: 'tl-original', class: 'chip', 'aria-pressed': 'false' }, tr("Исходник"));
  const save = el('button', { type: 'button', id: 'tl-save', class: 'btn btn-sm' }, tr("Сохранить снимок"));
  const fail = el('p', { class: 'error tl-photo-error', 'aria-live': 'polite' });
  const img = el('img', { class: 'tl-photo-img', alt: tr("Снимок"), src: `/api/media/${encodeURIComponent(record.media_id)}` });
  const layer = el('div', { class: 'tl-layer' });
  const frame = el('div', { id: 'tl-photo', class: 'tl-photo' }, img, layer);
  const tools = el('div', { class: 'tl-photo-tools' }, original, save);
  const node = el('div', { class: 'tl-photo-view' },
    el('div', { class: 'tl-photo-bar' }, el('div', { class: 'tl-modes', role: 'group', 'aria-label': tr("Вид перевода") },
      toOverlay, toText), tools), fail, frame);

  function fitAll() {
    for (const b of layer.children) fitBlock(b);
  }

  function paintBlocks() {
    const done = shown(blocks);
    const next = done.map((p) => `${p.n}:${p.box.join(',')}:${p.text}`).join('\n');
    if (next !== key) {
      key = next;
      layer.replaceChildren(...done.map((p) => {
        const [x1, y1, x2, y2] = p.box;
        const b = el('div', { class: 'tl-block', 'data-n': String(p.n) });
        b.style.left = `${x1 / 10}%`;
        b.style.top = `${y1 / 10}%`;
        b.style.width = `${(x2 - x1) / 10}%`;
        b.style.height = `${(y2 - y1) / 10}%`;
        b.textContent = p.text;
        return b;
      }));
      fitAll();
    }
    save.disabled = RUNNING.has(rec.state) || !done.length;
  }

  function setMode(next) {
    const overlay = next !== 'text';
    toOverlay.setAttribute('aria-pressed', String(overlay));
    toText.setAttribute('aria-pressed', String(!overlay));
    frame.hidden = !overlay;
    tools.hidden = !overlay;
    if (!overlay) fail.textContent = '';
  }

  toOverlay.addEventListener('click', () => {
    setMode('overlay');
    onMode('overlay');
  });
  toText.addEventListener('click', () => {
    setMode('text');
    onMode('text');
  });
  original.addEventListener('click', () => {
    const on = original.getAttribute('aria-pressed') !== 'true';
    original.setAttribute('aria-pressed', String(on));
    layer.hidden = on;
    if (!on) fitAll();                   // пока слой был спрятан, кадр мог смениться и прийти новые блоки
  });
  save.addEventListener('click', async () => {
    save.disabled = true;
    fail.textContent = '';
    try {
      const blob = await savePng(rec, blocks);
      await saveFile(new File([blob], fileName(rec), { type: 'image/png' }), node);
    } catch (err) {
      fail.textContent = words(err);
    }
    paintBlocks();
  });
  const sized = new ResizeObserver(fitAll);    // загрузка картинки, ширина окна, возвращение из «Текстом»
  sized.observe(frame);

  setMode(mode);
  paintBlocks();

  return {
    node,
    setMode,
    destroy() {
      sized.disconnect();
    },
    /** Та же запись после опроса: новые переводы блоков — на слой. */
    update(nextRecord, nextParts) {
      rec = nextRecord;
      blocks = nextParts;
      paintBlocks();
    },
  };
}

/* canvas */

/** Строки текста по словам в ширину `width`; слово шире строки — своей строкой (обрежется рамкой, как на экране). */
function wrap(ctx, text, width) {
  const lines = [];
  for (const paragraph of text.split('\n')) {
    let line = '';
    for (const word of paragraph.split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word;
      if (line && ctx.measureText(next).width > width) {
        lines.push(line);
        line = word;
      } else {
        line = next;
      }
    }
    lines.push(line);
  }
  return lines;
}

function canvasFont(size, family) {
  return `${size}px ${family}`;
}

/** Кегль блока на canvas: наибольший от MIN_PX до высоты рамки, при котором строки по словам помещаются в
 * рамку по ширине и высоте; не помещается и на MIN_PX — MIN_PX (рамка обрежет). */
function fitCanvas(ctx, text, w, h, family) {
  const ok = (size) => {
    ctx.font = canvasFont(size, family);
    const lines = wrap(ctx, text, w - 2 * PAD_X * size);
    return lines.length * size * LINE + 2 * PAD_Y * size <= h
      && lines.every((line) => ctx.measureText(line).width <= w - 2 * PAD_X * size);
  };
  let lo = MIN_PX;
  let hi = Math.max(MIN_PX, Math.floor(h));
  if (!ok(lo)) return lo;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (ok(mid)) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

export async function savePng(record, parts) {
  const response = await fetch(`/api/media/${encodeURIComponent(record.media_id)}`, { credentials: 'same-origin' });
  if (!response.ok) throw new Error(response.status === 404 ? tr("снимок удалён") : tr("ошибка сервера ({0})", response.status));
  const picture = await createImageBitmap(await response.blob());
  const canvas = document.createElement('canvas');
  canvas.width = picture.width;
  canvas.height = picture.height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(picture, 0, 0);
  picture.close();
  const family = getComputedStyle(document.body).fontFamily || 'sans-serif';
  ctx.textBaseline = 'top';
  for (const p of shown(parts)) {
    const [x1, y1, x2, y2] = p.box;
    const x = x1 * canvas.width / 1000;
    const y = y1 * canvas.height / 1000;
    const w = (x2 - x1) * canvas.width / 1000;
    const h = (y2 - y1) * canvas.height / 1000;
    ctx.fillStyle = BACKING;
    ctx.fillRect(x, y, w, h);
    const size = fitCanvas(ctx, p.text, w, h, family);
    ctx.font = canvasFont(size, family);
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y, w, h);
    ctx.clip();
    ctx.fillStyle = INK;
    wrap(ctx, p.text, w - 2 * PAD_X * size).forEach((line, i) => {
      ctx.fillText(line, x + PAD_X * size, y + PAD_Y * size + i * size * LINE + (LINE - 1) * size / 2);
    });
    ctx.restore();
  }
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error(tr("снимок не собрался")))), 'image/png');
  });
}
