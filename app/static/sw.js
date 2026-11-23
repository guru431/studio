/* Служебный работник студии. `const VERSION` дописывает сервер в начало ответа /sw.js.
 *
 * В кэше — только оболочка на случай обрыва связи (страница «Нет связи», стили, иконки), и кладётся
 * она один раз при установке. Ответы на лету в этот кэш не кладутся никогда: API, модули js/* и
 * медиа — мимо него, из сети. Но HTTP-кэш браузера медиа держит: картинки и ролики идут с
 * `Cache-Control: private, max-age=86400` (media.py, иначе перемотка ролика качает его заново) и
 * остаются на диске устройства до суток; выход, «Удалить всю историю» и аварийный выход стирают HTTP-кэш
 * заголовком `Clear-Site-Data: "cache"` — этот кэш оболочки (Cache Storage) он не трогает. Навигация — из
 * сети, без связи и при 502/503/504 прокси — /offline.html. Новая версия удаляет кэш прежних. Ещё — уведомления: «В работе»
 * без подписки показывает открытая студия (jobs.js), Web Push заданий и «Кода» — обработчик `push` здесь; нажатие на
 * любое — notificationclick; смена подписки браузером — pushsubscriptionchange. Ещё два своих кэша устройства, не
 * оболочки (смена версии их не трогает): RECEIVED — принятое системным листом «Поделиться» до выбора раздела
 * (POST RECEIVE, share-target.js), BADGE — число на значке приложения (jobs.js). */
const CACHE = `studio-shell-${VERSION}`;
const RECEIVED = 'studio-received';
const RECEIVE = '/receive';
const RECEIVED_FILES = 10;
const BADGE = 'studio-badge';
const SHELL = [
  '/offline.html',
  `/static/css/app.css?v=${VERSION}`,
  `/static/theme.js?v=${VERSION}`,
  `/static/offline.js?v=${VERSION}`,
  `/static/en.js?v=${VERSION}`,
  `/static/i18n.js?v=${VERSION}`,
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
    if (studio) {
      const moved = await studio.navigate(target).catch(() => null);
      // окно без контроля работника (открыто жёсткой перезагрузкой) navigate отклоняет: не вторая вкладка, а фокус
      // и адрес сообщением (jobs.js); новое окно — только если и фокус не удался (окна уже нет)
      if (!moved && await studio.focus().then(() => true, () => false)) {
        studio.postMessage({ jobs: href });
        return;
      }
      studio = moved;
    }
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
  event.waitUntil(raiseBadge(tag).then(() => self.registration.showNotification(text(data.title) || 'Студия', {
    body: text(data.body), tag, ...(tag ? { renotify: true } : {}), icon: '/static/icons/icon-192.png',
    data: { href: text(data.href) } })));
});

/** Число на значке приложения при закрытой студии: к числу, которое последним поставила страница (jobs.js кладёт его
 * в кэш BADGE), — единица за новое уведомление; тот же tag (тот же чат или задание) уже на экране — не новое.
 * Открытая студия ставит точное число сама. Нет API значка или кэш закрыт — ничего: уведомление важнее. */
async function raiseBadge(tag) {
  try {
    if (!self.navigator || typeof self.navigator.setAppBadge !== 'function') return;
    const shown = tag ? await self.registration.getNotifications({ tag }) : [];
    const cache = await caches.open(BADGE);
    const hit = await cache.match('/badge');
    const next = (hit ? Number(await hit.text()) || 0 : 0) + (shown.length ? 0 : 1);
    await cache.put('/badge', new Response(String(next)));
    await self.navigator.setAppBadge(next);
  } catch {
    // значок — подсказка, а не данные
  }
}

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

/** Офлайн-оболочка по исходному адресу (у ответа из Cache Storage свой URL). reason — `data-reason` у <body>
 * (offline.js меняет слова); оболочки в кэше нет — fallback либо ошибка сети. */
async function shell(reason, fallback) {
  const page = await caches.match('/offline.html');
  if (!page) return fallback || Response.error();
  const body = reason ? (await page.text()).replace('<body class="solo">', `<body class="solo" data-reason="${reason}">`)
    : page.body;
  return new Response(body, { status: page.status, headers: page.headers });
}

/** Системный лист «Поделиться» (share_target манифеста): POST RECEIVE с файлами, текстом и ссылкой до сервера не идёт —
 * принятое ложится в кэш RECEIVED (прежнее непринятое заменяет), окно уходит на `/`. Студия спросит раздел при запуске,
 * в том числе после входа (share-target.js). Кривая форма — студия просто откроется. */
async function receive(request) {
  try {
    const form = await request.formData();
    const value = (name) => (typeof form.get(name) === 'string' ? form.get(name) : '');
    const files = form.getAll('files').filter((f) => typeof f === 'object' && f && f.size > 0).slice(0, RECEIVED_FILES);
    await caches.delete(RECEIVED);
    const cache = await caches.open(RECEIVED);
    await Promise.all(files.map((f, i) => cache.put(`/received/${i}`,
      new Response(f, { headers: { 'content-type': f.type || 'application/octet-stream' } }))));
    await cache.put('/received/meta', new Response(JSON.stringify({ at: Date.now(), title: value('title'),
      text: value('text'), url: value('url'), files: files.map((f) => ({ name: f.name || '', type: f.type || '' })) })));
  } catch {
    // ничего не принято: студия откроется как обычно
  }
  return Response.redirect(new URL('/', self.location.origin).href, 303);
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method === 'POST' && url.origin === self.location.origin && url.pathname === RECEIVE) {
    event.respondWith(receive(request));
    return;
  }
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate') {
    // 502/503/504 страницы — прокси без службы (выкладка, перезапуск): та же оболочка с «Повторить», что без связи,
    // со словами о перезапуске; она сама откроет адрес, когда служба ответит. Адреса API (файл в новой вкладке) —
    // как есть: их 503 — отказ самой службы.
    const pageAddress = !url.pathname.startsWith('/api/');
    event.respondWith(fetch(request).then((response) => (pageAddress && [502, 503, 504].includes(response.status)
      ? shell('restart', response) : response), () => shell()));
    return;
  }
  // API (и медиа под ним) и модули — только сеть, мимо кэша
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/static/js/')) return;
  if (SHELL.includes(url.pathname + url.search)) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
