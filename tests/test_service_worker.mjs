import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../app/static/sw.js', import.meta.url), 'utf8');
const origin = 'https://studio.example';
const href = 'code/7/0123456789abcdef/a1111111-1111-4111-8111-111111111111';
const target = `${origin}/#/${href}`;

async function click({ windows = [], opened = null } = {}) {
  const handlers = new Map();
  const calls = [];
  const clients = {
    async matchAll(options) {
      assert.equal(options.type, 'window');
      assert.equal(options.includeUncontrolled, true);
      return windows;
    },
    async openWindow(url) {
      calls.push(url);
      return opened;
    },
  };
  vm.runInNewContext(source, {
    VERSION: 'test', URL,
    self: { location: { origin }, clients, addEventListener: (name, fn) => handlers.set(name, fn) },
  });
  let finished;
  let closed = false;
  handlers.get('notificationclick')({
    notification: { data: { href }, close() { closed = true; } },
    waitUntil(promise) { finished = promise; },
  });
  await finished;
  assert.equal(closed, true);
  return calls;
}

function windowAt(url, { navigateFails = false, focusFails = false } = {}) {
  return {
    url, messages: [], navigations: [], focused: false,
    // A resumed iPhone page has no message listener yet: postMessage loses the route.
    postMessage(message) { this.messages.push(message); },
    async navigate(destination) {
      this.navigations.push(destination);
      if (navigateFails) throw new TypeError('navigate failed');
      this.url = destination;
      return this;
    },
    async focus() {
      if (focusFails) throw new TypeError('focus failed');
      this.focused = true;
      return this;
    },
  };
}

// Warm resume: another project is open and messages are dropped until the page is ready.
{
  const studio = windowAt(`${origin}/#/code/7/fedcba9876543210`);
  assert.deepEqual(await click({ windows: [studio] }), []);
  assert.equal(studio.url, target);
  assert.equal(studio.focused, true);
}

// Cold start: open the full project/chat URL even with no existing window.
assert.deepEqual(await click(), [target]);

// Safari may return a newly opened PWA at its start page: navigate that client explicitly.
{
  const studio = windowAt(`${origin}/#/code`);
  assert.deepEqual(await click({ opened: studio }), [target]);
  assert.equal(studio.url, target);
}

// A disappeared client can be neither navigated nor focused; open the same target in a new window.
{
  const stale = windowAt(`${origin}/`, { navigateFails: true, focusFails: true });
  const studio = windowAt(target);
  assert.deepEqual(await click({ windows: [stale], opened: studio }), [target]);
  assert.equal(studio.url, target);
}

// An uncontrolled window (opened by a hard reload) refuses navigate: focus it and route by message, no second tab.
{
  const uncontrolled = windowAt(`${origin}/#/settings`, { navigateFails: true });
  assert.deepEqual(await click({ windows: [uncontrolled] }), []);
  assert.equal(uncontrolled.focused, true);
  assert.equal(JSON.stringify(uncontrolled.messages), JSON.stringify([{ jobs: href }]));
}

// A focus error must not prevent routing to the project.
{
  const studio = windowAt(`${origin}/#/settings`, { focusFails: true });
  assert.deepEqual(await click({ windows: [studio] }), []);
  assert.equal(studio.url, target);
}

// pushsubscriptionchange: a locked Studio (423) keeps the new subscription for the page to send after unlock;
// any other refusal drops it, as before.
async function resubscribe(status) {
  const handlers = new Map();
  const sent = [];
  const posted = [];
  let unsubscribed = false;
  const sub = { toJSON: () => ({ endpoint: 'https://web.push.apple.com/new' }), async unsubscribe() { unsubscribed = true; } };
  const fetch = async (url, options = {}) => {
    sent.push(`${options.method || 'GET'} ${url}`);
    if (url === '/api/me') return { ok: true, status: 200, json: async () => ({ csrf: 'c' }) };
    return { ok: status === 204, status, json: async () => ({}) };
  };
  const studio = { postMessage: (message) => posted.push(message) };
  vm.runInNewContext(source, {
    VERSION: 'test', URL, fetch, JSON,
    self: { location: { origin }, addEventListener: (name, fn) => handlers.set(name, fn),
      clients: { async matchAll(options) { assert.equal(options.type, 'window'); return [studio]; } } },
  });
  let finished;
  handlers.get('pushsubscriptionchange')({ newSubscription: sub, waitUntil(promise) { finished = promise; } });
  await finished;
  return { sent, posted, unsubscribed };
}

