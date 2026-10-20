/* Раздел «Панель» (#/admin, только администратору): пользователи и список разрешённых адресов.
 *
 * Пользователи — таблица `#users` (логин, имя, админ, отключён, подключений) с действиями строки:
 * «Изменить» (логин и имя; себя тоже — тогда страница перезагружается за новым логином в меню),
 * «Подключения» (те же разделы, что в настройках пользователя), «Сбросить пароль», «Отключить» / «Включить»,
 * «Удалить» (окно с вводом логина). Себя отключить и удалить нельзя — эти кнопки у своей строки
 * выключены (сервер всё равно ответил бы 409). Чужую историю панель не показывает и не получает. */
import { api, me } from './api.js';
import { settingsView } from './settings.js';
import { button, el, field, openDialog, showError, words } from './ui.js';

const COLUMNS = [['login', 'Логин'], ['name', 'Имя'], ['admin', 'Админ'], ['disabled', 'Отключён'],
  ['connections', 'Подключений']];
const USER_ERRORS = { login: 'login', taken: 'login', name: 'name', short: 'password' };

function yesNo(value) {
  return value ? 'да' : 'нет';
}

function usersBlock(host) {
  const tbody = el('tbody');
  const note = el('p', { class: 'status error', id: 'admin-status', 'aria-live': 'polite' });
  const table = el('table', { class: 'users', id: 'users' },
    el('thead', {}, el('tr', {}, ...COLUMNS.map(([, title]) => el('th', { scope: 'col' }, title)),
      el('th', { scope: 'col' }, el('span', { class: 'sr' }, 'Действия')))), tbody);

  async function reload() {
    try {
      const { items } = await api('GET', '/api/admin/users');
      tbody.replaceChildren(...items.map(row));
    } catch (err) {
      note.textContent = words(err);
    }
  }

  async function act(promise) {
    note.textContent = '';
    try {
      await promise;
    } catch (err) {
      note.textContent = words(err);
    }
    await reload();
  }

  function create() {
    const login = el('input', { class: 'input', name: 'login', maxlength: '32', autocomplete: 'off',
      autocapitalize: 'none', spellcheck: 'false', placeholder: 'латиница, цифры, . _ -' });
    const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off' });
    const password = el('input', { class: 'input', name: 'password', type: 'password', maxlength: '1024',
      autocomplete: 'new-password', placeholder: 'не короче 10 знаков' });
    const admin = el('input', { type: 'checkbox', name: 'is_admin' });
    const save = button('Создать', { class: 'btn btn-accent', id: 'user-save' });
    const form = el('div', {}, field('Логин', login, 'login'), field('Имя', name, 'name'),
      field('Пароль', password, 'password'), el('label', { class: 'check' }, admin, 'Администратор'),
      el('p', { class: 'error', 'data-for': 'form', 'aria-live': 'polite' }));
    const dialog = openDialog(host, { id: 'user-dialog', title: 'Новый пользователь', body: [form], actions: [save] });
    login.focus();
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await api('POST', '/api/admin/users', { login: login.value.trim(), name: name.value.trim() || login.value.trim(),
          password: password.value, is_admin: admin.checked });
        dialog.close();
        await reload();
      } catch (err) {
        showError(form, err, USER_ERRORS, 'form');
      } finally {
        save.disabled = false;
      }
    });
  }

  function edit(user) {
    const login = el('input', { class: 'input', name: 'login', maxlength: '32', autocomplete: 'off',
      autocapitalize: 'none', spellcheck: 'false', value: user.login });
    const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off', value: user.name });
    const save = button('Сохранить', { class: 'btn btn-accent', id: 'user-edit-save' });
    const form = el('div', {}, field('Логин', login, 'login'), field('Имя', name, 'name'),
      el('p', { class: 'error', 'data-for': 'form', 'aria-live': 'polite' }));
    const dialog = openDialog(host, { id: 'user-edit-dialog', title: `Изменить: ${user.login}`, body: [form], actions: [save] });
    login.focus();
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await api('PATCH', `/api/admin/users/${user.id}`, { login: login.value.trim(), name: name.value.trim() });
        dialog.close();
        if (user.id === me.id) location.reload();         // меню аватара берёт логин из /api/me
        else await reload();
      } catch (err) {
        showError(form, err, USER_ERRORS, 'form');
      } finally {
        save.disabled = false;
      }
    });
  }

  function resetPassword(user) {
    const password = el('input', { class: 'input', name: 'password', type: 'password', maxlength: '1024',
      autocomplete: 'new-password', placeholder: 'не короче 10 знаков' });
    const save = button('Сменить', { class: 'btn btn-accent', id: 'reset-save' });
    const form = el('div', {}, el('p', { class: 'muted small' }, 'Все сессии пользователя закроются.'),
      field('Новый пароль', password, 'password'));
    const dialog = openDialog(host, { id: 'reset-dialog', title: `Пароль: ${user.login}`, body: [form], actions: [save] });
    password.focus();
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        await api('PATCH', `/api/admin/users/${user.id}`, { password: password.value });
        dialog.close();
      } catch (err) {
        showError(form, err, {}, 'password');
      } finally {
        save.disabled = false;
      }
    });
  }

  function remove(user) {
    const confirm = el('input', { class: 'input', name: 'confirm', autocomplete: 'off', autocapitalize: 'none',
      spellcheck: 'false' });
    const yes = button('Удалить', { class: 'btn btn-danger solid', id: 'delete-confirm', disabled: true });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'delete-dialog', title: 'Удалить пользователя?', actions: [yes], body: [
      el('p', {}, `Удалятся учётка «${user.login}», её подключения и вся история. Отменить нельзя.`),
      field('Для подтверждения введите логин', confirm, 'confirm'), fail] });
    confirm.focus();
    confirm.addEventListener('input', () => { yes.disabled = confirm.value.trim() !== user.login; });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/admin/users/${user.id}`);
        dialog.close();
        await reload();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function connections(user) {
    const box = el('div', {});
    const dialog = openDialog(host, { id: 'user-conns-dialog', title: `Подключения: ${user.login}`, body: [box],
      cancelText: 'Закрыть' });
    dialog.addEventListener('close', reload);
    const view = settingsView(dialog, { sections: `/api/admin/users/${user.id}/sections`,
      connections: `/api/admin/users/${user.id}/connections`, section: 'chat' });
    box.append(view.el);
    view.reload();
  }

  function row(user) {
    const self = user.id === me.id;
    const values = { login: user.login, name: user.name, admin: yesNo(user.is_admin), disabled: yesNo(user.disabled),
      connections: String(user.connections) };
    return el('tr', { 'data-login': user.login, class: user.disabled ? 'off' : null },
      ...COLUMNS.map(([key, title]) => el('td', { 'data-col': key, 'data-label': title }, values[key])),
      el('td', { class: 'actions' }, el('div', { class: 'acts' },
        button('Изменить', { class: 'btn btn-sm user-edit' }, () => edit(user)),
        button('Подключения', { class: 'btn btn-sm user-conns' }, () => connections(user)),
        button('Сбросить пароль', { class: 'btn btn-sm user-reset' }, () => resetPassword(user)),
        button(user.disabled ? 'Включить' : 'Отключить', { class: 'btn btn-sm user-toggle', disabled: self,
          title: self ? 'себя отключить нельзя' : null },
        () => act(api('PATCH', `/api/admin/users/${user.id}`, { disabled: !user.disabled }))),
        button('Удалить', { class: 'btn btn-sm btn-danger user-delete', disabled: self,
          title: self ? 'себя удалить нельзя' : null }, () => remove(user)))));
  }

  const add = button('Создать пользователя', { class: 'btn btn-accent', id: 'user-add' }, create);
  return {
    el: el('section', { class: 'block' }, el('div', { class: 'block-head' }, el('h2', {}, 'Пользователи'), add),
      el('div', { class: 'table-wrap' }, table), note),
    reload,
  };
}

function allowlistBlock() {
  let entries = [];
  const list = el('ul', { class: 'allow-list', id: 'allow-list' });
  const input = el('input', { class: 'input', id: 'allow-entry', autocomplete: 'off', spellcheck: 'false',
    autocapitalize: 'none', inputmode: 'url', placeholder: 'IP:порт или имя:порт, например 10.0.0.5:4000 или api.openai.com:443' });
  const note = el('p', { class: 'status', id: 'allow-status', 'aria-live': 'polite' });
  const save = button('Сохранить', { class: 'btn btn-accent', id: 'allowlist-save' });

  function say(text, kind) {
    note.textContent = text;
    note.className = `status${kind ? ` ${kind}` : ''}`;
  }

  function draw() {
    list.replaceChildren(...(entries.length ? entries.map((entry) => el('li', { 'data-entry': entry },
      el('code', {}, entry),
      button('Удалить', { class: 'btn btn-sm btn-danger', 'aria-label': `Удалить ${entry}` }, () => {
        entries = entries.filter((e) => e !== entry);
        say('');
        draw();
      })))
      : [el('li', { class: 'muted' }, 'Список пуст: через сервер не идёт ни одно подключение')]));
  }

  function add() {
    const entry = input.value.trim();
    if (entry && !entries.includes(entry)) entries.push(entry);
    input.value = '';
    say('');
    draw();
  }

  async function load() {
    try {
      ({ entries } = await api('GET', '/api/admin/allowlist'));
      draw();
    } catch (err) {
      say(words(err), 'error');
    }
  }

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') add();
  });
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      ({ entries } = await api('PUT', '/api/admin/allowlist', { entries }));
      draw();
      say('Сохранено', 'ok');
    } catch (err) {
      say(words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });

  return {
    el: el('section', { class: 'block' }, el('h2', {}, 'Разрешённые внутренние адреса'),
      el('p', { class: 'muted small' }, 'Подключения к этим адресам студия зовёт сама, их токен браузеру не отдаётся. '
        + 'Запись — IP и порт (10.0.0.5:4000, [::1]:8080) или имя и порт (api.openai.com:443). Имя — только для https: '
        + 'студия при каждом запросе проверяет, что имя ведёт в интернет, а не во внутреннюю сеть.'),
      list, el('div', { class: 'row' }, input, button('Добавить', { class: 'btn', id: 'allow-add' }, add)),
      el('div', { class: 'row' }, save, note)),
    load,
  };
}

export function mount(view, ctx) {
  if (!me.is_admin) {
    ctx.navigate('chat');
    return;
  }
  const root = el('div', { class: 'page wide' });
  const users = usersBlock(root);
  const allow = allowlistBlock();
  root.append(users.el, allow.el);
  view.append(el('header', { class: 'head' }, el('h1', {}, 'Панель')), root);
  users.reload();
  allow.load();
}

export function unmount() {}
