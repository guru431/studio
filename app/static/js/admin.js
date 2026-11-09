/* Раздел «Панель» (#/admin, только администратору): пользователи, список разрешённых адресов и хранилище.
 *
 * Пользователи — таблица `#users` (логин, имя, админ, отключён, подключений) с действиями строки:
 * «Изменить» (логин и имя; себя тоже — тогда страница перезагружается за новым логином в меню),
 * «Подключения» (те же разделы, что в настройках пользователя), «Сбросить пароль», «Отключить» / «Включить»,
 * «Удалить» (окно с вводом логина). Себя отключить и удалить нельзя — эти кнопки у своей строки
 * выключены (сервер всё равно ответил бы 409). Чужую историю панель не показывает и не получает. */
import { api, me } from './api.js';
import { NAMES } from './sections-meta.js';
import { dateTime } from './time.js';
import { momentText, queueText, settingsView, sizeText } from './settings.js';
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
        const { pending_files: left } = await api('DELETE', `/api/admin/users/${user.id}`);
        dialog.close();
        await reload();
        // хранилище было недоступно: файлы учётки ждут в очереди удаления — сказать как есть
        if (left) note.textContent = `«${user.login}» удалён, но файлов на хранилище ещё ${left}: удалятся, когда оно станет доступно`;
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
        button('Сбросить пароль', { class: 'btn btn-sm user-reset', disabled: self,
          title: self ? 'свой пароль — в «Настройках» → «Прочее»' : null }, () => resetPassword(user)),
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

/* «Хранилище»: шара (доступна — свободное место; недоступна или не отвечает — словами сервера), возраст
 * последней копии базы (старше STALE_SEC — красным) и очередь удаления числом. Чужих объёмов здесь нет. */
const STALE_SEC = 36 * 3600;          // копия — раз в сутки в 04:30: старше полутора суток — пропущена

function agoText(sec) {
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))} мин назад`;
  if (sec < 48 * 3600) return `${Math.round(sec / 3600)} ч назад`;
  return `${Math.round(sec / 86400)} дн назад`;
}

/* «Проверка копии» (POST/GET /api/admin/backup-check): поднимется ли студия из новейшей копии базы —
 * итог словами, только числа; пока идёт — опрос раз в CHECK_POLL_MS, пока строка на странице. */
const CHECK_POLL_MS = 2000;

function count(n) {
  return n.toLocaleString('ru-RU');
}

function checkText(r) {
  if (!r.backup) return r.problems.join('; ');
  const at = dateTime(r.at, { day: '2-digit', month: '2-digit', hour: '2-digit',
    minute: '2-digit' }).replace(',', '');
  const parts = [];
  if (r.schema_to !== null) parts.push(`схема ${r.schema_from} → ${r.schema_to}`);
  if (r.integrity === 'ok') parts.push('целостность в порядке');
  if (r.schema_to !== null) parts.push(`токены расшифрованы ${r.tokens.ok} из ${r.tokens.ok + r.tokens.failed}`);
  if (r.files.state === 'ok' || r.files.state === 'partial') {
    parts.push(`файлов нет на шаре ${count(r.files.missing)} из ${count(r.files.total)}`);
  }
  if (r.files.state === 'partial') parts.push(`проверено ${count(r.files.checked)} из ${count(r.files.total)}`);
  const text = `Копия ${at} — ${r.ok ? 'поднимается' : 'не поднимается'}${parts.length ? `: ${parts.join(', ')}` : ''}`;
  return r.ok ? text : `${text}. ${r.problems.join('; ')}`;
}

function backupCheck() {
  const result = el('p', { class: 'status', id: 'admin-backup-result', 'aria-live': 'polite' });
  const start = button('Проверить копию', { class: 'btn btn-sm', id: 'admin-backup-check' });

  function show(text, kind) {
    result.textContent = text;
    result.classList.toggle('ok', kind === 'ok');
    result.classList.toggle('error', kind === 'error');
  }

  async function poll() {
    if (!result.isConnected) return;                       // ушли со страницы — опрос кончен
    try {
      const { state, result: r } = await api('GET', '/api/admin/backup-check');
      start.disabled = state === 'running';
      if (state === 'running') {
        show('Проверка идёт: копия копируется и сверяется с шарой — это минуты');
        setTimeout(poll, CHECK_POLL_MS);
      } else if (state === 'idle') {
        show('Ещё не проверялась: копия поднимается в стороне, рабочая база не трогается');
      } else {
        show(checkText(r), r.ok ? 'ok' : 'error');
      }
    } catch (err) {
      start.disabled = false;
      show(words(err), 'error');
    }
  }

  start.addEventListener('click', async () => {
    start.disabled = true;
    try {
      await api('POST', '/api/admin/backup-check');
    } catch (err) {
      if (err.code !== 'running') {            // не запустилась — ошибка остаётся, опрос её не затрёт
        start.disabled = false;
        show(words(err), 'error');
        return;
      }
    }
    poll();
  });
  return { el: el('div', { class: 'backup-check' }, el('div', { class: 'row' }, el('b', {}, 'Проверка копии'), start),
    result), poll };
}

function storageBlock() {
  const list = el('dl', { class: 'storage', id: 'admin-storage' });
  const note = el('p', { class: 'status error', 'aria-live': 'polite' });
  const refresh = button('Обновить', { class: 'btn btn-sm', id: 'admin-storage-refresh' });
  const check = backupCheck();

  const line = (key, name, text, bad) => [el('dt', {}, name),
    el('dd', { 'data-key': key, class: bad ? 'error' : null }, text)];

  async function load() {
    refresh.disabled = true;
    note.textContent = '';
    try {
      const { share, backup, pending, orphans } = await api('GET', '/api/admin/storage');
      const shareText = share.state === 'ok' ? `доступна, свободно ${sizeText(share.free)} из ${sizeText(share.total)}`
        : share.message;
      const backupText = backup.state === 'ok'
        ? `${agoText(backup.age)} (${momentText(backup.at)}), копий — ${backup.copies}`
        : backup.state === 'none' ? 'копий нет' : 'неизвестно: хранилище не прочитано';
      const [queue] = queueText(pending);
      list.replaceChildren(...line('share', 'Шара', shareText, share.state !== 'ok'),
        ...line('backup', 'Копия базы', backupText, backup.state !== 'ok' || backup.age > STALE_SEC),
        ...line('queue', 'Удаление', queue, false),
        ...line('orphans', 'Файлы без записи', !orphans ? 'проверки ещё не было'
          : orphans.found === null ? `проверка пропущена ${momentText(orphans.at)}: база новее кода`
            : `${momentText(orphans.at)}: удалено — ${orphans.found}`, orphans && orphans.found === null));
    } catch (err) {
      note.textContent = words(err);
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener('click', load);
  return {
    el: el('section', { class: 'block' }, el('div', { class: 'block-head' }, el('h2', {}, 'Хранилище'), refresh),
      list, note, check.el),
    load: () => {
      load();
      check.poll();
    },
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
  const storage = storageBlock();
  root.append(users.el, allow.el, storage.el);
  view.append(el('header', { class: 'head' }, el('h1', {}, NAMES.admin)), root);
  users.reload();
  allow.load();
  storage.load();
}

export function unmount() {}
