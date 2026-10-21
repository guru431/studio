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
 * (холодный старт без связи) — подготовка входа и пароль берут его заново.
 *
 * iOS 16+: без PublicKeyCredential.parse*FromJSON и toJSON (они с iOS 18.4) — свои base64url.
 * navigator.credentials.* зовётся прямо в обработчике нажатия, без ожидания сети перед ним: Safari
 * иначе теряет «нажатие» и Face ID не показывает. Поэтому варианты (options) готовят заранее и
 * заново, пока они не старше FRESH_SEC (вызов на сервере живёт 120 с): по таймеру отметок и при
 * возвращении на экран, а не по нажатию. Пока готовятся или идёт вызов устройства, «Открыть»
 * выключена («Готовлю…»), а подготовки не начинаются: новый вызов заменил бы подписываемый. */
import { api, hooks, loadMe, me, useCsrf } from './api.js';
import { cancelRecording } from './mic.js';
import { words } from './ui.js';

const AWAY_SEC = 60;
const ALIVE_SEC = 15;
const MARK_SEC = 8;
/** Вызов passkey живёт на сервере 120 с (screenlock.CHALLENGE_SEC): варианты старше — готовить заново. */
export const FRESH_SEC = 100;

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
async function lockState() {
  const state = await api('GET', '/api/lock');
  useCsrf(state.csrf);
  token = true;
  return state;
}

/** Отметка «на экране»; true — сервер считает студию запертой (тогда экран блокировки), null — нет связи
 * или ответа нет дольше MARK_SEC (у fetch своего срока нет, iOS ждёт минуту и больше). */
async function mark() {
  if (waiting) return null;
  try {
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
      offerPassword();
    } else say(words(err), 'error');
  }
}

function offerPassword() {
  openButton.hidden = !keys;
  usePassword.hidden = true;
  passwordForm.hidden = false;
  passwordInput.focus();
}

/** Экран блокировки. lockNow — обещание запроса «заперто» (true — сервер согласился): пока он не
 * прошёл, ответу «не заперто» не верить — иначе обрыв связи открыл бы студию без ключа. */
async function showLock(lockNow) {
  say('');
  cancelRecording();
  for (const player of document.querySelectorAll('video, audio')) player.pause();
  options = null;
  paintOpen();
  openButton.hidden = false;
  usePassword.hidden = false;
  passwordForm.hidden = true;
  if (!screen.open) screen.showModal();
  const trusted = lockNow ? await lockNow : true;
  let state;
  try {
    state = await lockState();
  } catch (err) {
    keys = true;                       // неизвестно: «Открыть» попробует подготовить вход ещё раз
    say(words(err), 'error');
    return;
  }
  if (!state.locked && trusted) {
    await opened();
    return;
  }
  keys = state.keys;
  if (keys) prepare();
  else offerPassword();
}

function waitOpen(lockNow = null) {
  if (!waiting) {
    let resolve;
    waiting = { promise: new Promise((done) => { resolve = done; }), resolve };
    shroud();
    showLock(lockNow);
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
  if (done) done.resolve();
}

async function openWithPasskey() {
  if (calling || preparing) return;    // кнопка выключена; на случай нажатия, пришедшего раньше перерисовки
  if (!options) {                      // вариантов нет (не было связи) — готовим, нажать ещё раз
    say('');
    prepare();
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

async function onVisibility() {
  if (document.visibilityState !== 'visible') {
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
    if (keys && screen.open && !fresh()) prepare();
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
    if (state.locked) waitOpen();
    else mark();
  } catch {
    // нет входа (api.js уводит на вход) или нет связи — отметка придёт по таймеру
  }
}

/** Оболочка зовёт до loadMe(): запертый старт ждёт отпирания внутри api.js. */
export function installLock() {
  curtain = document.getElementById('curtain');
  screen = document.getElementById('lock');
  openButton = document.getElementById('lock-open');
  usePassword = document.getElementById('lock-use-password');
  passwordForm = document.getElementById('lock-password-form');
  passwordInput = document.getElementById('lock-password');
  note = document.getElementById('lock-status');
  hooks.locked = () => waitOpen();
  for (const dialog of [curtain, screen]) dialog.addEventListener('cancel', (event) => event.preventDefault());
  screen.addEventListener('close', () => { if (waiting) screen.showModal(); });
  curtain.addEventListener('close', () => { if (covering) curtain.showModal(); });
  openButton.addEventListener('click', openWithPasskey);
  usePassword.addEventListener('click', offerPassword);
  passwordForm.addEventListener('submit', openWithPassword);
  document.addEventListener('visibilitychange', onVisibility);
  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    mark();
    // экран блокировки открыт давно: вызов на сервере истечёт раньше нажатия — готовить заранее, а не по нажатию (Safari)
    if (waiting && keys && screen.open && options && !fresh()) prepare();
  }, ALIVE_SEC * 1000);
  begin();
}
