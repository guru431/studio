/* Блокировка на телефоне (спека 2026-10-21-studio-screen-lock): шторка, экран блокировки, отметки
 * «на экране» и помощники passkey.
 *
 * Запирает сервер (auth.guard: 423 на всё /api/*, кроме запросов блокировки); здесь — экран и
 * согласие с сервером. Устройство с `pointer: coarse` шлёт отметку при старте, при возвращении и раз
 * в ALIVE_SEC, пока студия на экране. Ушла с экрана (при включённой блокировке) — сразу шторка;
 * вернулась через AWAY_SEC и больше — «заперто» серверу и экран блокировки, раньше — отметка решает,
 * убрать ли шторку. Любой ответ 423 ждёт одного общего отпирания (api.js hooks.locked) и повторяется.
 * Шторка и экран — <dialog> через showModal: поверх открытых окон просмотра, остальное — inert;
 * Escape их не закрывает (cancel отменён, а закрытое силой окно открывается снова).
 *
 * iOS 16+: без PublicKeyCredential.parse*FromJSON и toJSON (они с iOS 18.4) — свои base64url.
 * navigator.credentials.* зовётся прямо в обработчике нажатия, без ожидания сети перед ним: Safari
 * иначе теряет «нажатие» и Face ID не показывает. Поэтому варианты (options) готовят заранее. */
import { api, hooks, loadMe, me, useCsrf } from './api.js';
import { words } from './ui.js';

const AWAY_SEC = 60;
const ALIVE_SEC = 15;

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
let options = null;      // варианты входа passkey — готовы до нажатия «Открыть»
let prepared = 0;        // номер последней подготовки: поздний ответ прежней не перебьёт новую

function say(text, kind) {
  note.textContent = text || '';
  note.classList.toggle('error', kind === 'error');
}

function cover(on) {
  covering = on;
  if (on && !curtain.open) curtain.showModal();
  else if (!on && curtain.open) curtain.close();
}

function guarded() {
  return isTouch() && !!me && !!me.screen_lock;
}

/** Отметка «на экране»; true — сервер считает студию запертой (тогда экран блокировки), null — нет связи. */
async function mark() {
  if (!isTouch() || waiting) return null;
  try {
    const { locked } = await api('POST', '/api/lock/alive', { touch: true });
    if (locked) waitOpen();
    return locked;
  } catch {
    return null;         // нет связи: без отметок сервер запрёт сам через 75 с
  }
}

async function prepare() {
  const mine = ++prepared;
  options = null;
  delete openButton.dataset.ready;
  try {
    const ready = requestOptions(await api('POST', '/api/lock/challenge'));
    if (mine !== prepared) return;
    options = ready;
    openButton.dataset.ready = '1';
  } catch (err) {
    if (mine !== prepared) return;
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
  options = null;
  delete openButton.dataset.ready;
  openButton.hidden = false;
  usePassword.hidden = false;
  passwordForm.hidden = true;
  if (!screen.open) screen.showModal();
  const trusted = lockNow ? await lockNow : true;
  let state;
  try {
    state = await api('GET', '/api/lock');
  } catch (err) {
    keys = true;                       // неизвестно: «Открыть» попробует подготовить вход ещё раз
    say(words(err), 'error');
    return;
  }
  useCsrf(state.csrf);
  if (!state.locked && trusted) {
    await opened();
    return;
  }
  keys = state.keys;
  if (keys) await prepare();
  else offerPassword();
}

function waitOpen(lockNow = null) {
  if (!waiting) {
    let resolve;
    waiting = { promise: new Promise((done) => { resolve = done; }), resolve };
    showLock(lockNow);
  }
  return waiting.promise;
}

async function opened() {
  const done = waiting;
  waiting = null;                      // до loadMe: его 423 начнёт новое ожидание, а не будет ждать это
  try {
    await loadMe();
  } catch {
    // нет связи — профиль прежний
  }
  if (!waiting) screen.close();
  cover(false);
  passwordInput.value = '';
  if (done) done.resolve();
}

async function openWithPasskey() {
  if (!options) {                      // вариантов нет (не было связи) — готовим, нажать ещё раз
    say('');
    prepare();
    return;
  }
  const pending = navigator.credentials.get({ publicKey: options });   // сразу: до любого запроса (Safari)
  options = null;
  delete openButton.dataset.ready;
  say('');
  let credential;
  try {
    credential = await pending;
  } catch {
    say('Устройство не подтвердило — нажмите «Открыть» ещё раз', 'error');
    prepare();
    return;
  }
  try {
    await api('POST', '/api/lock/open', { credential: credentialJSON(credential) });
  } catch (err) {
    say(words(err), 'error');
    prepare();
    return;
  }
  await opened();
}

async function openWithPassword(event) {
  event.preventDefault();
  const go = document.getElementById('lock-password-go');
  go.disabled = true;
  try {
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
    if (guarded() && !waiting) {
      hiddenAt = Date.now();
      cover(true);
    }
    return;
  }
  const since = hiddenAt;
  hiddenAt = null;
  if (waiting) {                       // уже заперта: варианты passkey могли истечь, пока экран был погашен
    if (keys && screen.open) prepare();
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
  if (!locked) cover(false);           // «не заперто» или нет связи — шторку убрать
}

async function begin() {
  if (!isTouch()) return;
  try {
    const state = await api('GET', '/api/lock');      // CSRF до профиля: отметка — запрос POST
    useCsrf(state.csrf);
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
    if (document.visibilityState === 'visible') mark();
  }, ALIVE_SEC * 1000);
  begin();
}
