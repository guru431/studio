/* Страница входа: адрес студии (#/…), с которого пришли (истёкшая сессия, нажатие на push), — серверу полем next
 * (auth.login_target проверит его и вернёт туда после входа) и в action формы: после неудачной попытки страница
 * входа остаётся с тем же адресом. Скрипт внешний: CSP страниц — только 'self'.
 * Вход — новая сессия: черновики «Агентов» и «Чата» прежней (sessionStorage вкладки, js/dialog-drafts.js) стираются,
 * как бы сюда ни пришли — выход, аварийный выход, сессия кончилась или завершена с другого устройства; вкладки
 * диалогов «Чата» (localStorage, js/chat-tabs.js) — тоже. */
(function () {
  try {
    sessionStorage.removeItem('studio-agent-drafts');
    sessionStorage.removeItem('studio-chat-drafts');
  } catch (e) { /* хранилище закрыто — черновиков нет */ }
  try { localStorage.removeItem('studio-chat-tabs'); } catch (e) { /* хранилище закрыто — вкладок нет */ }
  // страница входа — только без сессии (с ней GET /login уводит в студию): вкладки студии этого браузера — на вход
  try { new BroadcastChannel('studio-session').postMessage('signed-out'); } catch (e) { /* канала нет — уйдут опросом */ }
  // без сессии число на значке приложения (js/jobs.js, sw.js) — чужое для следующего входа
  if (navigator.clearAppBadge) navigator.clearAppBadge().catch(function () {});
  if (window.caches) caches.delete('studio-badge').catch(function () {});
  // непринятое из листа «Поделиться» (js/share-target.js), уже показанное прежней сессии, — тоже чужое; принятое без
  // сессии (offered ещё нет) ждёт этого входа
  if (window.caches) {
    caches.match('/received/meta', { cacheName: 'studio-received' })
      .then(function (hit) { return hit ? hit.json() : null; })
      .then(function (meta) { if (meta && meta.offered) return caches.delete('studio-received'); })
      .catch(function () {});
  }
  var login = document.getElementById('login');     // после отказа логин уже набран (auth._login_page) — к паролю
  if (matchMedia('(hover: hover) and (pointer: fine)').matches) (login && login.value ? document.getElementById('password') : login)?.focus();
  var next = document.getElementById('next');
  if (!next || !/^#\//.test(location.hash)) return;
  next.value = location.hash;
  next.form.action = '/login' + location.hash;
})();
