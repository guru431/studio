/* Страница входа: адрес студии (#/…), с которого пришли (истёкшая сессия, нажатие на push), — серверу полем next
 * (auth.login_target проверит его и вернёт туда после входа) и в action формы: после неудачной попытки страница
 * входа остаётся с тем же адресом. Скрипт внешний: CSP страниц — только 'self'.
 * Вход — новая сессия: черновики «Агентов» прежней (sessionStorage вкладки, js/code-drafts.js) стираются, как бы сюда
 * ни пришли — выход, аварийный выход, сессия кончилась или завершена с другого устройства. */
(function () {
  try { sessionStorage.removeItem('studio-agent-drafts'); } catch (e) { /* хранилище закрыто — черновиков нет */ }
  var login = document.getElementById('login');     // после отказа логин уже набран (auth._login_page) — к паролю
  if (matchMedia('(hover: hover) and (pointer: fine)').matches) (login && login.value ? document.getElementById('password') : login)?.focus();
  var next = document.getElementById('next');
  if (!next || !/^#\//.test(location.hash)) return;
  next.value = location.hash;
  next.form.action = '/login' + location.hash;
})();
