/* Блокировка на телефоне (спека 2026-10-21-studio-screen-lock): шторка, экран блокировки, отметки
 * «на экране» и помощники passkey.
 *
 * Запирает сервер (auth.guard: 423 на всё /api/*, кроме запросов блокировки); здесь — экран и
 * согласие с сервером. Видимая вкладка шлёт отметку при старте, при возвращении и раз в ALIVE_SEC
 * (touch — основной указатель палец: сервер защёлкивает по нему сессию как мобильную). Отметки шлёт
 * и компьютер: сессия, защёлкнутая в режиме планшета, иначе запиралась бы посреди работы. Ушла с
 * экрана (телефон, блокировка включена) — сразу шторка; вернулась через AWAY_SEC и больше —
 * «заперто» серверу и экран блокировки, раньше — отметка решает, убрать ли шторку. Со скрытой
 * студии шторка не снимается никогда. Любой ответ 423 ждёт одного общего отпирания (api.js
 * hooks.locked) и повторяется.
 * Шторка и экран — <dialog> через showModal: поверх открытых окон просмотра, остальное — inert;
 * Escape их не закрывает (cancel отменён, closedby="none", а закрытое силой окно открывается снова);
 * пока заперто или под шторкой, <html class="shrouded"> прячет оболочку и в миг до нового showModal.
 * Отметка без ответа дольше MARK_SEC — как без связи. Под шторкой запись голоса отменяется
 * (mic.js), при запирании плееры встают на паузу; после отпирания картинки и плееры /api/media/…,
 * получившие 423 мимо api.js, грузятся заново. CSRF до профиля — из GET /api/lock; не пришёл
 * (холодный старт без связи) — отметка, подготовка входа и пароль берут его заново.
 *
 * iOS 16+: без PublicKeyCredential.parse*FromJSON и toJSON (они с iOS 18.4) — свои base64url.
 * navigator.credentials.* зовётся прямо в обработчике нажатия, без ожидания сети перед ним: Safari
 * иначе теряет «нажатие» и Face ID не показывает. Поэтому варианты (options) готовят заранее и
 * заново, пока они не старше FRESH_SEC (вызов на сервере живёт 120 с): по таймеру отметок и при
 * возвращении на экран, а не по нажатию. Пока готовятся или идёт вызов устройства, «Открыть»
 * выключена («Готовлю…»), а подготовки не начинаются: новый вызов заменил бы подписываемый.
 *
 * PIN (спека 2026-10-28-studio-lock-pin): GET /api/lock дал pin и pin_left > 0 — цифровая панель вместо
 * поля пароля (кнопки страницы, не поле: системная клавиатура и менеджер паролей молчат; цифры,
 * Backspace и Enter с клавиатуры — тоже). Набранное — только в переменной digits, стирается после каждой
 * попытки, отпирания и ухода с экрана. Пока идёт запрос, панель нажатий не принимает. Исчерпан
 * (pin_exhausted, left 0, pin_left 0) или убран на другом устройстве (no_pin) — панель уходит, остаётся пароль.
 * GET /api/lock не пришёл при показе экрана — состояние перечитывают возвращение, таймер отметок и «Открыть»
 * (stale), а не подготовка passkey: иначе no_keys учётки с PIN увёл бы к паролю. */
import { api, hooks, loadMe, me, useCsrf } from './api.js';
import { cancelRecording } from './mic.js';
import { plural, words } from './ui.js';

const AWAY_SEC = 60;
const ALIVE_SEC = 15;
const MARK_SEC = 8;
/** Вызов passkey живёт на сервере 120 с (screenlock.CHALLENGE_SEC): варианты старше — готовить заново. */
export const FRESH_SEC = 100;
const PIN_MIN = 4;
const PIN_MAX = 8;
const PIN_WARN = 5;                    // с этого остатка подпись называет число попыток
const EXHAUSTED = 'PIN не принимается после 10 ошибок — откройте паролем учётки';     // = screenlock pin_exhausted

/** «осталась 1 попытка», «осталось 3 попытки», «осталось 5 попыток». */
export function attemptsLeft(n) {
  return `${plural(n, ['осталась', 'осталось', 'осталось'])} ${n} ${plural(n, ['попытка', 'попытки', 'попыток'])}`;
}

/** Основной указатель — палец: телефон или планшет (не User-Agent: его меняет «версия для ПК»). */
export function isTouch() {
  return matchMedia('(pointer: coarse)').matches;
}

/** Название ключа по устройству — из screenlock.NAMES; iPad по умолчанию представляется Mac. */
export function deviceName() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return 'iPad';
  if (/Android/.test(ua)) return 'Android';
  return 'Устройство';
}

