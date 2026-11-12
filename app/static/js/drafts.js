/* Черновики основных полей «Чата», «Фото», «Видео», «Озвучки» и «Перевода»: раздел → текст, только память вкладки
 * (как черновики «Агентов» в code-chat.js). Смена раздела пересоздаёт поле — набранное возвращается. Не сервер и не
 * хранилище браузера: выход и аварийный выход уводят со страницы (память гаснет), блокировка экрана (событие
 * `studio-locked`, lock.js) стирает сохранённые черновики. Картинки и файлы не хранятся. */
const texts = new Map();
document.addEventListener('studio-locked', () => texts.clear());

/** Черновик поля раздела `key`: пустое поле при сборке получает его. `sent(value)` — текст ушёл (успешная
 * отправка) и черновиком не станет; `save(keep)` — при уходе из раздела: непустое, не отправленное и `keep` —
 * в черновик, иначе черновика нет. */
export function sectionDraft(key, field) {
  const saved = texts.get(key);
  if (saved && !field.value) field.value = saved;
  let sent = null;
  return {
    sent(value = field.value) {
      sent = value;
      if (texts.get(key) === value) texts.delete(key);
    },
    save(keep = true) {
      const value = field.value;
      if (keep && value.trim() && value !== sent) texts.set(key, value);
      else texts.delete(key);
    },
  };
}
