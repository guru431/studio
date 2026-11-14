import { tr } from './i18n.js';
/* Shared by the page and Markdown worker. Code spans/fences stay literal. */
const START = 'visualize';
const END = '';

export function visualizationReference(value) {
  if (!value || typeof value.path !== 'string' || value.path.length > 1024
      || !/\.html$/i.test(value.path) || /[\x00-\x1f\x7f]/.test(value.path)) return null;
  return { path: value.path, mode: value.mode === 'wide' ? 'wide' : 'normal',
    title: typeof value.title === 'string' ? value.title.slice(0, 120) : '' };
}

export const visualizationExtension = {
  name: 'studioVisualization', level: 'inline',
  start(source) { return source.indexOf(START); },
  tokenizer(source) {
    if (!source.startsWith(START)) return;
    const end = source.indexOf(END, START.length);
    if (end < 0 || end > 4096) return;
    try {
      const reference = visualizationReference(JSON.parse(source.slice(START.length, end)));
      if (reference) return { type: 'studioVisualization', raw: source.slice(0, end + END.length), reference };
    } catch { /* Partial and malformed references remain text. */ }
  },
  renderer({ reference }) {
    const value = JSON.stringify(reference).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
    return `<a href="#studio-visualization" title="${value}">${tr("Открыть визуализацию")}</a>`;
  },
};
