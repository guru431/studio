/* Служебный работник студии. `const VERSION` дописывает сервер в начало ответа /sw.js.
 *
 * В кэше — только оболочка на случай обрыва связи (страница «Нет связи», стили, иконки), и кладётся
 * она один раз при установке. Ответы на лету в этот кэш не кладутся никогда: API, модули js/* и
 * медиа — мимо него, из сети. Но HTTP-кэш браузера медиа держит: картинки и ролики идут с
 * `Cache-Control: private, max-age=86400` (media.py, иначе перемотка ролика качает его заново) и
 * остаются на диске устройства до суток; выход, «Удалить всю историю» и аварийный выход стирают HTTP-кэш
 * заголовком `Clear-Site-Data: "cache"` — этот кэш оболочки (Cache Storage) он не трогает. Навигация — из
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

/* Адрес push должен попасть в URL до focus: на iPhone просыпающаяся страница может ещё не слушать
 * postMessage. Переход не зависит от готовности оболочки и переживает экран блокировки.
 * При холодном запуске Safari может вернуть окно на стартовой странице — направляем и его. */
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const value = event.notification.data && event.notification.data.href;
  const href = typeof value === 'string' ? value : '';
  const target = new URL(`/#/${href}`, self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    let studio = windows.find((w) => {
      const url = new URL(w.url);
      return url.origin === self.location.origin && url.pathname === '/';
    });
    if (studio) studio = await studio.navigate(target).catch(() => null);
    if (!studio) {
      studio = await self.clients.openWindow(target);
      if (studio && studio.url !== target) studio = await studio.navigate(target).catch(() => studio);
    }
    if (studio) {
      await studio.focus().catch(() => null);
      studio.postMessage({ jobs: href });       // запасной переход и обновление уже открытой записи
    }
  })());
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
 * подписки в событии — подписаться заново ключом VAPID сервера. Студия заперта (423: запертой сессии смена подписки
 * закрыта — иначе она увела бы уведомления) — подписку оставить и сказать открытым окнам: страница (push-sync.js)
 * сверит её с сервером и отправит после отпирания, а без открытого окна — при следующем запуске. Иной отказ (сессия
 * кончилась, сети нет) — новую снять: флажок «Настроек» сверяется с подпиской браузера (settings.js pushBox). */
self.addEventListener('pushsubscriptionchange', (event) => {
  const check = (r) => {
    if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
    return r;
  };
  event.waitUntil((async () => {
    let sub = event.newSubscription || null;
    try {
      if (!sub) {
        const { key } = await fetch('/api/me/push').then(check).then((r) => r.json());
        sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      }
      const { csrf } = await fetch('/api/me').then(check).then((r) => r.json());
      check(await fetch('/api/me/push', { method: 'POST', body: JSON.stringify(sub.toJSON()),
        headers: { 'content-type': 'application/json', 'x-studio-csrf': csrf } }));
    } catch (err) {
      if (err && err.status === 423) {
        const windows = await self.clients.matchAll({ type: 'window' }).catch(() => []);
        for (const w of windows) w.postMessage({ push: 'resync' });
        return;
      }
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
