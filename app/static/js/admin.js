import { locale, tr } from './i18n.js';
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

const COLUMNS = [['login', tr("Логин")], ['email', 'Email'], ['name', tr("Имя")], ['admin', tr("Админ")], ['disabled', tr("Отключён")],
  ['connections', tr("Подключений")]];
const USER_ERRORS = { login: 'login', taken: 'login', name: 'name', short: 'password' };

function yesNo(value) {
  return value ? tr("да") : tr("нет");
}

function usersBlock(host) {
  const tbody = el('tbody');
  const note = el('p', { class: 'status error', id: 'admin-status', 'aria-live': 'polite' });
  const table = el('table', { class: 'users', id: 'users' },
    el('thead', {}, el('tr', {}, ...COLUMNS.map(([, title]) => el('th', { scope: 'col' }, title)),
      el('th', { scope: 'col' }, el('span', { class: 'sr' }, tr("Действия"))))), tbody);

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

  function edit(user) {
    const login = el('input', { class: 'input', name: 'login', maxlength: '32', autocomplete: 'off',
      autocapitalize: 'none', spellcheck: 'false', value: user.login });
    const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off', value: user.name });
    const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'user-edit-save' });
    const form = el('div', {}, field(tr("Логин"), login, 'login'), field(tr("Имя"), name, 'name'),
      el('p', { class: 'error', 'data-for': 'form', 'aria-live': 'polite' }));
    const dialog = openDialog(host, { id: 'user-edit-dialog', title: tr("Изменить: {0}", user.login), body: [form], actions: [save] });
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
      autocomplete: 'new-password', placeholder: tr("не короче 8 знаков") });
    const save = button(tr("Сменить"), { class: 'btn btn-accent', id: 'reset-save' });
    const form = el('div', {}, el('p', { class: 'muted small' }, tr("Все сессии пользователя закроются.")),
      field(tr("Новый пароль"), password, 'password'));
    const dialog = openDialog(host, { id: 'reset-dialog', title: tr("Пароль: {0}", user.login), body: [form], actions: [save] });
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
    const yes = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'delete-confirm', disabled: true });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'delete-dialog', title: tr("Удалить пользователя?"), actions: [yes], body: [
      el('p', {}, tr("Удалятся учётка «{0}», её подключения и вся история. Отменить нельзя.", user.login)),
      field(tr("Для подтверждения введите логин"), confirm, 'confirm'), fail] });
    confirm.focus();
    confirm.addEventListener('input', () => { yes.disabled = confirm.value.trim() !== user.login; });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        const { pending_files: left } = await api('DELETE', `/api/admin/users/${user.id}`);
        dialog.close();
        await reload();
        // хранилище было недоступно: файлы учётки ждут в очереди удаления — сказать как есть
        if (left) note.textContent = tr("«{0}» удалён, но файлов на хранилище ещё {1}: удалятся, когда оно станет доступно", user.login, left);
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function connections(user) {
    const box = el('div', {});
    const dialog = openDialog(host, { id: 'user-conns-dialog', title: tr("Подключения: {0}", user.login), body: [box],
      cancelText: tr("Закрыть") });
    dialog.addEventListener('close', reload);
    const view = settingsView(dialog, { sections: `/api/admin/users/${user.id}/sections`,
      connections: `/api/admin/users/${user.id}/connections`, section: 'chat' });
    box.append(view.el);
    view.reload();
  }

  function row(user) {
    const self = user.id === me.id;
    const values = { login: user.login, email: user.email || tr("Требует подтверждения"), name: user.name, admin: yesNo(user.is_admin), disabled: yesNo(user.disabled),
      connections: String(user.connections) };
    return el('tr', { 'data-login': user.login, class: user.disabled ? 'off' : null },
      ...COLUMNS.map(([key, title]) => el('td', { 'data-col': key, 'data-label': title }, values[key])),
      el('td', { class: 'actions' }, el('div', { class: 'acts' },
        button(tr("Изменить"), { class: 'btn btn-sm user-edit' }, () => edit(user)),
        button(tr("Подключения"), { class: 'btn btn-sm user-conns' }, () => connections(user)),
        button(tr("Сбросить пароль"), { class: 'btn btn-sm user-reset', disabled: self,
          title: self ? tr("свой пароль — в «Настройках» → «Защита»") : null }, () => resetPassword(user)),
        button(user.disabled ? tr("Включить") : tr("Отключить"), { class: 'btn btn-sm user-toggle', disabled: self,
          title: self ? tr("себя отключить нельзя") : null },
        () => act(api('PATCH', `/api/admin/users/${user.id}`, { disabled: !user.disabled }))),
        button(tr("Удалить"), { class: 'btn btn-sm btn-danger user-delete', disabled: self,
          title: self ? tr("себя удалить нельзя") : null }, () => remove(user)))));
  }

  return {
    el: el('section', { class: 'block' }, el('div', { class: 'block-head' }, el('h2', {}, tr("Пользователи"))),
      el('div', { class: 'table-wrap' }, table), note),
    reload,
  };
}

