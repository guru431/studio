/* Только разбор текста в памяти. DOM и обязательная очистка остаются в странице. */
import '../vendor/marked.min.js';

self.onmessage = ({ data }) => {
  try {
    self.postMessage({ id: data.id, tokens: globalThis.marked.lexer(data.text, { gfm: true }) });
  } catch {
    self.postMessage({ id: data.id, tokens: null });
  }
};
