/* Служебный работник студии. `const VERSION` дописывает сервер в начало ответа /sw.js.
 *
 * В кэше — только оболочка на случай обрыва связи (страница «Нет связи», стили, иконки), и кладётся
 * она один раз при установке. Ответы на лету в кэш не кладутся никогда: API, модули js/* и медиа —
 * только из сети, промпты и картинки на диске устройства не остаются. Навигация — из сети, без
 * связи — /offline.html. Новая версия удаляет кэш прежних. */
const CACHE = `studio-shell-${VERSION}`;
const SHELL = [
  '/offline.html',
  `/static/css/app.css?v=${VERSION}`,
  `/static/theme.js?v=${VERSION}`,
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith('studio-shell-') && k !== CACHE)
      .map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/offline.html')));
    return;
  }
  // API (и медиа под ним) и модули — только сеть, мимо кэша
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/static/js/')) return;
  if (SHELL.includes(url.pathname + url.search)) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
