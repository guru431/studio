/* Цепочка промптов картинки или ролика (item.chain, спека 2026-10-20, раздел 6.6): шаги по порядку,
 * текущий выделен. У шага — миниатюра (нажатие открывает картинку шага), вид, модель, размер, seed
 * или сила, дата, промпт и «Копировать». Удалённая картинка — «удалена», миниатюра не нажимается.
 * Текст сервера — только textContent. */
import { el } from './ui.js';

const KINDS = { generation: 'Генерация', upload: 'Фото с устройства', edit: 'Правка', brush: 'Правка кистью',
  video: 'Ролик' };
const NAMES = { lustify: 'Lustify', flux: 'FLUX' };
const RESOLUTION = { fast: '480p', standard: '576p', high: '720p' };

function meta(step, n) {
  const date = step.created ? new Date(step.created * 1000).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' }) : '';
  const parts = step.kind === 'video'
    ? [`${step.seconds} с`, RESOLUTION[step.quality] || step.quality]
    : [step.model && (NAMES[step.model] || step.model), step.size && String(step.size).replace('x', '×'),
      step.seed !== undefined && step.seed !== null ? `seed ${step.seed}` : '',
      step.strength !== undefined && step.strength !== null ? `сила ${step.strength}` : ''];
  return [`${n}. ${KINDS[step.kind] || step.kind}`, ...parts, date].filter(Boolean).join(' · ');
}

function thumb(step, isNow, onOpen) {
  // ролик — не картинка: его миниатюры нет, а полный файл в <img> не покажется
  if (step.gone || !step.media_id || step.kind === 'video') {
    return el('span', { class: `chain-thumb${step.gone ? ' gone' : ''}`, 'aria-hidden': 'true' });
  }
  const img = el('img', { class: 'chain-thumb', src: `/api/media/${encodeURIComponent(step.media_id)}?thumb=1`, alt: '',
    loading: 'lazy', decoding: 'async' });
  if (isNow || !step.image_id) return img;
  const open = el('button', { type: 'button', class: 'chain-open', 'aria-label': 'Открыть картинку шага' }, img);
  open.addEventListener('click', () => onOpen(step.image_id));
  return open;
}

function copyButton(text) {
  const copy = el('button', { type: 'button', class: 'btn btn-sm chain-copy' }, 'Копировать');
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(text.textContent);
      copy.textContent = 'Скопировано';
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
    if (step.gone) body.append(el('p', { class: 'chain-gone' }, 'удалена'));
    let copy = null;
    if (step.kind !== 'upload') {
      const text = el('p', { class: 'chain-prompt' },
        step.prompt || (step.kind === 'video' ? 'придуман по фото' : ''));
      body.append(text);
      if (step.prompt) copy = copyButton(text);
    }
    return el('li', { class: `chain-step${isNow ? ' now' : ''}`, 'data-kind': step.kind, 'aria-current': isNow ? 'true' : null },
      thumb(step, isNow, onOpen), body, copy || el('span'));
  }));
}
