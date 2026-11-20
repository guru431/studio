/* Формы учётной записи. Секреты ссылок — только в памяти страницы и телах запросов. */
(async function () {
  'use strict';
  const { message: translateMessage, tr } = globalThis.StudioI18n;
  const screen = location.pathname.split('/').pop();
  const token = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  const form = document.getElementById('account-form');
  const title = document.getElementById('title');
  const description = document.getElementById('description');
  const note = document.getElementById('account-status');
  let csrf = null;
  let submit;

  async function unlock() {
    const state = await request('/api/lock');
    csrf = state.csrf;
    return new Promise((resolve, reject) => {
      const dialog = document.createElement('dialog');
      dialog.className = 'dialog'; dialog.id = 'account-unlock';
      const heading = document.createElement('h2'); heading.textContent = tr("Студия заперта");
      heading.id = 'account-unlock-title'; dialog.setAttribute('aria-labelledby', heading.id);
      heading.tabIndex = -1; heading.setAttribute('autofocus', '');
      const label = document.createElement('label'); label.htmlFor = 'account-unlock-password'; label.textContent = tr("Пароль учётной записи");
      const password = document.createElement('input');
      Object.assign(password, { id: label.htmlFor, type: 'password', className: 'input', required: true,
        autocomplete: 'current-password', maxLength: 1024 });
      const form = document.createElement('form');
      const error = document.createElement('p'); error.className = 'error'; error.setAttribute('role', 'alert');
      const open = document.createElement('button'); open.type = 'submit'; open.className = 'btn btn-accent'; open.textContent = tr("Открыть");
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'btn'; cancel.textContent = tr("Отмена");
      form.append(label, password, error, open, cancel); dialog.append(heading, form); document.body.append(dialog);
      let opened = false;
      dialog.addEventListener('close', () => {
        dialog.remove();
        if (!opened) reject(new Error(tr("Откройте Студию, чтобы продолжить.")));
      });
      cancel.addEventListener('click', () => dialog.close());
      form.addEventListener('submit', async event => {
        event.preventDefault();
        if (open.disabled) return;
        open.disabled = true;
        try {
          await request('/api/lock/password', { password: password.value });
          password.value = ''; opened = true; dialog.close(); resolve();
        } catch (err) { error.textContent = err.message; }
        finally { open.disabled = false; }
      });
      dialog.showModal();
      if (matchMedia('(hover: hover) and (pointer: fine)').matches) password.focus();
    });
  }

  function field(id, label, type, autocomplete, maxLength = 1024) {
    const name = document.createElement('label');
    name.htmlFor = id;
    name.textContent = label;
    const input = document.createElement('input');
    Object.assign(input, { id, name: id, type, autocomplete, maxLength, required: true, className: 'input' });
    if (type !== 'password') { input.autocapitalize = 'none'; input.spellcheck = false; }
    form.append(name, input);
    return input;
  }

  async function request(path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (csrf) headers['x-studio-csrf'] = csrf;
    let response;
    try {
      response = await fetch(path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', headers,
        body: body ? JSON.stringify(body) : undefined });
    } catch { throw new Error(tr("Нет связи с сервером. Попробуйте ещё раз.")); }
    if (response.redirected) { location.assign(response.url); return {}; }
    if (response.status === 204) return {};
    let result;
    try { result = await response.json(); }
    catch { throw new Error(tr("Сервер недоступен. Попробуйте позже.")); }
    if (response.status === 423 && result.detail?.code === 'screen_locked') {
      await unlock();
      return request(path, body);
    }
    if (!response.ok) {
      const error = new Error(translateMessage(result.detail?.message || tr("Не удалось выполнить запрос. Попробуйте позже.")));
      Object.assign(error, { status: response.status, code: result.detail?.code, left: result.detail?.left });
      throw error;
    }
    return result;
  }

  function done(message) {
    form.replaceChildren();
    note.className = 'ok';
    note.textContent = translateMessage(message);
  }

  try {
    let action;
    let extra = null;
    if (screen === 'invite') {
      title.textContent = tr("Приглашение в Студию");
      const invite = await request('/account/invite/check', { token });
      description.textContent = tr("Почта: {0}. Придумайте логин и пароль.", invite.email);
      const login = field('login', tr("Логин"), 'text', 'username', 32);
      login.pattern = '[a-zA-Z0-9._\\-]{2,32}';      // pattern компилируется с флагом v: дефис в классе — только с \
      const password = field('password', tr("Пароль (не короче 8 знаков)"), 'password', 'new-password');
      password.minLength = 8;
      const repeat = field('repeat', tr("Повтор пароля"), 'password', 'new-password');
      action = async () => {
        if (password.value !== repeat.value) throw new Error(tr("Пароли не совпадают"));
        await request('/account/invite/accept', { token, login: login.value, password: password.value });
        done(tr("Учётная запись создана. Теперь можно войти по логину или почте."));
      };
    } else if (screen === 'forgot') {
      title.textContent = tr("Восстановление пароля");
      description.textContent = tr("На почту учётной записи придёт одноразовая ссылка для нового пароля.");
      const login = field('login', tr("Логин или email"), 'text', 'username', 254);
      action = async () => done((await request('/account/forgot', { login: login.value })).message);
    } else if (screen === 'reset') {
      title.textContent = tr("Новый пароль");
      await request('/account/reset/check', { token });       // истекшая ссылка — до набора пароля
      description.textContent = tr("После сохранения все прежние сеансы Студии закроются.");
      const password = field('password', tr("Пароль (не короче 8 знаков)"), 'password', 'new-password');
      password.minLength = 8;
      const repeat = field('repeat', tr("Повтор пароля"), 'password', 'new-password');
      action = async () => {
        if (password.value !== repeat.value) throw new Error(tr("Пароли не совпадают"));
        await request('/account/reset', { token, password: password.value });
        done(tr("Пароль сохранён. Войдите с новым паролем."));
      };
    } else if (screen === 'code') {
      title.textContent = tr("Код из письма");
      description.textContent = tr("Введите шестизначный код. Он действует 10 минут.");
      const code = field('code', tr("Код входа"), 'text', 'one-time-code', 6);
      code.inputMode = 'numeric';
      code.pattern = '[0-9]{6}';
      // неверный код — сколько попыток осталось; код погас (срок, попытки) — только вход заново
      const failed = (err) => new Error(err.code === 'wrong_code' ? tr("Неверный код — осталось попыток: {0}", err.left)
        : err.code === 'expired' ? tr("Код больше не действует. Начните вход заново.") : err.message);
      action = async () => {
        try { location.assign((await request('/account/code', { code: code.value })).next); }
        catch (err) { throw failed(err); }
      };
      const resend = document.createElement('button');
      resend.type = 'button'; resend.className = 'btn'; resend.id = 'account-resend'; resend.textContent = tr("Отправить код ещё раз");
      resend.addEventListener('click', async () => {
        resend.disabled = true; note.textContent = '';
        try {
          await request('/account/code/resend', {});
          code.value = '';
          note.className = 'ok'; note.textContent = tr("Новый код отправлен. Прежний больше не действует.");
        } catch (err) { note.className = 'error'; note.textContent = failed(err).message; }
        finally { resend.disabled = false; }
      });
      extra = resend;
    } else {
      if (screen === 'verify') title.textContent = tr("Подтверждение почты");
      let me;
      try { me = await request('/api/me'); }
      catch (err) {
        // без сессии: секрет ссылки уже стёрт из адреса — после входа её надо открыть снова
        if (screen === 'verify' && err.status === 401) throw new Error(tr("Войдите в Студию в этом браузере, затем снова откройте ссылку из письма."));
        throw err;
      }
      csrf = me.csrf;
      // вошедшему «К входу» вело бы по кругу (/login → / → /account/email): выход и, если почта есть, назад
      const back = document.getElementById('account-back');
      const logout = document.getElementById('account-logout');
      logout.hidden = false;
      // через fetch: у страницы Referrer-Policy no-referrer, и обычная форма ушла бы с Origin: null — 403 «чужой сайт»
      logout.addEventListener('submit', async (event) => {
        event.preventDefault();
        try { await fetch('/logout', { method: 'POST', credentials: 'same-origin' }); } catch { /* сеть — всё равно ко входу */ }
        location.assign('/login');
      });
      back.href = '/';
      back.textContent = tr("Назад в Студию");
      back.hidden = !me.email && screen === 'email';
      if (screen === 'verify') {
        back.textContent = tr("Открыть Студию");
        await request('/api/me/email/confirm', { token });
        done(tr("Почта подтверждена."));
        back.hidden = false;
        return;
      }
      title.textContent = me.email ? tr("Изменить почту") : tr("Укажите почту");
      description.textContent = tr("Для учётной записи обязательна почта. Подтвердите адрес по ссылке из письма в этом браузере.");
      const email = field('email', tr("Электронная почта"), 'email', 'email', 254);
      email.value = me.email || '';
      const password = field('password', tr("Текущий пароль"), 'password', 'current-password');
      action = async () => {
        if (me.email && email.value.trim().toLowerCase() === me.email) throw new Error(tr("Это и есть почта вашей учётки — укажите новую."));
        const result = await request('/api/me/email', { email: email.value, password: password.value });
        password.value = '';
        note.className = 'ok'; note.textContent = result.message;
      };
    }
    submit = document.createElement('button');
    submit.type = 'submit'; submit.className = 'btn btn-accent';
    submit.textContent = ({ invite: tr("Создать учётную запись"), forgot: tr("Отправить ссылку"), reset: tr("Сохранить пароль"),
      code: tr("Войти"), email: tr("Подтвердить почту") })[screen];
    form.append(submit);
    if (extra) form.append(extra);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      submit.disabled = true; note.textContent = '';
      try { await action(); }
      catch (err) { note.className = 'error'; note.textContent = err.message; }
      finally { submit.disabled = false; }
    });
  } catch (err) {
    form.replaceChildren(); note.className = 'error'; note.textContent = err.message;
  }
})();
