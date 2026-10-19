/* Тема до первой отрисовки: последняя выбранная, из localStorage. Значение профиля (/api/me)
 * потом ставит app.js и оно главнее. Скрипт обычный, не модуль: исполняется сразу, в <head>. */
(function () {
  try {
    var theme = localStorage.getItem('studio-theme');
    if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
  } catch (e) {
    // хранилище закрыто (приватный режим) — тема по настройке устройства
  }
})();
