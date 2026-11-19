/* Черновики основных полей «Чата», «Фото», «Видео», «Озвучки» и «Перевода»: раздел → текст, только память вкладки
 * (как черновики «Агентов» в code-chat.js). Смена раздела пересоздаёт поле — набранное возвращается. Не сервер и не
 * хранилище браузера: выход и аварийный выход уводят со страницы (память гаснет), блокировка экрана (событие
 * `studio-locked`, lock.js) стирает сохранённые черновики. Картинки и файлы не хранятся. Перезагрузка стёрла бы
 * их: автоматическую (refresh.js) черновик или неотправленный текст поля запрещают. */
const texts = new Map();
const live = new Set();          // поля на экране: { field, sent(), own() } — текст в них ещё не черновик
document.addEventListener('studio-locked', () => texts.clear());
document.addEventListener('studio-before-auto-refresh', (event) => {
  if (texts.size || [...live].some(({ field, sent, own }) => field.isConnected && field.value.trim() && field.value !== sent()
    && own())) {
    event.preventDefault();
  }
});

/** Черновик поля раздела `key`: пустое поле при сборке получает его. `sent(value)` — текст ушёл (успешная
 * отправка) и черновиком не станет; `save(keep)` — при уходе из раздела: непустое, не отправленное и `keep` —
 * в черновик, иначе черновика нет. `own()` — текст поля набран человеком (у «Перевода» — не исходник открытой записи). */
export function sectionDraft(key, field, own = () => true) {
  const saved = texts.get(key);
  if (saved && !field.value) field.value = saved;
  let sent = null;
  const shown = { field, sent: () => sent, own };
  live.add(shown);
  return {
    sent(value = field.value) {
      sent = value;
      if (texts.get(key) === value) texts.delete(key);
    },
    save(keep = true) {
      live.delete(shown);
      const value = field.value;
      if (keep && value.trim() && value !== sent) texts.set(key, value);
      else texts.delete(key);
    },
  };
}
