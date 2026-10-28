/* «Скачать» картинку или ролик истории (превью ленты, просмотр, карточка ролика) и несколько разом
 * (выделение в ленте, select.js).
 *
 * Компьютер и Android — обычная загрузка `/api/media/<id>?download=1` (в «Загрузки»). iPhone — меню
 * «Поделиться» с готовым файлом: молча сохранить в «Фото» веб-приложение не может, а в меню есть
 * «Сохранить изображение» / «Сохранить видео». Файл качается после нажатия, и Safari может счесть
 * жест истёкшим (NotAllowedError) — тогда окно «Файл готов» с кнопкой «Сохранить»: её нажатие —
 * новый жест. Файл больше SHARE_MAX в память телефона не берётся — обычная загрузка: по заголовку
 * Content-Length — до чтения, а без заголовка (chunked) — по настоящему размеру прочитанного.
 *
 * Несколько (saveMany): iPhone — все файлы одним меню «Поделиться», если вместе они не больше
 * SHARE_MAX; иначе и не на iPhone — один ZIP: POST /api/media/archives со списком, загрузка по токену.
 *
 * Файл, собранный самой страницей (saveFile: снимок «Перевода» с наложением), — тем же меню на iPhone, иначе —
 * загрузка blob под его именем. */
import { api } from './api.js';
import { button, el, openDialog } from './ui.js';

export const SHARE_MAX = 100 * 1024 * 1024;
const EXT = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'video/mp4': '.mp4' };

function isIOS() {
  return /iP(hone|ad|od)/.test(navigator.userAgent)
    || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
}

function link(href) {
  el('a', { href, download: '' }).click();
}

function download(mediaId) {
  link(`/api/media/${encodeURIComponent(mediaId)}?download=1`);
}

async function fileOf(mediaId, response) {
  const blob = await response.blob();
  return new File([blob], `studio-${mediaId.slice(0, 8)}${EXT[blob.type] || ''}`, { type: blob.type });
}

/** Меню «Поделиться» с файлами; жест истёк — окно с «Сохранить» (новый жест); закрыли меню — тихо.
 * false — окно закрыли «Отменой», не сохранив (выделение в ленте остаётся). */
async function offer(files, host) {
  try {
    await navigator.share({ files });
  } catch (err) {
    if (err.name === 'AbortError') return true;
    if (err.name !== 'NotAllowedError') throw err;
    const many = files.length > 1;
    const again = button('Сохранить', { id: 'save-again', class: 'btn btn-accent' });
    const dialog = openDialog(host, { id: 'save-dialog', title: many ? 'Файлы готовы' : 'Файл готов', actions: [again],
      body: [el('p', {}, many ? 'Нажмите «Сохранить» и выберите в меню «Сохранить изображения» или «Сохранить видео».'
        : 'Нажмите «Сохранить» и выберите в меню «Сохранить изображение» или «Сохранить видео».')] });
    return new Promise((resolve) => {
      again.addEventListener('click', () => {
        navigator.share({ files }).catch(() => {});
        resolve(true);
        dialog.close();
      });
      dialog.addEventListener('close', () => resolve(false));
    });
  }
  return true;
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
  const file = await fileOf(mediaId, response);
  if (file.size > SHARE_MAX || !navigator.canShare({ files: [file] })) {
    download(mediaId);
    return;
  }
  await offer([file], host);
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

/** Файл, собранный страницей (снимок «Перевода» с наложением): iPhone — меню «Поделиться» с ним, как saveMany;
 * иначе или меню не открылось — загрузка ссылкой на blob под его именем. */
export async function saveFile(file, host) {
  if (isIOS() && typeof navigator.canShare === 'function' && file.size <= SHARE_MAX && navigator.canShare({ files: [file] })) {
    try {
      await offer([file], host);
      return;
    } catch {
      // меню не открылось — загрузкой
    }
  }
  const url = URL.createObjectURL(file);
  el('a', { href: url, download: file.name }).click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

/** Файлы в память телефона по одному; удалённый в другой вкладке (404) пропускается, как в архиве;
 * null — не вышло, ни одного файла или вместе больше SHARE_MAX. */
async function fetchAll(mediaIds, onProgress) {
  const files = [];
  let total = 0;
  for (const [i, mediaId] of mediaIds.entries()) {
    onProgress(i + 1, mediaIds.length);
    const abort = new AbortController();
    const response = await fetch(`/api/media/${encodeURIComponent(mediaId)}`,
      { credentials: 'same-origin', signal: abort.signal });
    if (response.status === 404) {
      abort.abort();
      continue;
    }
    if (!response.ok || total + (Number(response.headers.get('Content-Length')) || 0) > SHARE_MAX) {
      abort.abort();
      return null;
    }
    const file = await fileOf(mediaId, response);
    total += file.size;                     // настоящий размер: заголовка может и не быть
    if (total > SHARE_MAX) return null;
    files.push(file);
  }
  return files.length ? files : null;
}

/** Несколько файлов истории разом; onProgress(i, n) — ход скачивания в память iPhone. Отказ сервера
 * архива — исключение api() (страница говорит его словами). false — окно «Файлы готовы» закрыли
 * «Отменой», ничего не сохранив. */
export async function saveMany(mediaIds, host, onProgress = () => {}) {
  if (isIOS() && typeof navigator.canShare === 'function') {
    let files = null;
    try {
      files = await fetchAll(mediaIds, onProgress);
    } catch {
      files = null;                       // сеть оборвалась — архивом
    }
    if (files && navigator.canShare({ files })) {
      try {
        return await offer(files, host);
      } catch {
        // меню не открылось — архивом
      }
    }
  }
  const { id } = await api('POST', '/api/media/archives', { media_ids: mediaIds });
  link(`/api/media/archives/${encodeURIComponent(id)}`);
  return true;
}
