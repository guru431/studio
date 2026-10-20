/* «Скачать» картинку или ролик истории (превью ленты, просмотр, карточка ролика).
 *
 * Компьютер и Android — обычная загрузка `/api/media/<id>?download=1` (в «Загрузки»). iPhone — меню
 * «Поделиться» с готовым файлом: молча сохранить в «Фото» веб-приложение не может, а в меню есть
 * «Сохранить изображение» / «Сохранить видео». Файл качается после нажатия, и Safari может счесть
 * жест истёкшим (NotAllowedError) — тогда окно «Файл готов» с кнопкой «Сохранить»: её нажатие —
 * новый жест. Файл больше SHARE_MAX в память телефона не берётся — обычная загрузка. */
import { button, el, openDialog } from './ui.js';

export const SHARE_MAX = 100 * 1024 * 1024;
const EXT = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'video/mp4': '.mp4' };

function isIOS() {
  return /iP(hone|ad|od)/.test(navigator.userAgent)
    || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
}

function download(mediaId) {
  el('a', { href: `/api/media/${encodeURIComponent(mediaId)}?download=1`, download: '' }).click();
}

async function share(mediaId, host) {
  const abort = new AbortController();
  const response = await fetch(`/api/media/${encodeURIComponent(mediaId)}`,
    { credentials: 'same-origin', signal: abort.signal });
  if (!response.ok || Number(response.headers.get('Content-Length')) > SHARE_MAX) {
    abort.abort();
    download(mediaId);
    return;
  }
  const blob = await response.blob();
  const file = new File([blob], `studio-${mediaId.slice(0, 8)}${EXT[blob.type] || ''}`, { type: blob.type });
  if (!navigator.canShare({ files: [file] })) {
    download(mediaId);
    return;
  }
  try {
    await navigator.share({ files: [file] });
  } catch (err) {
    if (err.name !== 'NotAllowedError') throw err;
    const again = button('Сохранить', { id: 'save-again', class: 'btn btn-accent' });
    const dialog = openDialog(host, { id: 'save-dialog', title: 'Файл готов', actions: [again],
      body: [el('p', {}, 'Нажмите «Сохранить» и выберите в меню «Сохранить изображение» или «Сохранить видео».')] });
    again.addEventListener('click', () => {
      navigator.share({ files: [file] }).catch(() => {});
      dialog.close();
    });
  }
}

export async function saveMedia(mediaId, host) {
  if (!isIOS() || typeof navigator.canShare !== 'function') {
    download(mediaId);
    return;
  }
  try {
    await share(mediaId, host);
  } catch (err) {
    if (err.name !== 'AbortError') download(mediaId);     // AbortError — человек закрыл меню
  }
}
