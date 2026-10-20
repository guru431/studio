/* Блокировка на телефоне (спека 2026-10-21-studio-screen-lock): помощники passkey.
 *
 * iOS 16+: без PublicKeyCredential.parse*FromJSON и toJSON (они с iOS 18.4) — свои base64url.
 * navigator.credentials.* зовётся прямо в обработчике нажатия, без ожидания сети перед ним: Safari
 * иначе теряет «нажатие» и Face ID не показывает. Поэтому варианты (options) готовят заранее. */
import { api } from './api.js';

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
