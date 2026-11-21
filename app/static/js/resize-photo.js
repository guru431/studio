import { el } from './ui.js';

const TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
const EXTENSIONS = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' };
export const DOCUMENT_PHOTO_SIDE = 2200;  // ocr_render.MAX_SIDE

/** Браузер применяет EXIF при декодировании <img>. При неудаче или росте файла отправляем исходник. */
export async function resizePhoto(file, limit, jpeg = false) {
  if (!TYPES.has(file.type)) return file;
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const side = Math.max(img.naturalWidth, img.naturalHeight);
    if (side <= limit) return file;
    const scale = limit / side;
    const canvas = el('canvas', { width: Math.max(1, Math.round(img.naturalWidth * scale)),
      height: Math.max(1, Math.round(img.naturalHeight * scale)) });
    const ctx = canvas.getContext('2d');
    if (jpeg) {
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, jpeg ? 'image/jpeg' : file.type, 0.9));
    if (!blob || blob.size >= file.size || !EXTENSIONS[blob.type]) return file;
    const name = file.name.replace(/\.[^.]*$/, '') || file.name;
    return new File([blob], `${name}.${EXTENSIONS[blob.type]}`, { type: blob.type });
  } catch {
    return file;
  } finally {
    URL.revokeObjectURL(url);
  }
}
