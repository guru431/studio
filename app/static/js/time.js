/* Все даты интерфейса — в часовом поясе учётки; серверные отметки остаются секундами Unix. */
import { me } from './api.js';
import { locale, tr } from './i18n.js';

export const DEFAULT_TIMEZONE = 'Europe/Moscow';
export const timeZone = () => me?.timezone || DEFAULT_TIMEZONE;
const DAY_MS = 86400000;

export function dateTime(stamp, options = {}) {
  return new Date(stamp * 1000).toLocaleString(locale(), { ...options, timeZone: timeZone() });
}

export function dateOnly(stamp, options = {}) {
  return new Date(stamp * 1000).toLocaleDateString(locale(), { ...options, timeZone: timeZone() });
}

export function timeOnly(stamp) {
  return new Date(stamp * 1000).toLocaleTimeString(locale(),
    { hour: '2-digit', minute: '2-digit', timeZone: timeZone() });
}

/** Длительность в миллисекундах: «{0} с» (меньше 10 с — до десятой), «{0} мин {1} с», «{0} ч {1} мин». */
export function duration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return tr("нет данных");
  const seconds = ms / 1000;
  if (seconds < 60) {
    const value = seconds < 10 ? Math.floor(seconds * 10) / 10 : Math.floor(seconds);
    return tr("{0} с", value.toLocaleString(locale(), { maximumFractionDigits: 1 }));
  }
  const whole = Math.floor(seconds);
  if (whole < 3600) return tr("{0} мин {1} с", Math.floor(whole / 60), whole % 60);
  return tr("{0} ч {1} мин", Math.floor(whole / 3600), Math.floor(whole % 3600 / 60));
}

/** Календарные сутки считаем по дате выбранного пояса: сутки с переходом часов бывают не по 24 часа. */
export function calendarDay(stamp) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: timeZone(),
    year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(new Date(stamp * 1000))
    .filter((part) => part.type !== 'literal').map((part) => [part.type, Number(part.value)]));
  return { ...parts, number: Date.UTC(parts.year, parts.month - 1, parts.day) / DAY_MS };
}

/** Включительный период из календарных дат, оканчивающийся днём пояса учётки. */
export function calendarRange(days, stamp = Date.now() / 1000) {
  const last = calendarDay(stamp).number;
  const iso = (day) => new Date(day * DAY_MS).toISOString().slice(0, 10);
  return { since: iso(last - days + 1), until: iso(last) };
}
