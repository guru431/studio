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

// A disappeared client cannot be navigated; open the same target in a new window.
{
  const stale = windowAt(`${origin}/`, { navigateFails: true });
  const studio = windowAt(target);
  assert.deepEqual(await click({ windows: [stale], opened: studio }), [target]);
  assert.equal(studio.url, target);
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

// Other origins and non-app pages are not reused.
{
  const foreign = windowAt('https://other.example/');
  const login = windowAt(`${origin}/login`);
  assert.deepEqual(await click({ windows: [foreign, login] }), [target]);
  assert.deepEqual(foreign.navigations, []);
  assert.deepEqual(login.navigations, []);
}