export function b64u(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let text = '';
  for (const b of view) text += String.fromCharCode(b);
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function unb64u(text) {
  const base = text.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base + '='.repeat((4 - (base.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

const ids = (list) => (list || []).map((c) => ({ ...c, id: unb64u(c.id) }));

/** JSON вариантов создания с сервера → объект для navigator.credentials.create({ publicKey }). */
export function creationOptions(json) {
  return { ...json, challenge: unb64u(json.challenge), user: { ...json.user, id: unb64u(json.user.id) },
    excludeCredentials: ids(json.excludeCredentials) };
}

/** JSON вариантов входа с сервера → объект для navigator.credentials.get({ publicKey }). */
export function requestOptions(json) {
  return { ...json, challenge: unb64u(json.challenge), allowCredentials: ids(json.allowCredentials) };
}

/** Ответ устройства → JSON для сервера (байты — base64url без `=`). */
export function credentialJSON(credential) {
  const r = credential.response;
  const response = { clientDataJSON: b64u(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = b64u(r.attestationObject);
    response.transports = typeof r.getTransports === 'function' ? r.getTransports() : [];
  } else {
    response.authenticatorData = b64u(r.authenticatorData);
    response.signature = b64u(r.signature);
    response.userHandle = r.userHandle ? b64u(r.userHandle) : null;
  }
  return { id: credential.id, rawId: b64u(credential.rawId), type: credential.type, response,
    clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {} };
}

/** Варианты создания ключа (пароль учётки проверяет сервер) — заранее, до нажатия «Добавить». */
export async function passkeyOptions(password) {
  return creationOptions(await api('POST', '/api/passkeys/options', { password }));
}

/** Создать ключ устройства и отдать серверу. Звать прямо из обработчика нажатия: create — до первого await. */
export async function addPasskey(options) {
  const credential = await navigator.credentials.create({ publicKey: options });
  return api('POST', '/api/passkeys', { credential: credentialJSON(credential), name: deviceName() });
}

/* ---- шторка и экран блокировки ---------------------------------------------------- */

let curtain;
let screen;
let openButton;
let usePassword;
let usePin;
let passwordForm;
let passwordInput;
let note;
let waiting = null;      // { promise, resolve }: одно ожидание отпирания на все 423
let covering = false;
let hiddenAt = null;     // Date.now() ухода с экрана под шторку
let keys = false;
let token = false;       // CSRF сессии взят из GET /api/lock (профиля может ещё не быть)
let options = null;      // варианты входа passkey — готовы до нажатия «Открыть»
let optionsAt = 0;       // Date.now() их выдачи
let preparing = null;    // идущая подготовка: нажатия и возвращения её не перезапускают
let calling = false;     // идёт navigator.credentials.get и его проверка: нажатия и подготовки — мимо
let pinPad;
let pinDots;
let pinOk;
let pinOn = false;       // у учётки задан PIN и попытки остались — панель вместо пароля
let digits = '';         // набранные цифры: только здесь, ни в DOM, ни в хранилище
let pinGoing = false;    // идёт POST /api/lock/pin: нажатия, «OK» и Enter — мимо
let stale = false;       // GET /api/lock при показе экрана не пришёл: ключи и PIN неизвестны — перечитать
let rereading = null;    // идущее перечитывание: возвращение, таймер и «Открыть» его не повторяют
let reading = null;      // старт оболочки и её 423 используют один запрос состояния

function say(text, kind) {
  note.textContent = text || '';
  note.classList.toggle('error', kind === 'error');
}

/** Пока заперто или под шторкой — <html class="shrouded">: содержимое скрыто (app.css) и в миг между
 * закрытием окна силой и его открытием снова. */
function shroud() {
  document.documentElement.classList.toggle('shrouded', covering || !!waiting);
}

function cover(on) {
  covering = on;
  if (on) cancelRecording();           // ушла с экрана — микрофон не слушает под шторкой
  if (on && !curtain.open) curtain.showModal();
  else if (!on && curtain.open) curtain.close();
  shroud();
}

function guarded() {
  return isTouch() && !!me && !!me.screen_lock;
}

/** Состояние блокировки и с ним CSRF сессии. */
function lockState() {
  if (!reading) reading = api('GET', '/api/lock').then((state) => {
    useCsrf(state.csrf);
    token = true;
    return state;
  }).finally(() => { reading = null; });
  return reading;
}

/** Отметка «на экране»; true — сервер считает студию запертой (тогда экран блокировки), null — нет связи
 * или ответа нет дольше MARK_SEC (у fetch своего срока нет, iOS ждёт минуту и больше). */
async function mark() {
  if (waiting) return null;
  try {
    if (!me && !token) await lockState();          // холодный старт без связи: без CSRF отметка получила бы 403
    const { locked } = await api('POST', '/api/lock/alive', { touch: isTouch() },
      { signal: AbortSignal.timeout(MARK_SEC * 1000) });
    if (locked) waitOpen();
    return locked;
  } catch {
    return null;         // нет связи: без отметок сервер запрёт сам через 75 с
  }
}

/** Варианты свежие: вызов на сервере проживёт ещё хотя бы 20 с. */
function fresh() {
  return !!options && Date.now() - optionsAt < FRESH_SEC * 1000;
}

/** «Открыть»: «Готовлю…» и выключена, пока готовятся варианты или идёт вызов; data-ready — варианты готовы. */
function paintOpen() {
  openButton.disabled = !!preparing || calling;
  openButton.textContent = preparing ? 'Готовлю…' : 'Открыть';
  if (options && !preparing && !calling) openButton.dataset.ready = '1';
  else delete openButton.dataset.ready;
}

/** Новые варианты входа. Идущую подготовку не перезапускает, во время вызова устройства — ничего:
 * новый вызов на сервере заменил бы тот, что подписывает устройство, и ответ получил бы отказ. */
function prepare() {
  if (!calling && !preparing) {
    options = null;
    preparing = load().finally(() => {
      preparing = null;
      paintOpen();
    });
  }
  paintOpen();
}

async function load() {
  try {
    if (!me && !token) await lockState();          // холодный старт без связи: CSRF ещё нет
    options = requestOptions(await api('POST', '/api/lock/challenge'));
    optionsAt = Date.now();
  } catch (err) {
    if (err.code === 'no_keys') {      // ключи удалили на другом устройстве
      keys = false;
      if (stale) await refresh();      // состояние экрана не пришло: может, у учётки есть PIN
      if (pinOn && passwordForm.hidden) openButton.hidden = true;       // панель PIN остаётся
      else offerPassword();
    } else say(words(err), 'error');
  }
}

/** Точки по числу набранных цифр (длина PIN странице неизвестна); «OK» — от PIN_MIN цифр; пока идёт
 * запрос — панель выключена. */
function paintPin() {
  pinDots.replaceChildren(...Array.from(digits, () => document.createElement('span')));
  for (const key of pinPad.querySelectorAll('button')) key.disabled = pinGoing;
  pinOk.disabled = pinGoing || digits.length < PIN_MIN;
}

function forgetDigits() {
  digits = '';
  paintPin();
}

function offerPin() {
  forgetDigits();
  openButton.hidden = !keys;
  pinPad.hidden = false;
  usePassword.hidden = false;
  passwordForm.hidden = true;
  usePin.hidden = true;
}

function offerPassword() {
  forgetDigits();
  pinPad.hidden = true;
  openButton.hidden = !keys;
  usePassword.hidden = true;
  passwordForm.hidden = false;
  usePin.hidden = !pinOn;
  passwordInput.focus();
}

/** PIN больше не открывает (исчерпан или убран): панель уходит, остаётся пароль (и passkey, если есть ключ). */
function pinGone(text) {
  pinOn = false;
  offerPassword();
  say(text, 'error');
}

/** Экран блокировки. lockNow — обещание запроса «заперто» (true — сервер согласился): пока он не
 * прошёл, ответу «не заперто» не верить — иначе обрыв связи открыл бы студию без ключа. */
async function showLock(lockNow, knownState) {
  say('');
  cancelRecording();
  for (const player of document.querySelectorAll('video, audio')) player.pause();
  options = null;
  paintOpen();
  pinOn = false;
  forgetDigits();
  pinPad.hidden = true;
  openButton.hidden = false;
  usePassword.hidden = false;
  passwordForm.hidden = true;
  usePin.hidden = true;
  stale = false;
  if (!screen.open) screen.showModal();
  const trusted = lockNow ? await lockNow : true;
  let state;
  try {
    state = knownState || await lockState();
  } catch (err) {
    keys = true;                       // неизвестно: «Открыть», возвращение и таймер перечитают состояние
    stale = true;
    say(words(err), 'error');
    return;
  }
  if (!state.locked && trusted) {
    await opened();
    return;
  }
  applyState(state);
}

/** Экран по состоянию блокировки: панель PIN, пароль, «Открыть» (passkey). Поле пароля, выбранное
 * «Открыть паролем», не отнимает. */
function applyState(state) {
  stale = false;
  keys = state.keys;
  pinOn = !!state.pin && state.pin_left > 0;
  if (!passwordForm.hidden) {
    openButton.hidden = !keys;
    usePin.hidden = !pinOn;
  } else if (pinOn) offerPin();
  else if (state.pin) pinGone(EXHAUSTED);
  else if (!keys) offerPassword();
  if (keys) prepare();
}

/** Перечитать состояние, не пришедшее при показе экрана: панель PIN появится, как только вернётся связь.
 * Экран не открывает — «не заперто» здесь не проверено запросом «заперто». */
function refresh() {
  if (!rereading) {
    rereading = (async () => {
      try {
        const state = await lockState();
        if (!waiting || !stale) return;
        say('');
        applyState(state);
      } catch (err) {
        say(words(err), 'error');
      }
    })().finally(() => { rereading = null; });
  }
  return rereading;
}

function waitOpen(lockNow = null, knownState = null) {
  if (!waiting) {
    let resolve;
    waiting = { promise: new Promise((done) => { resolve = done; }), resolve };
    shroud();
    showLock(lockNow, knownState);
  }
  return waiting.promise;
}

/** Картинки и плееры /api/media/…, получившие 423 во время блокировки (они идут мимо api.js), — заново. */
function reloadMedia() {
  for (const node of document.querySelectorAll('img, video, audio')) {
    if (!(node.getAttribute('src') || '').startsWith('/api/media/')) continue;
    if (node instanceof HTMLImageElement) {
      if (node.complete && node.naturalWidth === 0) node.src = node.getAttribute('src');
    } else if (node.error) node.load();
  }
}

async function opened() {
  const done = waiting;
  waiting = null;                      // до loadMe: его 423 начнёт новое ожидание, а не будет ждать это
  try {
    await loadMe();
  } catch {
    // нет связи — профиль прежний
  }
  if (!waiting) {
    screen.close();
    reloadMedia();
  }
  if (document.visibilityState === 'visible') cover(false);     // скрытая — под шторкой до возвращения
  shroud();
  passwordInput.value = '';
  forgetDigits();
  if (done) done.resolve();
}

async function openWithPasskey() {
  if (calling || preparing) return;    // кнопка выключена; на случай нажатия, пришедшего раньше перерисовки
  if (!options) {                      // вариантов нет (не было связи) — готовим, нажать ещё раз
    say('');
    if (stale) refresh();              // сначала состояние: у учётки может не быть ключей, а быть PIN
    else prepare();
    return;
  }
  const pending = navigator.credentials.get({ publicKey: options });   // сразу: до любого запроса (Safari)
  calling = true;
  options = null;
  paintOpen();
  say('');
  let credential = null;
  try {
    credential = await pending;
    await api('POST', '/api/lock/open', { credential: credentialJSON(credential) });
  } catch (err) {
    calling = false;
    say(credential ? words(err) : 'Устройство не подтвердило — нажмите «Открыть» ещё раз', 'error');
    prepare();
    return;
  }
  calling = false;
  paintOpen();
  await opened();
}

async function openWithPassword(event) {
  event.preventDefault();
  const go = document.getElementById('lock-password-go');
  go.disabled = true;
  try {
    if (!me && !token) await lockState();          // холодный старт без связи: CSRF ещё нет
    await api('POST', '/api/lock/password', { password: passwordInput.value });
  } catch (err) {
    say(words(err), 'error');
    return;
  } finally {
    go.disabled = false;
  }
  await opened();
}

function typeDigit(digit) {
  if (pinGoing || digits.length >= PIN_MAX) return;
  digits += digit;
  paintPin();
}

function eraseDigit() {
  if (pinGoing) return;
  digits = digits.slice(0, -1);
  paintPin();
}

/** «OK»: одна попытка за раз (повторное касание и Enter, пока идёт запрос, — мимо); цифры стираются сразу. */
async function openWithPin() {
  if (pinGoing || digits.length < PIN_MIN) return;
  const pin = digits;
  pinGoing = true;
  digits = '';
  paintPin();
  say('');
  try {
    if (!me && !token) await lockState();          // холодный старт без связи: CSRF ещё нет
    await api('POST', '/api/lock/pin', { pin });
  } catch (err) {
    pinGoing = false;
    paintPin();
    const left = err.detail ? err.detail.left : undefined;
    if (err.code === 'pin_exhausted' || (err.code === 'pin' && left === 0)) pinGone(EXHAUSTED);
    else if (err.code === 'no_pin') pinGone('');                // PIN убрали на другом устройстве
    else if (err.code === 'pin') say(left <= PIN_WARN ? `Неверный PIN — ${attemptsLeft(left)}` : 'Неверный PIN', 'error');
    else say(words(err), 'error');
    return;
  }
  pinGoing = false;
  paintPin();
  await opened();
}

/** Клавиатура, пока на экране панель: цифры и Backspace — панели при любом фокусе, кроме поля ввода
 * (поле пароля — его клавиши); Enter — PIN, только если фокус на панели или ни на чём (экран, body):
 * на «Открыть», «Открыть паролем», «Выйти» Enter нажимает кнопку. */
function onKey(event) {
  if (!screen.open || pinPad.hidden) return;
  if (event.target === passwordInput || (event.target.closest && event.target.closest('input, textarea'))) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const onPad = event.target === document.body || event.target === screen || pinPad.contains(event.target);
  if (/^[0-9]$/.test(event.key)) typeDigit(event.key);
  else if (event.key === 'Backspace') eraseDigit();
  else if (event.key === 'Enter' && onPad) openWithPin();
  else return;
  event.preventDefault();              // Enter не «нажимает» кнопку в фокусе (цифру или «OK» второй раз)
}

async function onVisibility() {
  if (document.visibilityState !== 'visible') {
    forgetDigits();                    // набранное не переживает уход с экрана
    if (guarded()) {                   // и при открытом экране блокировки: отпирание могло пройти уже без человека
      hiddenAt = Date.now();
      cover(true);
    }
    return;
  }
  const since = hiddenAt;
  hiddenAt = null;
  if (waiting) {                       // уже заперта: варианты passkey могли истечь, пока экран был погашен
    cover(false);
    if (stale && screen.open) refresh();          // состояние не пришло при показе экрана — перечитать
    else if (keys && screen.open && !fresh()) prepare();
    return;
  }
  if (since === null) {
    mark();
    return;
  }
  if (Date.now() - since >= AWAY_SEC * 1000) {
    waitOpen(api('POST', '/api/lock/now').then(() => true, () => false));
    return;
  }
  const locked = await mark();
  // «не заперто» или нет связи — шторку убрать, если студия всё ещё на экране и не заперлась
  if (!locked && !waiting && document.visibilityState === 'visible') cover(false);
}

async function begin() {
  try {
    const state = await lockState();                  // CSRF до профиля: отметка — запрос POST
    if (state.locked) waitOpen(null, state);
    else mark();
  } catch {
    // нет входа (api.js уводит на вход) или нет связи — отметка придёт по таймеру
  }
}

/** Оболочка зовёт до loadMe(): запертый старт ждёт отпирания внутри api.js. */
export function installLock() {
  if (screen) return;          // ранний lock-start.js и оболочка вызывают один раз
  curtain = document.getElementById('curtain');
  screen = document.getElementById('lock');
  openButton = document.getElementById('lock-open');
  usePassword = document.getElementById('lock-use-password');
  usePin = document.getElementById('lock-use-pin');
  passwordForm = document.getElementById('lock-password-form');
  passwordInput = document.getElementById('lock-password');
  note = document.getElementById('lock-status');
  pinPad = document.getElementById('lock-pin');
  pinDots = document.getElementById('lock-pin-dots');
  pinOk = document.getElementById('lock-pin-ok');
  hooks.locked = () => waitOpen();
  for (const dialog of [curtain, screen]) dialog.addEventListener('cancel', (event) => event.preventDefault());
  screen.addEventListener('close', () => { if (waiting) screen.showModal(); });
  curtain.addEventListener('close', () => { if (covering) curtain.showModal(); });
  openButton.addEventListener('click', openWithPasskey);
  usePassword.addEventListener('click', offerPassword);
  usePin.addEventListener('click', () => {
    if (!pinOn) return;
    passwordInput.value = '';
    say('');
    offerPin();
  });
  passwordForm.addEventListener('submit', openWithPassword);
  for (const key of pinPad.querySelectorAll('[data-digit]')) key.addEventListener('click', () => typeDigit(key.dataset.digit));
  document.getElementById('lock-pin-back').addEventListener('click', eraseDigit);
  pinOk.addEventListener('click', openWithPin);
  document.addEventListener('keydown', onKey);
  document.addEventListener('visibilitychange', onVisibility);
  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    mark();
    if (waiting && stale && screen.open) refresh();   // связь вернулась — панель PIN без нажатий
    // экран блокировки открыт давно: вызов на сервере истечёт раньше нажатия — готовить заранее, а не по нажатию (Safari)
    else if (waiting && keys && screen.open && options && !fresh()) prepare();
  }, ALIVE_SEC * 1000);
  begin();
}
