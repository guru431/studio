/* Формы учётной записи. Секреты ссылок — только в памяти страницы и телах запросов. */
(async function () {
  'use strict';
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
      const heading = document.createElement('h2'); heading.textContent = 'Студия заперта';
      heading.id = 'account-unlock-title'; dialog.setAttribute('aria-labelledby', heading.id);
      heading.tabIndex = -1; heading.setAttribute('autofocus', '');
      const label = document.createElement('label'); label.htmlFor = 'account-unlock-password'; label.textContent = 'Пароль учётной записи';
      const password = document.createElement('input');
      Object.assign(password, { id: label.htmlFor, type: 'password', className: 'input', required: true,
        autocomplete: 'current-password', maxLength: 1024 });
      const form = document.createElement('form');
      const error = document.createElement('p'); error.className = 'error'; error.setAttribute('role', 'alert');
      const open = document.createElement('button'); open.type = 'submit'; open.className = 'btn btn-accent'; open.textContent = 'Открыть';
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'btn'; cancel.textContent = 'Отмена';
      form.append(label, password, error, open, cancel); dialog.append(heading, form); document.body.append(dialog);
      let opened = false;
      dialog.addEventListener('close', () => {
        dialog.remove();
        if (!opened) reject(new Error('Откройте Студию, чтобы продолжить.'));
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
    } catch { throw new Error('Нет связи с сервером. Попробуйте ещё раз.'); }
    if (response.redirected) { location.assign(response.url); return {}; }
    if (response.status === 204) return {};
    let result;
    try { result = await response.json(); }
    catch { throw new Error('Сервер недоступен. Попробуйте позже.'); }
    if (response.status === 423 && result.detail?.code === 'screen_locked') {
      await unlock();
      return request(path, body);
    }
    if (!response.ok) throw new Error(result.detail?.message || 'Не удалось выполнить запрос. Попробуйте позже.');
    return result;
  }

  function done(message) {
    form.replaceChildren();
    note.className = 'ok';
    note.textContent = message;
  }

  try {
    let action;
    if (screen === 'invite') {
      title.textContent = 'Приглашение в Студию';
      const invite = await request('/account/invite/check', { token });
      description.textContent = `Почта: ${invite.email}. Придумайте логин и пароль.`;
      const login = field('login', 'Логин', 'text', 'username', 32);
      login.pattern = '[a-zA-Z0-9._-]{2,32}';
      const password = field('password', 'Пароль (не короче 10 знаков)', 'password', 'new-password');
      password.minLength = 10;
      const repeat = field('repeat', 'Повтор пароля', 'password', 'new-password');
      action = async () => {
        if (password.value !== repeat.value) throw new Error('Пароли не совпадают');
        await request('/account/invite/accept', { token, login: login.value, password: password.value });
        done('Учётная запись создана. Теперь можно войти по логину или почте.');
      };
    } else if (screen === 'forgot') {
      title.textContent = 'Восстановление пароля';
      description.textContent = 'На почту учётной записи придёт одноразовая ссылка для нового пароля.';
      const login = field('login', 'Логин или email', 'text', 'username', 254);
      action = async () => done((await request('/account/forgot', { login: login.value })).message);
    } else if (screen === 'reset') {
      title.textContent = 'Новый пароль';
      description.textContent = 'После сохранения все прежние сеансы Студии закроются.';
      const password = field('password', 'Пароль (не короче 10 знаков)', 'password', 'new-password');
      password.minLength = 10;
      const repeat = field('repeat', 'Повтор пароля', 'password', 'new-password');
      action = async () => {
        if (password.value !== repeat.value) throw new Error('Пароли не совпадают');
        await request('/account/reset', { token, password: password.value });
        done('Пароль сохранён. Войдите с новым паролем.');
      };
    } else if (screen === 'code') {
      title.textContent = 'Код из письма';
      description.textContent = 'Введите шестизначный код. Он действует 10 минут. Чтобы получить новый, начните вход заново.';
      const code = field('code', 'Код входа', 'text', 'one-time-code', 6);
      code.inputMode = 'numeric';
      code.pattern = '[0-9]{6}';
      action = () => request('/account/code', { code: code.value });
    } else {
      const me = await request('/api/me');
      csrf = me.csrf;
      if (screen === 'verify') {
        title.textContent = 'Подтверждение почты';
        await request('/api/me/email/confirm', { token });
        done('Почта подтверждена.');
        const back = document.getElementById('account-back');
        back.href = '/'; back.textContent = 'Открыть Студию';
        return;
      }
      title.textContent = me.email ? 'Изменить почту' : 'Укажите почту';
      description.textContent = 'Для учётной записи обязательна почта. Подтвердите адрес по ссылке из письма в этом браузере.';
      const email = field('email', 'Электронная почта', 'email', 'email', 254);
      email.value = me.email || '';
      const password = field('password', 'Текущий пароль', 'password', 'current-password');
      action = async () => {
        const result = await request('/api/me/email', { email: email.value, password: password.value });
        password.value = '';
        note.className = 'ok'; note.textContent = result.message;
      };
    }
    submit = document.createElement('button');
    submit.type = 'submit'; submit.className = 'btn btn-accent';
    submit.textContent = ({ invite: 'Создать учётную запись', forgot: 'Отправить ссылку', reset: 'Сохранить пароль',
      code: 'Войти', email: 'Подтвердить почту' })[screen];
    form.append(submit);
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
