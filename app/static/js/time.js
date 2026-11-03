/* Все даты интерфейса — в часовом поясе учётки; серверные отметки остаются секундами Unix. */
import { me } from './api.js';

export const DEFAULT_TIMEZONE = 'Europe/Moscow';
export const timeZone = () => me?.timezone || DEFAULT_TIMEZONE;

export function dateTime(stamp, options = {}) {
  return new Date(stamp * 1000).toLocaleString('ru-RU', { ...options, timeZone: timeZone() });
}

export function dateOnly(stamp, options = {}) {
  return new Date(stamp * 1000).toLocaleDateString('ru-RU', { ...options, timeZone: timeZone() });
}

export function timeOnly(stamp) {
  return new Date(stamp * 1000).toLocaleTimeString('ru-RU',
    { hour: '2-digit', minute: '2-digit', timeZone: timeZone() });
}

/** Календарные сутки считаем по дате выбранного пояса: сутки с переходом часов бывают не по 24 часа. */
export function calendarDay(stamp) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: timeZone(),
    year: 'numeric', month: 'numeric', day: 'numeric' }).formatToParts(new Date(stamp * 1000))
    .filter((part) => part.type !== 'literal').map((part) => [part.type, Number(part.value)]));
  return { ...parts, number: Date.UTC(parts.year, parts.month - 1, parts.day) / 86400000 };
}
