import { tr } from './i18n.js';
/* Часовой пояс устройства против пояса учётки (по умолчанию — Москва у всех). При запуске, если сейчас их смещения
 * разные, над разделом — строка #timezone-offer: «Перейти на <пояс устройства>» (PATCH /api/me) или «Оставить».
 * «Оставить» запоминается на устройстве для этой учётки и этого пояса (localStorage `studio-timezone-offer:<id>`):
 * в поездке с другим поясом спросит снова. Хранилище закрыто — не спрашивает вовсе, иначе спрашивал бы на каждом
 * запуске. Строка живёт внутри раздела и уходит со сменой раздела. `offsetOf` и `deviceZone` — и для списка поясов
 * настроек (settings.js). */
import { api, me } from './api.js';
import { DEFAULT_TIMEZONE } from './time.js';
import { button, el, words } from './ui.js';

const KEY = 'studio-timezone-offer:';

/** Пояс браузера (IANA) или null. */
export function deviceZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

/** Смещение пояса сейчас: «UTC+05:00»; неизвестный пояс — ''. */
export function offsetOf(zone) {
  try {
    const name = new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'longOffset' })
      .formatToParts(Date.now()).find((part) => part.type === 'timeZoneName')?.value || '';
    return name === 'GMT' ? 'UTC+00:00' : name.replace('GMT', 'UTC');
  } catch {
    return '';
  }
}

/** Подпись пояса: Москва — словом, прочие — именем IANA. */
export function zoneName(zone) {
  return zone === DEFAULT_TIMEZONE ? tr("Москва") : zone;
}

/** Строка предложения или null (поясы совпадают, отказ уже запомнен, хранилища нет). */
export function timezoneOffer() {
  const device = deviceZone();
  const account = me.timezone || DEFAULT_TIMEZONE;
  if (!device || device === account || !offsetOf(device) || offsetOf(device) === offsetOf(account)) return null;
  const key = KEY + me.id;
  try {
    if (localStorage.getItem(key) === device) return null;
  } catch {
    return null;
  }
  const text = el('span', {}, tr("Время в Студии — по поясу {0} ({1}), а на этом устройстве — {2} ({3}).",
    zoneName(account), offsetOf(account), device, offsetOf(device)));
  const fail = el('span', { class: 'error' });
  const box = el('div', { id: 'timezone-offer', class: 'timezone-offer', role: 'status' }, text);
  const yes = button(tr("Перейти на {0}", device), { class: 'btn btn-accent btn-sm', id: 'timezone-offer-yes' }, async () => {
    yes.disabled = no.disabled = true;
    try {
      const profile = await api('PATCH', '/api/me', { timezone: device });
      me.timezone = profile.timezone;
      document.dispatchEvent(new Event('studio-me'));
      box.replaceChildren(el('span', {}, tr("Часовой пояс сохранён: {0}. Раздел покажет новое время, когда его откроют снова.", device)));
    } catch (err) {
      fail.textContent = words(err);
      yes.disabled = no.disabled = false;
    }
  });
  const no = button(tr("Оставить"), { class: 'btn btn-sm', id: 'timezone-offer-no' }, () => {
    try { localStorage.setItem(key, device); } catch { /* в следующий запуск спросит снова */ }
    box.remove();
  });
  box.append(yes, no, fail);
  return box;
}