{
  const locked = await resubscribe(423);
  assert.deepEqual(locked.sent, ['GET /api/me', 'POST /api/me/push']);
  assert.equal(locked.unsubscribed, false);
  assert.equal(JSON.stringify(locked.posted), '[{"push":"resync"}]');
  const gone = await resubscribe(401);
  assert.equal(gone.unsubscribed, true);
  assert.deepEqual(gone.posted, []);
  const done = await resubscribe(204);
  assert.equal(done.unsubscribed, false);
  assert.deepEqual(done.posted, []);
}

/** Cache Storage в памяти: имя кэша → Map(ключ → Response). */
function memoryCaches(initial = {}) {
  const store = new Map(Object.entries(initial).map(([name, entries]) => [name, new Map(Object.entries(entries))]));
  return {
    store,
    async open(name) {
      if (!store.has(name)) store.set(name, new Map());
      const entries = store.get(name);
      return {
        async put(key, response) { entries.set(String(key), response); },
        async match(key) { const hit = entries.get(String(key)); return hit && hit.clone(); },
      };
    },
    async delete(name) { return store.delete(name); },
    async match() { return undefined; },
  };
}

// Share target: POST /receive never reaches the server — files, text and link go to the device cache, the window to /.
{
  const handlers = new Map();
  const caches = memoryCaches({ 'studio-received': { '/received/0': new Response('old') } });
  vm.runInNewContext(source, {
    VERSION: 'test', URL, Response, JSON, Date, caches,
    self: { location: { origin }, addEventListener: (name, fn) => handlers.set(name, fn) },
  });
  const form = new FormData();
  form.append('title', 'Заметка');
  form.append('text', 'текст');
  form.append('url', 'https://example.com/a');
  form.append('files', new File(['RIFF'], 'запись.m4a', { type: 'audio/mp4' }));
  form.append('files', new File([], 'empty.png', { type: 'image/png' }));       // пустой — не принят
  const request = new Request(`${origin}/receive`, { method: 'POST', body: form });
  let response;
  handlers.get('fetch')({ request, respondWith(promise) { response = promise; } });
  const got = await response;
  assert.equal(got.status, 303);
  assert.equal(got.headers.get('location'), `${origin}/`);
  const received = caches.store.get('studio-received');
  assert.deepEqual([...received.keys()].sort(), ['/received/0', '/received/meta']);
  assert.equal(await received.get('/received/0').text(), 'RIFF');
  assert.equal(received.get('/received/0').headers.get('content-type'), 'audio/mp4');
  const meta = await received.get('/received/meta').json();
  assert.equal(meta.title, 'Заметка');
  assert.equal(meta.text, 'текст');
  assert.equal(meta.url, 'https://example.com/a');
  assert.deepEqual(meta.files, [{ name: 'запись.m4a', type: 'audio/mp4' }]);
  // GET to the same path and foreign POSTs pass by
  let touched = false;
  handlers.get('fetch')({ request: { method: 'POST', url: `${origin}/api/chats`, mode: 'cors' },
    respondWith() { touched = true; } });
  assert.equal(touched, false);
}

