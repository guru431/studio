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

// Other origins and non-app pages are not reused.
{
  const foreign = windowAt('https://other.example/');
  const login = windowAt(`${origin}/login`);
  assert.deepEqual(await click({ windows: [foreign, login] }), [target]);
  assert.deepEqual(foreign.navigations, []);
  assert.deepEqual(login.navigations, []);
}
