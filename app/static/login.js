/* Страница входа: адрес студии (#/…), с которого пришли (истёкшая сессия, нажатие на push), — серверу полем next
 * (auth.login_target проверит его и вернёт туда после входа) и в action формы: после неудачной попытки страница
 * входа остаётся с тем же адресом. Скрипт внешний: CSP страниц — только 'self'. */
(function () {
  if (matchMedia('(hover: hover) and (pointer: fine)').matches) document.getElementById('login')?.focus();
  var next = document.getElementById('next');
  if (!next || !/^#\//.test(location.hash)) return;
  next.value = location.hash;
  next.form.action = '/login' + location.hash;
})();