function invitationsBlock(host) {
  const list = el('tbody', { id: 'invitations' });
  const note = el('p', { id: 'invitations-status', role: 'status', 'aria-live': 'polite' });
  const statuses = { pending: tr("Ожидает регистрации"), accepted: tr("Зарегистрирован"), expired: tr("Срок истёк"),
    revoked: tr("Отозвано"), failed: tr("Письмо не отправлено") };
  async function act(action) {
    note.textContent = '';
    try { await action(); }
    catch (err) { note.textContent = words(err); }
    await reload();
  }
  function edit(invite = null) {
    const email = el('input', { class: 'input', id: 'invite-email', name: 'email', type: 'email',
      autocomplete: 'email', maxlength: '254', required: true, value: invite?.email || '', readonly: !!invite });
    const comment = el('textarea', { class: 'input', id: 'invite-comment', name: 'comment', maxlength: '1000' }, invite?.comment || '');
    const admin = el('input', { type: 'checkbox', id: 'invite-admin' });
    const fail = el('p', { class: 'error', role: 'alert' });
    const save = button(invite ? tr("Сохранить") : tr("Отправить приглашение"), { class: 'btn btn-accent', id: 'invite-save',
      type: 'submit', form: 'invite-form' });
    const form = el('form', { id: 'invite-form' }, field('Email', email, email.id),
      field(tr("Комментарий (виден только администраторам)"), comment, comment.id),
      ...(!invite ? [el('label', { class: 'check' }, admin, tr("Администратор"))] : []), fail);
    const dialog = openDialog(host, { id: 'invite-dialog', title: invite ? tr("Комментарий к приглашению") : tr("Пригласить пользователя"),
      body: [form], actions: [save] });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      save.disabled = true;
      try {
        if (invite) await api('PATCH', `/api/admin/invitations/${invite.id}`, { comment: comment.value });
        else await api('POST', '/api/admin/invitations', { email: email.value, comment: comment.value, is_admin: admin.checked });
        dialog.close();
        await reload();
      } catch (err) { fail.textContent = words(err); await reload(); }
      finally { save.disabled = false; }
    });
  }
  async function reload() {
    try {
      const { items } = await api('GET', '/api/admin/invitations');
      list.replaceChildren(...items.map(invite => el('tr', { 'data-email': invite.email },
        el('td', { 'data-label': 'Email' }, invite.email), el('td', { 'data-label': tr("Комментарий") }, invite.comment),
        el('td', { 'data-label': tr("Статус") }, statuses[invite.status]),
        el('td', { class: 'actions' }, el('div', { class: 'acts' },
          button(tr("Комментарий"), { class: 'btn btn-sm invite-edit' }, () => edit(invite)),
          ...(invite.status !== 'accepted' ? [
            button(tr("Отправить снова"), { class: 'btn btn-sm invite-resend' }, event => {
              const button = event.currentTarget;
              button.disabled = true;
              act(() => api('POST', '/api/admin/invitations', {
                email: invite.email, comment: invite.comment, is_admin: invite.is_admin }));
            }),
            button(tr("Отозвать"), { class: 'btn btn-sm invite-revoke' }, () =>
              act(() => api('DELETE', `/api/admin/invitations/${invite.id}`))),
          ] : []))))));
    } catch (err) { note.textContent = words(err); }
  }
  return { reload, el: el('section', { class: 'block' }, el('div', { class: 'block-head' },
    el('h2', {}, tr("Приглашения")), button(tr("Пригласить пользователя"), { class: 'btn btn-accent', id: 'user-add' }, () => edit())),
    el('p', { class: 'muted small' }, tr("Ссылка одноразовая, действует 48 часов. Получатель сам выбирает логин и пароль.")),
    el('div', { class: 'table-wrap' }, el('table', { class: 'users' }, el('thead', {}, el('tr', {},
      ...['Email', tr("Комментарий"), tr("Статус"), tr("Действия")].map(text => el('th', { scope: 'col' }, text)))), list)), note) };
}

