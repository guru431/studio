/* The synchronous head script selects the language before modules render. The Markdown worker
 * has no document/storage and only tokenizes visualization references. */
const fallback = (source, ...values) => source.replace(/\{(\d+)\}/g,
  (match, index) => index < values.length ? String(values[index]) : match);
export const tr = (...args) => globalThis.StudioI18n?.tr(...args) ?? fallback(...args);
export const message = (value) => globalThis.StudioI18n?.message(value) ?? String(value ?? '');
export const locale = () => globalThis.StudioI18n?.locale() ?? 'ru-RU';
export const language = () => globalThis.StudioI18n?.language() ?? 'ru';
export const languagePicker = (...args) => globalThis.StudioI18n.languagePicker(...args);
