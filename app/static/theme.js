/* Тема до первой отрисовки: последняя выбранная, из localStorage. Значение профиля (/api/me)
 * потом ставит app.js и оно главнее. Скрипт обычный, не модуль: исполняется сразу, в <head>. */
(function () {
  try {
    var theme = localStorage.getItem('studio-theme');
    if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
    var font = Number(localStorage.getItem('studio-font-size')) || 100;
    var scale = Number(localStorage.getItem('studio-ui-scale')) || 100;
    if (font >= 85 && font <= 150) document.documentElement.style.setProperty('--font-scale', font / 100);
    if ([75, 85, 100, 110, 125, 150].indexOf(scale) !== -1) {
      document.documentElement.style.setProperty('--ui-scale', scale / 100);
      document.documentElement.style.zoom = String(scale / 100);
    }
  } catch (e) {
    // хранилище закрыто (приватный режим) — тема по настройке устройства
  }
})();