function allowlistBlock() {
  let entries = [];
  const list = el('ul', { class: 'allow-list', id: 'allow-list' });
  const input = el('input', { class: 'input', id: 'allow-entry', autocomplete: 'off', spellcheck: 'false',
    autocapitalize: 'none', inputmode: 'url', placeholder: tr("IP:порт или имя:порт, например 10.0.0.5:4000 или api.openai.com:443") });
  const note = el('p', { class: 'status', id: 'allow-status', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'allowlist-save' });

  function say(text, kind) {
    note.textContent = text;
    note.className = `status${kind ? ` ${kind}` : ''}`;
  }

  function draw() {
    list.replaceChildren(...(entries.length ? entries.map((entry) => el('li', { 'data-entry': entry },
      el('code', {}, entry),
      button(tr("Удалить"), { class: 'btn btn-sm btn-danger', 'aria-label': tr("Удалить {0}", entry) }, () => {
        entries = entries.filter((e) => e !== entry);
        say('');
        draw();
      })))
      : [el('li', { class: 'muted' }, tr("Список пуст: через сервер не идёт ни одно подключение"))]));
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
      say(tr("Сохранено"), 'ok');
    } catch (err) {
      say(words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });

  return {
    el: el('section', { class: 'block' }, el('h2', {}, tr("Разрешённые внутренние адреса")),
      el('p', { class: 'muted small' }, tr("Подключения к этим адресам студия зовёт сама, их токен браузеру не отдаётся. Запись — IP и порт (10.0.0.5:4000, [::1]:8080) или имя и порт (api.openai.com:443). Имя — только для https: студия при каждом запросе проверяет, что имя ведёт в интернет, а не во внутреннюю сеть.")),
      list, el('div', { class: 'row' }, input, button(tr("Добавить"), { class: 'btn', id: 'allow-add' }, add)),
      el('div', { class: 'row' }, save, note)),
    load,
  };
}

/* «Хранилище»: шара (доступна — свободное место; недоступна или не отвечает — словами сервера), возраст
 * последней копии базы (старше STALE_SEC — красным) и очередь удаления числом. Чужих объёмов здесь нет. */
const STALE_SEC = 36 * 3600;          // копия — раз в сутки в 04:30: старше полутора суток — пропущена

function agoText(sec) {
  if (sec < 3600) return tr("{0} мин назад", Math.max(1, Math.round(sec / 60)));
  if (sec < 48 * 3600) return tr("{0} ч назад", Math.round(sec / 3600));
  return tr("{0} дн назад", Math.round(sec / 86400));
}

/* «Проверка копии» (POST/GET /api/admin/backup-check): поднимется ли студия из новейшей копии базы —
 * итог словами, только числа; пока идёт — опрос раз в CHECK_POLL_MS, пока строка на странице. */
const CHECK_POLL_MS = 2000;

function count(n) {
  return n.toLocaleString(locale());
}

