/* Драйвер браузерного smoke: одна вкладка headless-браузера по DevTools Protocol.
 *
 * Задание — JSON на stdin, ответ — JSON на stdout; запускает его `tests/browser.py`.
 * Копия драйвера панели llm_routers (dashboard/tests/cdp_driver.mjs) на 2026-10-19.
 * Зависимостей нет: `fetch` и `WebSocket` встроены в node 22+.
 *
 * Задание: { port, url, width, height, mobile, touch, authenticator, init, timeout, timezone, steps, probe, writes }.
 * `init` — сценарий, который вкладка выполняет до скриптов каждой загруженной страницы (подмена fetch
 * с самого старта, в том числе после перезагрузки).
 * `writes` — пути, куда вкладке можно писать (запрос не GET); остальную запись
 * браузер обрывает сам. Нет поля — запись не ограничена.
 * `touch` — касание вместо мыши (`pointer: coarse`); `authenticator` — виртуальный ключ WebAuthn
 * (internal, проверка пользователя проходит сама). Ключ живёт во вкладке: добавленный в одном
 * задании в следующем не виден.
 * Шаги исполняются по порядку до первого отказа:
 *   ["idle"]               — страница загружена и запросов в полёте нет;
 *   ["wait", выражение]    — выражение стало истинным;
 *   ["eval", имя, выражение] — значение выражения в ответ под именем;
 *   ["click", селектор]    — нажатие мышью в середину элемента;
 *   ["type", селектор, текст] — нажатие на поле и ввод текста;
 *   ["press", клавиша]     — нажатие клавиши (Escape, Enter, Tab, Backspace, Delete, Home, End, стрелки влево и
 *                            вправо, цифры 0–9);
 *   ["screenshot", путь, селектор?] — снимок окна или элемента в PNG (глазами проверить вёрстку).
 * `probe` вычисляется всегда, и после отказа тоже: по нему видно, на чём
 * страница остановилась.
 *
 * Ответ: { values, probe, log, sent, failed, where }. В `log` — ошибки журнала браузера
 * (нарушения политики содержимого, отказы загрузки), необработанные
 * исключения и console.error. В `sent` — каждый запрос не GET, «МЕТОД путь»,
 * в том числе оборванный: отправку видно в миг запроса, а не по ответу.
 * `where` — только после отказа шага: где стояла страница (адрес, readyState, data-ready,
 * открытые окна) и пути запросов, которые ещё в полёте, — редкий отказ объясняет себя сам.
 */
import { writeFile } from 'node:fs/promises';
import process from 'node:process';

const POLL_MS = 50;
const IDLE_MS = 300;
const KEYS = { Escape: 27, Enter: 13, Tab: 9, Backspace: 8, Delete: 46, Home: 36, End: 35, ArrowLeft: 37, ArrowRight: 39,
  ...Object.fromEntries([...'0123456789'].map((d) => [d, 48 + Number(d)])) };

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function readJob() {
  let text = '';
  for await (const chunk of process.stdin) text += chunk;
  return JSON.parse(text);
}

/** Вкладка: команды с ответом по номеру и подписка на события протокола. */
class Tab {
  constructor(socket) {
    this.socket = socket;
    this.sent = 0;
    this.waiting = new Map();
    this.handlers = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id === undefined) {
        (this.handlers.get(message.method) || []).forEach((fn) => fn(message.params));
        return;
      }
      const { resolve, reject } = this.waiting.get(message.id);
      this.waiting.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = ++this.sent;
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, fn) {
    this.handlers.set(method, [...(this.handlers.get(method) || []), fn]);
  }

  async evaluate(expression) {
    const answer = await this.send('Runtime.evaluate',
      { expression, returnByValue: true, awaitPromise: true });
    if (answer.exceptionDetails) {
      const thrown = answer.exceptionDetails.exception;
      throw new Error((thrown && thrown.description) || answer.exceptionDetails.text);
    }
    return answer.result.value;
  }
}

async function openTab(port) {
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,
                                    { method: 'PUT' })).json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve);
    socket.addEventListener('error', () => reject(new Error('браузер не принял соединение DevTools')));
  });
  return { id: target.id, tab: new Tab(socket) };
}

async function until(check, timeout, what) {
  const deadline = Date.now() + timeout;
  for (;;) {
    if (await check()) return;
    if (Date.now() > deadline) throw new Error(`не дождались за ${timeout} мс: ${what}`);
    await sleep(POLL_MS);
  }
}

/* Нажатие настоящей мышью, а не `el.click()`: кнопка, накрытая соседним блоком,
 * сценарий обязана уронить. За край окна элемент прокручивается сам
 * (`scrollIntoView`) — падает только тот, что и после прокрутки не виден. Середина элемента
 * меряется, пока не перестанет двигаться: окно действия выезжает с анимацией,
 * и нажатие по первому замеру попадало мимо кнопки. */
async function click(tab, selector, timeout) {
  const measure = `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return { error: 'элемента нет на странице' };
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const box = el.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !el.contains(hit)) return { error: 'элемент перекрыт или вне окна' };
    return { x, y };
  })()`;
  const deadline = Date.now() + timeout;
  let spot = await tab.evaluate(measure);
  for (;;) {
    await sleep(POLL_MS);
    const next = await tab.evaluate(measure);
    if (!next.error && next.x === spot.x && next.y === spot.y) break;
    if (Date.now() > deadline) throw new Error(`${selector}: ${next.error || 'элемент не остановился'}`);
    spot = next;
  }
  for (const type of ['mousePressed', 'mouseReleased']) {
    await tab.send('Input.dispatchMouseEvent',
      { type, x: spot.x, y: spot.y, button: 'left', clickCount: 1 });
  }
}

