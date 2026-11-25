import { tr } from './i18n.js';
/* Число вложений сообщения «Агентов» — по пределам моста (`limits` в /api/agents из `hello.limits`, сервер —
 * `bridge.turn_limits`); мост без поля (до 0.48.0) — прежние 4 изображения и 8 вложений всего. Отказ — до отправки,
 * словами с действующим числом: незнакомого числа старому мосту страница не шлёт. */

const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const LEGACY = { images: 4, attachments: 8 };

/** Пределы компьютера: { images, attachments }. */
export function attachCounts(agent) {
  const limits = agent?.limits;
  if (!Number.isInteger(limits?.images) || !Number.isInteger(limits?.attachments)) return { ...LEGACY };
  return { images: limits.images, attachments: limits.attachments };
}

/** Отказ словами, если прикреплённые (`kept`: { mime }) и добавляемые (`added`: File) не умещаются, иначе null. */
export function countRefusal(kept, added, counts) {
  const images = [...kept, ...added].filter((f) => IMAGE_TYPES.has(f.mime || f.type)).length;
  if (images > counts.images) return tr("Можно прикрепить до {0} изображений к сообщению", counts.images);
  if (kept.length + added.length > counts.attachments) return tr("Можно прикрепить до {0} файлов", counts.attachments);
  return null;
}
