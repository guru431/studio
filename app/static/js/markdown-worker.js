/* Только разбор текста в памяти. DOM и обязательная очистка остаются в странице. */
import '../vendor/marked.min.js';
import { visualizationExtension } from './visualization-markup.js';

const parser = new globalThis.marked.Marked({ gfm: true }).use({ extensions: [visualizationExtension] });

self.onmessage = ({ data }) => {
  try {
    self.postMessage({ id: data.id, tokens: parser.lexer(data.text) });
  } catch {
    self.postMessage({ id: data.id, tokens: null });
  }
};