function checkText(r) {
  if (!r.backup) return r.problems.join('; ');
  const at = dateTime(r.at, { day: '2-digit', month: '2-digit', hour: '2-digit',
    minute: '2-digit' }).replace(',', '');
  const parts = [];
  if (r.schema_to !== null) parts.push(tr("схема {0} → {1}", r.schema_from, r.schema_to));
  if (r.integrity === 'ok') parts.push(tr("целостность в порядке"));
  if (r.schema_to !== null) parts.push(tr("токены расшифрованы {0} из {1}", r.tokens.ok, r.tokens.ok + r.tokens.failed));
  if (r.files.state === 'ok' || r.files.state === 'partial') {
    parts.push(tr("файлов нет на шаре {0} из {1}", count(r.files.missing), count(r.files.total)));
  }
  if (r.files.state === 'partial') parts.push(tr("проверено {0} из {1}", count(r.files.checked), count(r.files.total)));
  const text = tr("Копия {0} — {1}{2}", at, r.ok ? tr("поднимается") : tr("не поднимается"), parts.length ? `: ${parts.join(', ')}` : '');
  return r.ok ? text : `${text}. ${r.problems.join('; ')}`;
}

function backupCheck() {
  const result = el('p', { class: 'status', id: 'admin-backup-result', 'aria-live': 'polite' });
  const start = button(tr("Проверить копию"), { class: 'btn btn-sm', id: 'admin-backup-check' });

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
        show(tr("Проверка идёт: копия копируется и сверяется с шарой — это минуты"));
        setTimeout(poll, CHECK_POLL_MS);
      } else if (state === 'idle') {
        show(tr("Ещё не проверялась: копия поднимается в стороне, рабочая база не трогается"));
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
  return { el: el('div', { class: 'backup-check' }, el('div', { class: 'row' }, el('b', {}, tr("Проверка копии")), start),
    result), poll };
}

function storageBlock() {
  const list = el('dl', { class: 'storage', id: 'admin-storage' });
  const note = el('p', { class: 'status error', 'aria-live': 'polite' });
  const refresh = button(tr("Обновить"), { class: 'btn btn-sm', id: 'admin-storage-refresh' });
  const check = backupCheck();

  const line = (key, name, text, bad) => [el('dt', {}, name),
    el('dd', { 'data-key': key, class: bad ? 'error' : null }, text)];

  async function load() {
    refresh.disabled = true;
    note.textContent = '';
    try {
      const { share, backup, pending, orphans, check } = await api('GET', '/api/admin/storage');
      const shareText = share.state === 'ok' ? tr("доступна, свободно {0} из {1}", sizeText(share.free), sizeText(share.total))
        : share.message;
      const backupText = backup.state === 'ok'
        ? tr("{0} ({1}), копий — {2}", agoText(backup.age), momentText(backup.at), backup.copies)
        : backup.state === 'none' ? tr("копий нет") : tr("неизвестно: хранилище не прочитано");
      const [queue] = queueText(pending);
      list.replaceChildren(...line('share', tr("Шара"), shareText, share.state !== 'ok'),
        ...line('backup', tr("Копия базы"), backupText, backup.state !== 'ok' || backup.age > STALE_SEC),
        // итог последней проверки новейшей копии (ночной после копии или из «Проверить копию»), хранится в базе
        ...line('check', tr("Проверка копии"), !check ? tr("ещё не было: ночная идёт сразу после копии")
          : `${check.ok ? tr("поднимается") : tr("не поднимается, проблем — {0}", check.problems)}`
            + ` — ${agoText(check.age)} (${momentText(check.at)})`, check && (!check.ok || check.age > STALE_SEC)),
        ...line('queue', tr("Удаление"), queue, false),
        ...line('orphans', tr("Файлы без записи"), !orphans ? tr("проверки ещё не было")
          : orphans.found === null ? tr("проверка пропущена {0}: база новее кода", momentText(orphans.at))
            : tr("{0}: удалено — {1}", momentText(orphans.at), orphans.found), orphans && orphans.found === null));
    } catch (err) {
      note.textContent = words(err);
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener('click', load);
  return {
    el: el('section', { class: 'block' }, el('div', { class: 'block-head' }, el('h2', {}, tr("Хранилище")), refresh),
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
  const invitations = invitationsBlock(root);
  const allow = allowlistBlock();
  const storage = storageBlock();
  root.append(invitations.el, users.el, allow.el, storage.el);
  view.append(el('header', { class: 'head' }, el('h1', {}, NAMES.admin)), root);
  users.reload();
  invitations.reload();
  allow.load();
  storage.load();
}

export function unmount() {}