async function press(tab, key) {
  if (!(key in KEYS)) throw new Error(`клавиша ${key} драйверу неизвестна`);
  for (const type of ['keyDown', 'keyUp']) {
    await tab.send('Input.dispatchKeyEvent',
      { type, key, code: key, windowsVirtualKeyCode: KEYS[key] });
  }
}

async function run(job) {
  const out = { values: {}, probe: null, log: [], sent: [], failed: null, where: null };
  const timeout = job.timeout || 30000;
  const { id, tab } = await openTab(job.port);
  try {
    // Запросы в полёте (id → «МЕТОД путь»). Поток событий не кончается никогда и «тишине» не мешает.
    const flying = new Map();
    let quietSince = Date.now();
    const landed = ({ requestId }) => { flying.delete(requestId); quietSince = Date.now(); };
    tab.on('Network.requestWillBeSent', ({ requestId, type, request }) => {
      const path = `${request.method} ${new URL(request.url).pathname}`;
      if (type !== 'EventSource') flying.set(requestId, path);
      if (request.method !== 'GET') out.sent.push(path);
    });
    if (job.writes) {
      // Живой прогон: запись мимо разрешённых путей не уходит из браузера вовсе.
      tab.on('Fetch.requestPaused', ({ requestId, request }) => {
        const allowed = request.method === 'GET' || job.writes.includes(new URL(request.url).pathname);
        tab.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest',
          allowed ? { requestId } : { requestId, errorReason: 'BlockedByClient' }).catch(() => {});
      });
      await tab.send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
    }
    tab.on('Network.loadingFinished', landed);
    tab.on('Network.loadingFailed', landed);
    tab.on('Log.entryAdded', ({ entry }) => {
      if (entry.level === 'error') out.log.push(`${entry.source}: ${entry.text}${entry.url ? ` (${entry.url})` : ''}`);
    });
    tab.on('Runtime.exceptionThrown', ({ exceptionDetails: details }) => {
      out.log.push(`исключение: ${(details.exception && details.exception.description) || details.text}`);
    });
    tab.on('Runtime.consoleAPICalled', ({ type, args }) => {
      if (type === 'error') out.log.push(`console.error: ${args.map((a) => a.value || a.description).join(' ')}`);
    });
    for (const domain of ['Page', 'Runtime', 'Log', 'Network']) await tab.send(`${domain}.enable`);
    await tab.send('Emulation.setDeviceMetricsOverride', {
      width: job.width || 1280, height: job.height || 900, deviceScaleFactor: 1, mobile: !!job.mobile,
    });
    if (job.touch) await tab.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
    if (job.timezone) await tab.send('Emulation.setTimezoneOverride', { timezoneId: job.timezone });
    if (job.authenticator) {
      await tab.send('WebAuthn.enable');
      await tab.send('WebAuthn.addVirtualAuthenticator', { options: { protocol: 'ctap2', transport: 'internal',
        hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
    }
    if (job.init) await tab.send('Page.addScriptToEvaluateOnNewDocument', { source: job.init });
    const opened = await tab.send('Page.navigate', { url: job.url });

    // Пока страница сменяется (вход, переход), выражение вычислить негде: это «ещё нет», а не отказ.
    const holds = (expression) => tab.evaluate(expression).catch(() => false);
    const idle = async () => (await holds("document.readyState === 'complete'"))
      && flying.size === 0 && Date.now() - quietSince >= IDLE_MS;
    try {
      // Неоткрывшийся адрес (отказ соединения, DNS) — сразу причиной, а не сроком первого шага.
      if (opened.errorText) throw new Error(`адрес не открылся: ${opened.errorText}`);
      for (const [kind, ...args] of job.steps || []) {
        if (kind === 'idle') await until(idle, timeout, 'страница не затихла');
        else if (kind === 'wait') await until(() => holds(args[0]), timeout, args[0]);
        else if (kind === 'eval') out.values[args[0]] = await tab.evaluate(args[1]);
        else if (kind === 'click') await click(tab, args[0], timeout);
        else if (kind === 'type') {
          await click(tab, args[0], timeout);
          await tab.send('Input.insertText', { text: args[1] });
        } else if (kind === 'press') await press(tab, args[0]);
        else if (kind === 'screenshot') {
          const clip = args[1] ? await tab.evaluate(`(() => {
            const r = document.querySelector(${JSON.stringify(args[1])}).getBoundingClientRect();
            return { x: r.x, y: r.y, width: r.width, height: r.height, scale: 1 };
          })()`) : null;
          const { data } = await tab.send('Page.captureScreenshot', { format: 'png', ...(clip ? { clip } : {}) });
          await writeFile(args[0], Buffer.from(data, 'base64'));
        }
        else throw new Error(`шаг ${kind} драйверу неизвестен`);
      }
    } catch (err) {
      out.failed = String(err.message || err);
      const page = await tab.evaluate(`({ href: location.href, readyState: document.readyState,
        ready: document.documentElement.dataset.ready || null,
        dialogs: [...document.querySelectorAll('dialog[open]')].map((d) => d.id || d.className) })`)
        .catch((e) => ({ unreadable: String(e.message || e) }));
      out.where = { ...page, flying: [...flying.values()].slice(0, 20) };
    }
    if (job.probe) {
      try { out.probe = await tab.evaluate(job.probe); } catch (err) { out.failed = out.failed || String(err.message || err); }
    }
  } finally {
    await fetch(`http://127.0.0.1:${job.port}/json/close/${id}`).catch(() => {});
  }
  return out;
}

process.stdout.write(JSON.stringify(await run(await readJob())));
process.exit(0);
