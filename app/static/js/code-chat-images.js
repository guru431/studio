/* История хранит ссылки на картинки; бинарные данные загружаются только рядом с экраном.
 * Blob остаётся в памяти открытого чата и освобождается при прокрутке и уходе. */
import { el } from './ui.js';

const TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const MAX_IMAGE = 10 * 1024 * 1024;
const BUDGET = 32 * 1024 * 1024;
const PARALLEL = 4;

export function historyImages(scroller, feed, { api, endpoint, closed }) {
  const entries = new Set();
  const owners = new WeakMap();
  let alive = true, loading = 0, used = 0;

  function release(entry) {
    if (entry.url) URL.revokeObjectURL(entry.url);
    used -= entry.size;
    entry.url = null;
    entry.size = 0;
    entry.image.removeAttribute('src');
    entry.image.hidden = true;
    entry.note.hidden = false;
  }
  function visible(entry) {
    const box = entry.node.getBoundingClientRect(), view = scroller.getBoundingClientRect();
    return box.bottom > view.top && box.top < view.bottom;
  }
  function prune() {
    for (const entry of entries) if (!entry.node.isConnected) {
      entry.controller?.abort();
      observer.unobserve(entry.node);
      release(entry);
      entries.delete(entry);
    }
  }
  async function load(entry) {
    loading += 1;
    const own = entry.controller = new AbortController();
    entry.retry.hidden = true;
    entry.note.textContent = 'Загрузка изображения…';
    try {
      const response = await api('POST', endpoint('/image'),
        { message_id: entry.message, index: entry.index }, { signal: own.signal,
          headers: { 'x-studio-passive': '1' } });
      const blob = await response.blob();
      if (!alive || own.signal.aborted || !entry.node.isConnected || !entry.near) return;
      if (!TYPES.has(blob.type) || !blob.size || blob.size > MAX_IMAGE) throw new Error('image');
      for (const other of entries) {
        if (used + blob.size <= BUDGET) break;
        if (other !== entry && other.url && !visible(other)) {
          release(other);
          other.suspended = true;
        }
      }
      if (used + blob.size > BUDGET) throw new Error('image_budget');
      entry.url = URL.createObjectURL(blob);
      entry.size = blob.size;
      used += blob.size;
      entry.image.src = entry.url;
      entry.image.hidden = false;
      entry.note.hidden = true;
    } catch (error) {
      if (!alive || own.signal.aborted || !entry.node.isConnected) return;
      if (error.code === 'closed') { closed(); return; }
      entry.failed = true;
      entry.note.textContent = 'Изображение недоступно';
      entry.retry.hidden = false;
    } finally {
      if (entry.controller === own) entry.controller = null;
      loading -= 1;
      pump();
    }
  }
  function pump() {
    if (!alive) return;
    prune();
    for (const entry of entries) {
      if (loading >= PARALLEL) break;
      if (entry.near && !entry.url && !entry.controller && !entry.failed && (!entry.suspended || visible(entry))) load(entry);
    }
  }
  const observer = new IntersectionObserver(changes => {
    for (const change of changes) {
      const entry = owners.get(change.target);
      if (!entry) continue;
      entry.near = change.isIntersecting;
      if (!entry.near) {
        entry.controller?.abort();
        release(entry);
        entry.failed = false;
        entry.suspended = false;
      }
    }
    pump();
  }, { root: scroller, rootMargin: '400px 0px' });
  const changes = new MutationObserver(prune);
  changes.observe(feed, { childList: true });
  scroller.addEventListener('scroll', pump, { passive: true });

  return {
    node(message, picture) {
      const image = el('img', { class: 'code-sent-image', alt: picture.name || 'Изображение', hidden: true });
      const note = el('span', { class: 'muted small' }, 'Загрузка изображения…');
      const retry = el('button', { class: 'btn btn-sm', type: 'button', hidden: true }, 'Повторить загрузку');
      const node = el('div', { class: 'code-history-image' }, image, note, retry);
      const entry = { node, image, note, retry, message, index: picture.index,
        url: null, size: 0, controller: null, failed: false, suspended: false, near: false };
      retry.addEventListener('click', () => { entry.failed = false; pump(); });
      image.addEventListener('error', () => {
        if (!entry.url) return;
        release(entry);
        entry.failed = true;
        note.textContent = 'Изображение недоступно';
        retry.hidden = false;
      });
      entries.add(entry);
      owners.set(node, entry);
      observer.observe(node);
      return node;
    },
    unmount() {
      alive = false;
      observer.disconnect(); changes.disconnect();
      scroller.removeEventListener('scroll', pump);
      for (const entry of entries) { entry.controller?.abort(); release(entry); }
      entries.clear();
    },
  };
}
