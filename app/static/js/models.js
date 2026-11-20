/* Подписи моделей шлюза — единственная таблица страницы: настройки и поля разделов (settings.js `modelLabel`),
 * цепочка и лента фото (chain.js), «Поиск» (search.js). Копия подписей картинок на сервере — `jobs.MODEL_NAMES`
 * (строки «В работе»), её сверяет tests/test_docs.py. Прочие модели (прямые подключения) — своим id. */
export const LABELS = { lustify: 'Lustify', flux: 'FLUX', wan: 'Wan', whisperx: 'WhisperX', 'qwen3-tts': 'Qwen3-TTS',
  'f5-tts': 'F5-TTS', gigaam: 'GigaAM', snofs: 'SNOFS Krea 2', 'big-love': 'Big Love Eryn1',
  fineview: 'FineView QW21', h3: 'H3 · DR34ML4Y', muse: 'Muse 3.5 Krea 2',
  'jib-moss': 'Jib Mix Moss V2', fineporn: 'FinePorn v5', 'moody-krea': 'Moody Krea v8',
  'hunyuan-ocr': 'HunyuanOCR' };

/** Подпись модели: короткие имена шлюза — словами, прочие — как есть. */
export function modelLabel(id) {
  return Object.hasOwn(LABELS, id) ? LABELS[id] : id;
}
