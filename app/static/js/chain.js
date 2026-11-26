import { tr } from './i18n.js';
/* Цепочка промптов картинки или ролика (item.chain, спека 2026-10-20, раздел 6.6): шаги по порядку,
 * текущий выделен. У шага — миниатюра (нажатие открывает картинку шага), вид, модель, размер, seed
 * или сила, дата, промпт и «Копировать». Удалённая картинка — «удалена», миниатюра не нажимается.
 * Текст сервера — только textContent.
 *
 * Параметры по модели (спека 2026-10-21-studio-photo-presets-compare-series, раздел 9) — общие для
 * цепочки, карточек и сравнения: сила — у моделей, которые её получали (включая снятые), seed — не
 * у внешнего сервиса (модель не из перечня шлюза). */
import { el } from './ui.js';
import { dateOnly } from './time.js';
import { LABELS } from './models.js';

const KINDS = { generation: tr("Генерация"), upload: tr("Фото с устройства"), edit: tr("Правка"), brush: tr("Правка кистью"),
  upscale: tr("Улучшение фото"), video: tr("Ролик") };
/** Модели картинок шлюза (images.MODELS и снятые) → подпись из общей таблицы models.js. */
export const NAMES = Object.fromEntries(['lustify', 'flux', 'snofs', 'big-love', 'fineview', 'muse', 'jib-moss', 'fineporn',
  'moody-krea'].map((id) => [id, LABELS[id]]));
const RESOLUTION = { fast: '480p', standard: '576p', high: '720p', native: '768p' };
const STRENGTH_HISTORY = new Set(['lustify', 'snofs', 'big-love', 'muse', 'jib-moss', 'fineporn', 'moody-krea']);

/** Модель внешнего сервиса: не из перечня шлюза (images.MODELS). */
export function isExternal(model) {
  return !!model && !Object.hasOwn(NAMES, model);
}

/** Seed, который стоит показывать (null — нет: внешний сервис его не получает). */
export function seedOf(step) {
  return step.seed !== undefined && step.seed !== null && (!isExternal(step.model) || step.mcp_job_id) ? step.seed : null;
}

/** Сила, фактически переданная модели при создании записи. */
export function strengthOf(step) {
  return STRENGTH_HISTORY.has(step.model) ? step.strength ?? null : null;
}

/** Модель, размер, seed и сила шага или записи — части подписи. */
export function paramParts(step) {
  const seed = seedOf(step);
  const strength = strengthOf(step);
  return [(step.model && (NAMES[step.model] || step.model)) || step.server, step.size && String(step.size).replace('x', '×'),
    seed !== null ? `seed ${seed}` : '', strength !== null ? tr("сила {0}", strength) : ''];
}

function meta(step, n) {
  const date = step.created ? dateOnly(step.created, { day: 'numeric', month: 'short' }) : '';
  const parts = step.kind === 'video'
    ? [step.seconds != null ? tr("{0} с", step.seconds) : null, RESOLUTION[step.quality] || step.quality]
    : paramParts(step);
  return [`${n}. ${KINDS[step.kind] || step.kind}`, ...parts, step.model ? step.server : null, date].filter(Boolean).join(' · ');
}

function thumb(step, isNow, onOpen) {
  // ролик — не картинка: его миниатюры нет, а полный файл в <img> не покажется
  if (step.gone || !step.media_id || step.kind === 'video') {
    return el('span', { class: `chain-thumb${step.gone ? ' gone' : ''}`, 'aria-hidden': 'true' });
  }
  const img = el('img', { class: 'chain-thumb', src: `/api/media/${encodeURIComponent(step.media_id)}?thumb=1`, alt: '',
    loading: 'lazy', decoding: 'async' });
  if (isNow || !step.image_id) return img;
  const open = el('button', { type: 'button', class: 'chain-open', 'aria-label': tr("Открыть картинку шага") }, img);
  open.addEventListener('click', () => onOpen(step.image_id));
  return open;
}

function copyButton(text) {
  const copy = el('button', { type: 'button', class: 'btn btn-sm chain-copy' }, tr("Копировать"));
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text.textContent);
      copy.textContent = tr("Скопировано");
    } catch {
      getSelection().selectAllChildren(text);          // буфера нет — выделить, скопирует сам человек
    }
  });
  return copy;
}

/** Список шагов `ol.viewer-chain`; current — id картинки, которую показывают (null — последний шаг). */
export function chainList(chain, { current, onOpen }) {
  const last = chain.length - 1;
  return el('ol', { class: 'viewer-chain' }, ...chain.map((step, i) => {
    const isNow = current ? step.image_id === current : i === last;
    const body = el('div', { class: 'chain-body' }, el('p', { class: 'chain-meta' }, meta(step, i + 1)));
    if (step.gone) body.append(el('p', { class: 'chain-gone' }, tr("удалена")));
    let copy = null;
    if (step.kind !== 'upload' && step.kind !== 'upscale') {
      const text = el('p', { class: 'chain-prompt' },
        step.prompt || (step.kind === 'video' ? tr("придуман по фото") : ''));
      body.append(text);
      if (step.prompt) copy = copyButton(text);
    }
    return el('li', { class: `chain-step${isNow ? ' now' : ''}`, 'data-kind': step.kind, 'aria-current': isNow ? 'true' : null },
      thumb(step, isNow, onOpen), body, copy || el('span'));
  }));
}
