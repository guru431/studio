/* Служебный работник студии. `const VERSION` дописывает сервер в начало ответа /sw.js.
 *
 * В кэше — только оболочка на случай обрыва связи (страница «Нет связи», стили, иконки), и кладётся
 * она один раз при установке. Ответы на лету в этот кэш не кладутся никогда: API, модули js/* и
 * медиа — мимо него, из сети. Но HTTP-кэш браузера медиа держит: картинки и ролики идут с
 * `Cache-Control: private, max-age=86400` (media.py, иначе перемотка ролика качает его заново) и
 * остаются на диске устройства до суток — и после «Удалить всю историю» и выхода. Навигация — из
 * сети, без связи — /offline.html. Новая версия удаляет кэш прежних. Ещё — уведомления: «В работе»
 * без подписки показывает открытая студия (jobs.js), Web Push заданий и «Кода» — обработчик `push` здесь; нажатие на
 * любое — notificationclick; смена подписки браузером — pushsubscriptionchange. */
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

/* Нажатие на уведомление «В работе» (jobs.js): открытое окно студии — на передний план и адрес записи
 * ему сообщением (оболочка переходит сама), окна нет — новое прямо на записи. */
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const href = (event.notification.data && event.notification.data.href) || '';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
    const studio = windows.find((w) => new URL(w.url).pathname === '/');
    if (!studio) return self.clients.openWindow(`/#/${href}`);
    return studio.focus().catch(() => studio).then(() => studio.postMessage({ jobs: href }));
  }));
});

/* Push заданий и «Кода» (push.py): заголовок, тело, tag и адрес —
 * из расшифрованной браузером полезной нагрузки; кривая — уведомление «Студия» без текста (userVisibleOnly:
 * показать нужно всегда). Нажатие — notificationclick выше. Тот же tag (один чат) заменяет прежнее уведомление —
 * renotify: снова звук и вибрация, иначе «ход окончен» молча заменил бы непрочитанное «ждёт подтверждения»;
 * без tag renotify бросает TypeError, поэтому только с ним. */
self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {};
  }
  if (!data || typeof data !== 'object') data = {};
  const text = (value) => (typeof value === 'string' ? value : '');
  const tag = text(data.tag);
  event.waitUntil(self.registration.showNotification(text(data.title) || 'Студия', {
    body: text(data.body), tag, ...(tag ? { renotify: true } : {}), icon: '/static/icons/icon-192.png',
    data: { href: text(data.href) } }));
});

/* Браузер сменил подписку push сам (служба push её обновила или отозвала): новую — серверу той же сессии, иначе
 * push тихо кончится. Cookie сессии запрос работника несёт сам; CSRF — из GET /api/me, как у страницы. Нет новой
 * подписки в событии — подписаться заново ключом VAPID сервера. Не дошла (сессия кончилась или заперта, сети нет) —
 * новую снять: флажок «Настроек» сверяется с подпиской браузера (settings.js pushBox) и покажет, что push выключен. */
self.addEventListener('pushsubscriptionchange', (event) => {
  const json = (r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  };
  event.waitUntil((async () => {
    let sub = event.newSubscription || null;
    try {
      if (!sub) {
        const { key } = await fetch('/api/me/push').then(json);
        sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      }
      const { csrf } = await fetch('/api/me').then(json);
      const r = await fetch('/api/me/push', { method: 'POST', body: JSON.stringify(sub.toJSON()),
        headers: { 'content-type': 'application/json', 'x-studio-csrf': csrf } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch {
      if (sub) await sub.unsubscribe().catch(() => false);
    }
  })());
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