// App badge from push: the page's last number plus one for a new notification; the same tag on screen is not new.
async function pushBadge({ stored, shownTags = [], tag }) {
  const handlers = new Map();
  const badges = [];
  const shown = [];
  const caches = memoryCaches(stored === undefined ? {} : { 'studio-badge': { '/badge': new Response(String(stored)) } });
  vm.runInNewContext(source, {
    VERSION: 'test', URL, Response, JSON, caches,
    self: {
      location: { origin }, addEventListener: (name, fn) => handlers.set(name, fn),
      navigator: { async setAppBadge(n) { badges.push(n); } },
      registration: {
        async getNotifications(options) { return shownTags.filter((t) => t === options.tag); },
        async showNotification(title, options) { shown.push([title, options.tag]); },
      },
    },
  });
  let finished;
  handlers.get('push')({ data: { json: () => ({ title: 'Студия: ролик готов', tag, href: 'video?id=v1' }) },
    waitUntil(promise) { finished = promise; } });
  await finished;
  return { badges, shown, stored: await caches.store.get('studio-badge')?.get('/badge')?.text() };
}

{
  const fresh = await pushBadge({ stored: 2, tag: 'v1' });
  assert.deepEqual(fresh.badges, [3]);
  assert.equal(fresh.stored, '3');
  assert.deepEqual(fresh.shown, [['Студия: ролик готов', 'v1']]);
  const again = await pushBadge({ stored: 2, shownTags: ['v1'], tag: 'v1' });
  assert.deepEqual(again.badges, [2]);
  assert.equal((await pushBadge({ tag: '' })).badges[0], 1);
}

// No badge API (Firefox, Safari outside the home screen): the notification still shows.
{
  const handlers = new Map();
  const shown = [];
  vm.runInNewContext(source, {
    VERSION: 'test', URL,
    self: { location: { origin }, addEventListener: (name, fn) => handlers.set(name, fn),
      registration: { async showNotification(title) { shown.push(title); } } },
  });
  let finished;
  handlers.get('push')({ data: null, waitUntil(promise) { finished = promise; } });
  await finished;
  assert.deepEqual(shown, ['Студия']);
}

// Other origins and non-app pages are not reused.
{
  const foreign = windowAt('https://other.example/');
  const login = windowAt(`${origin}/login`);
  assert.deepEqual(await click({ windows: [foreign, login] }), [target]);
  assert.deepEqual(foreign.navigations, []);
  assert.deepEqual(login.navigations, []);
}

// Navigation: the proxy answers 502/503/504 while the service restarts — the offline shell with "restarting" words
// at the same address; a refusal of an API address (a file opened in a new tab) and other answers pass as they are;
// no network — the plain offline shell.
async function navigate(path, answer) {
  const handlers = new Map();
  const offline = '<html><body class="solo"><p>offline</p></body></html>';
  const caches = { async match(key) {
    return key === '/offline.html' ? new Response(offline, { status: 200, headers: { 'content-type': 'text/html' } }) : undefined;
  } };
  const fetch = async () => {
    if (answer === 'network') throw new TypeError('Failed to fetch');
    return new Response('proxy', { status: answer });
  };
  vm.runInNewContext(source, {
    VERSION: 'test', URL, Response, fetch, caches,
    self: { location: { origin }, addEventListener: (name, fn) => handlers.set(name, fn) },
  });
  let response;
  handlers.get('fetch')({ request: { method: 'GET', mode: 'navigate', url: `${origin}${path}` },
    respondWith(promise) { response = promise; } });
  const got = await response;
  return [got.status, await got.text()];
}

{
  const restarting = [200, '<html><body class="solo" data-reason="restart"><p>offline</p></body></html>'];
  assert.deepEqual(await navigate('/', 503), restarting);
  assert.deepEqual(await navigate('/share', 502), restarting);
  assert.deepEqual(await navigate('/login', 504), restarting);
  assert.deepEqual(await navigate('/api/media/1', 503), [503, 'proxy']);
  assert.deepEqual(await navigate('/', 500), [500, 'proxy']);
  assert.deepEqual(await navigate('/', 'network'), [200, '<html><body class="solo"><p>offline</p></body></html>']);
}
