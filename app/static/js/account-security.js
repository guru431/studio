import { tr } from './i18n.js';
/* Безопасность учётки (account.py): сессии с «Завершить», доступы и ссылки, аварийный выход, журнал безопасности.
 * Самостоятельные блоки без привязки к месту в «Настройках» — их вызывает settings.js.
 *
 * Сессия — признак сервера (не дайджест и не cookie), своё имя, браузер, ОС, доступная модель, последний IP,
 * время входа и активности, push и ntfy. Текущую закрывает обычный «Выйти». Аварийный выход — с паролем
 * учётки: все сессии, ссылки «Поделиться» и на файлы, доступы участников к проектам, push и ntfy, по выбору ключи компьютеров;
 * затем страница входа.
 * Журнал — новые сверху страницами сервера, «Показать ещё» — по `next`. */
import { api } from './api.js';
import { sectionName } from './sections-meta.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, plural, words } from './ui.js';

const DEVICES = { phone: tr("Телефон"), computer: tr("Компьютер") };
const EVENTS = { email_change: tr("почта изменена"), email_codes_on: tr("коды входа включены"), email_codes_off: tr("коды входа выключены"),
  password_recovery: tr("пароль восстановлен через почту"), login: tr("вход"), login_fail: tr("неверный пароль"), logout: tr("выход"),
  login_code_sent: tr("пароль верен, код отправлен на почту"), login_code_fail: tr("неверный код из письма"), unlock: tr("открыто"),
  unlock_fail: tr("не открыто"), pin_exhausted: tr("PIN исчерпан"), screen_lock_on: tr("блокировка включена"),
  screen_lock_off: tr("блокировка выключена"), screen_lock_change: tr("блокировка перенастроена"), password: tr("пароль сменён"), session_end: tr("сессия завершена"),
  sessions_end: tr("остальные сессии завершены"),
  emergency: tr("аварийный выход"), share_create: tr("ссылка создана"), share_revoke: tr("ссылка отозвана"),
  share_refresh: tr("снимок ссылки обновлён"),
  file_link_create: tr("ссылка на файл создана"), file_link_revoke: tr("ссылка на файл отозвана"),
  share_pin_fail: tr("неверный PIN ссылки"), share_locked: tr("ссылка остановлена"), pin_set: tr("PIN задан"),
  pin_delete: tr("PIN убран"), passkey_add: tr("ключ добавлен"), passkey_delete: tr("ключ удалён"),
  password_reset: tr("пароль сброшен администратором"), admin_grant: tr("права администратора выданы"),
  admin_revoke: tr("права администратора сняты"), project_member_add: tr("доступ к проекту выдан"),
  project_member_remove: tr("доступ к проекту отозван"), project_leave: tr("вы покинули совместные проекты"),
  totp_on: tr("приложение-аутентификатор подключено"), totp_off: tr("приложение-аутентификатор отключено"),
  totp_reset: tr("приложение-аутентификатор сброшено администратором"), totp_fail: tr("неверный код приложения"),
  totp_backup_used: tr("вход резервным кодом") };
const DETAILS = { password: tr("паролем"), pin: 'PIN', passkey: tr("ключом"), bridge: tr("мост"), login: '', chat: tr("чат"),
  code: tr("агенты"), agents: tr("и компьютеры"), pause: tr("на 15 минут"), revoked: tr("— отключена"), all: tr("— все"),
  operator: tr("оператором хоста"),
  // раздел записи у ссылки «Поделиться» (chat_shares.ITEMS)
  transcribe: sectionName('transcribe'), translate: sectionName('translate'), ocr: sectionName('ocr'),
  photo: sectionName('photo'), video: sectionName('video') };

function moment(ts) {
  return ts ? dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' }) : '—';
}

function status(id) {
  return el('p', { class: 'status', id, 'aria-live': 'polite' });
}

function say(node, text, kind) {
  node.textContent = text;
  node.classList.toggle('error', kind === 'error');
  node.classList.toggle('ok', kind === 'ok');
}

/** «Сессии»: устройства, где открыта учётка; «Завершить» — любую, кроме этой. */
export function sessionsBlock(host) {
  const list = el('ul', { class: 'agents sessions', id: 'sessions' });
  const note = status('sessions-status');
  const refresh = button(tr("Обновить"), { class: 'btn btn-sm', id: 'sessions-refresh' }, () => reload());
  const endOthers = button(tr("Завершить все остальные сессии"),
    { class: 'btn btn-sm btn-danger', id: 'sessions-end-others', hidden: true }, () => end());
  let reading = 0;

  function title(s) {
    return s.label || s.model || DEVICES[s.device] || tr("Устройство");
  }

  function rename(session) {
    const input = el('input', { class: 'input', id: 'session-name', maxlength: '80', type: 'text',
      autocomplete: 'off', value: session.label || '', placeholder: tr("Например, рабочий ноутбук") });
    const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'session-name-save', type: 'submit', form: 'session-name-form' });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const form = el('form', { id: 'session-name-form' }, field(tr("Имя устройства"), input, 'session-name'), fail);
    const dialog = openDialog(host, { id: 'session-name-dialog', title: tr("Назвать устройство"), actions: [save], body: [
      el('p', { tabindex: '-1', autofocus: true }, tr("Имя видно на всех ваших устройствах. Оставьте поле пустым, чтобы вернуть автоматическое название.")),
      form] });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      save.disabled = true;
      try {
        await api('PATCH', `/api/me/sessions/${encodeURIComponent(session.id)}`, { label: input.value.trim() });
        dialog.close();
        if (await reload()) say(note, tr("Имя устройства сохранено"), 'ok');
        list.querySelector(`[data-id="${session.id}"] .session-rename`)?.focus({ preventScroll: true });
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    });
  }

  function end(session) {
    const all = !session;
    const yes = button(tr("Завершить"), { class: 'btn btn-danger', id: 'session-end-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'session-end-dialog',
      title: all ? tr("Завершить все остальные сессии?") : tr("Завершить сессию?"), actions: [yes], body: [
      all ? null : el('p', { class: 'session-description' }, [title(session), session.browser, session.os, session.ip,
        tr("сессия {0}", session.id.slice(0, 8))].filter(Boolean).join(' · ')),
      el('p', {}, all ? tr("На других устройствах нужно будет войти заново. Текущая сессия останется открытой.")
        : tr("Устройство сразу потеряет доступ, push и ntfy на нём перестанут приходить. Войти снова можно паролем.")),
      fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        const url = all ? '/api/me/sessions' : `/api/me/sessions/${encodeURIComponent(session.id)}`;
        await api('DELETE', url);
        dialog.close();
        if (await reload()) say(note, all ? tr("Остальные сессии завершены") : tr("Сессия завершена"), 'ok');
        refresh.focus({ preventScroll: true });
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  async function reload() {
    const version = ++reading;
    refresh.disabled = true;
    list.setAttribute('aria-busy', 'true');
    try {
      const { items } = await api('GET', '/api/me/sessions');
      if (version !== reading) return false;
      say(note, '');
      endOthers.hidden = !items.some((session) => !session.current);
      list.replaceChildren(...items.map((s) => el('li', { class: 'agent session', 'data-id': s.id },
        el('div', { class: 'session-info' },
          el('div', { class: 'row' }, el('b', { class: 'session-device' }, title(s)),
            s.current ? el('span', { class: 'tag session-current' }, tr("Это устройство")) : null,
            s.push ? el('span', { class: 'tag' }, 'push') : null,
            s.ntfy ? el('span', { class: 'tag' }, 'ntfy') : null),
          el('p', { class: 'small session-description' },
            [s.label ? s.model : null, s.browser, s.os].filter(Boolean).join(' · ')
              || tr("Сведения появятся, когда Студия снова откроется на этом устройстве.")),
          el('p', { class: 'muted small session-address' }, tr("IP: {0} · сессия {1}", s.ip || tr("пока неизвестен"), s.id.slice(0, 8))),
          el('p', { class: 'muted small session-times' },
            el('span', {}, tr("Вход: {0}", moment(s.created))), el('span', {}, tr("Активность: {0}", moment(s.active))))),
        el('div', { class: 'session-actions' },
          button(tr("Назвать"), { class: 'btn btn-sm session-rename', 'aria-label': tr("Назвать устройство: {0}", title(s)) }, () => rename(s)),
          s.current ? null : button(tr("Завершить"), { class: 'btn btn-sm btn-danger session-end',
            'aria-label': tr("Завершить сессию: {0}", title(s)) }, () => end(s))))));
      return true;
    } catch (err) {
      if (version === reading) say(note, words(err), 'error');
      return false;
    } finally {
      if (version === reading) {
        refresh.disabled = false;
        list.setAttribute('aria-busy', 'false');
      }
    }
  }

  reload();
  return el('section', { class: 'block sessions-block' }, el('h2', {}, tr("Сессии")),
    el('p', { class: 'muted small' }, tr("Где открыта ваша учётка. Незнакомое устройство завершите здесь, а затем смените пароль.")),
    el('p', { class: 'muted small' }, tr("Браузер может скрывать модель и точную версию ОС. Задайте своё имя, чтобы различать одинаковые устройства. IP может совпадать у устройств в одной сети или меняться при VPN.")),
    list, el('div', { class: 'row' }, refresh, endOthers, note));
}

/** «Доступы и ссылки» (GET /api/me/access): участники проектов ваших компьютеров — и к папкам, которых уже нет, — и
 * ваши ссылки «Поделиться». «Отозвать» по строке, «Отозвать всё» — после подтверждения. Имя проекта — от моста, когда
 * компьютер на связи и открыт PIN; иначе — начало id проекта. */
export function accessBlock(host) {
  const members = el('ul', { class: 'agents access-list', id: 'access-members', 'aria-label': tr("Участники проектов") });
  const shares = el('ul', { class: 'agents access-list', id: 'access-shares', 'aria-label': tr("Ссылки «Поделиться»") });
  const files = el('p', { class: 'small', id: 'access-file-links' });
  const note = status('access-status');
  const refresh = button(tr("Обновить"), { class: 'btn btn-sm', id: 'access-refresh' }, () => reload());
  const all = button(tr("Отозвать всё"), { class: 'btn btn-sm btn-danger', id: 'access-revoke-all', hidden: true },
    () => revokeAll());
  let reading = 0;
  let busy = false;

  function project(item) {
    if (item.missing) return tr("папка не найдена (проект {0})", item.project.slice(0, 8));
    return item.name ? `«${item.name}»` : tr("проект {0}", item.project.slice(0, 8));
  }

  const controls = () => [refresh, all, ...members.querySelectorAll('button'), ...shares.querySelectorAll('button')];

  async function act(request, message) {
    if (busy) return;
    busy = true;
    for (const node of controls()) node.disabled = true;
    try {
      await request();
      if (await reload()) say(note, message, 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      busy = false;
      for (const node of controls()) node.disabled = false;
    }
  }

  function memberRow(m) {
    const where = `${m.computer} · ${project(m)}`;
    return el('li', { class: 'agent session access-member', 'data-project': m.project },
      el('div', { class: 'session-info' },
        el('div', { class: 'row' }, el('b', {}, m.login), m.disabled ? el('span', { class: 'tag' }, tr("заблокирован")) : null,
          m.missing ? el('span', { class: 'tag access-missing' }, tr("папки нет")) : null),
        el('p', { class: 'small' }, where),
        el('p', { class: 'muted small' }, tr("Выдан {0}", moment(m.created)))),
      el('div', { class: 'session-actions' }, button(tr("Отозвать"), { class: 'btn btn-sm btn-danger access-revoke',
        'aria-label': tr("Отозвать доступ: {0}, {1}", m.login, where) }, () => act(() => api('DELETE',
        `/api/me/access/members/${m.computer_id}/${encodeURIComponent(m.project)}/${m.user_id}`), tr("Доступ отозван")))));
  }

  function shareRow(s) {
    // запись раздела (расшифровка, перевод, скан, картинка, ролик): `kind` — её раздел, открыть — #/<раздел>?id=<id>
    const item = !!s.item_id;
    const place = s.kind === 'chat' || item ? '' : `${s.computer} · ${project(s)}`;   // у обычного чата проекта нет
    const title = item ? s.title || sectionName(s.kind)
      : s.kind === 'chat' ? s.title || tr("Чат") : (s.title ? `${s.title} · ${place}` : place);
    const facts = [sectionName(s.kind), s.expires ? tr("до {0}", moment(s.expires)) : tr("бессрочная"),
      tr("открыта {0} {1}", s.views || 0, plural(s.views || 0, [tr("раз"), tr("раза"), tr("раз")])),
      s.fails ? tr("неверных PIN: {0}", s.fails) : null].filter(Boolean).join(' · ');
    // чат в корзине: ссылка получателю недоступна, пока его не вернут; открыть его — со страницы «Корзина»
    const href = s.trashed ? '#/trash' : s.chat_id ? `#/chat/${encodeURIComponent(s.chat_id)}`
      : item ? `#/${s.kind}?id=${encodeURIComponent(s.item_id)}`
        : s.session_id ? `#/code/${s.computer_id}/${encodeURIComponent(s.project)}/${encodeURIComponent(s.session_id)}` : null;
    const info = el('div', { class: 'session-info' },
      el('div', { class: 'row' }, el('b', {}, title),
        s.trashed ? el('span', { class: 'tag access-trashed' }, tr("в корзине — недоступна")) : null,
        // сработал защищённый источник MCP — ссылка недоступна навсегда (chat_shares.LIVE); остаётся отключить
        s.protected && !s.trashed ? el('span', { class: 'tag access-protected' }, tr("защищённые данные — недоступна")) : null),
      el('p', { class: 'muted small' }, facts),
      el('p', { class: 'muted small' }, tr("Создана {0}", moment(s.created))));
    return el('li', { class: 'agent session access-share', 'data-id': s.id }, info,
      el('div', { class: 'session-actions' },
        href ? el('a', { class: 'btn btn-sm access-open', href, 'aria-label': s.trashed ? tr("Открыть корзину: {0}", title)
          : item ? tr("Открыть запись: {0}", title) : tr("Открыть чат: {0}", title) },
        s.trashed ? tr("Открыть корзину") : item ? tr("Открыть запись") : tr("Открыть чат")) : null,
        button(tr("Копировать ссылку"), { class: 'btn btn-sm access-copy', 'aria-label': tr("Копировать ссылку: {0}", title) },
          () => copyLink(s, info)),
        button(tr("Отключить"), { class: 'btn btn-sm btn-danger access-revoke',
          'aria-label': tr("Отключить ссылку: {0}", title) }, () => act(() => api('DELETE',
          `/api/me/access/shares/${encodeURIComponent(s.id)}`), tr("Ссылка отключена")))));
  }

  /** Адрес ссылки — только по нажатию (GET …/link, no-store). Safari теряет право записи в буфер после await, поэтому
   * обещание адреса уходит в ClipboardItem сразу; не вышло — поле с выделенным адресом для ручного копирования. */
  async function copyLink(s, info) {
    say(note, '');
    const url = api('GET', `/api/me/access/shares/${encodeURIComponent(s.id)}/link`).then((data) => data.url);
    try {
      if (typeof ClipboardItem === 'function' && navigator.clipboard.write) {
        await navigator.clipboard.write([new ClipboardItem({
          'text/plain': url.then((text) => new Blob([text], { type: 'text/plain' })) })]);
      } else await navigator.clipboard.writeText(await url);
      say(note, tr("Ссылка скопирована"), 'ok');
    } catch {
      let address;
      try { address = await url; } catch (err) { say(note, words(err), 'error'); return; }
      info.querySelector('.access-link')?.remove();
      const box = el('input', { class: 'input access-link', readonly: true, value: address,
        'aria-label': tr("Ссылка «Поделиться»") });
      info.append(box);
      box.focus();
      box.select();
      say(note, tr("Скопируйте выделенную ссылку"), 'error');
    }
  }

  function revokeAll() {
    const yes = button(tr("Отозвать всё"), { class: 'btn btn-danger solid', id: 'access-revoke-all-confirm' });
    const dialog = openDialog(host, { id: 'access-revoke-all-dialog', title: tr("Отозвать все доступы и ссылки?"),
      actions: [yes], body: [el('p', {}, tr("Участники потеряют доступ к проектам ваших компьютеров, все ссылки «Поделиться» и ссылки на файлы отключатся. Вернуть их можно, только выдав доступ и создав ссылки заново."))] });
    yes.addEventListener('click', () => {
      dialog.close();
      act(() => api('DELETE', '/api/me/access'), tr("Все доступы и ссылки отозваны")).then(() => refresh.focus({ preventScroll: true }));
    });
  }

  async function reload() {
    const version = ++reading;
    refresh.disabled = true;
    try {
      const data = await api('GET', '/api/me/access');
      if (version !== reading) return false;
      say(note, '');
      members.replaceChildren(...(data.members.length ? data.members.map(memberRow)
        : [el('li', { class: 'muted small' }, tr("Проекты ваших компьютеров никому не открыты."))]));
      shares.replaceChildren(...(data.shares.length ? data.shares.map(shareRow)
        : [el('li', { class: 'muted small' }, tr("Ссылок нет."))]));
      // ссылки на файлы — только счёт: список с «Отозвать» — в «Моих файлах»
      files.replaceChildren(data.file_links ? tr("Действующих ссылок на файлы: {0}.", data.file_links) : tr("Ссылок на файлы нет."),
        ...(data.file_links ? [' ', el('a', { class: 'btn btn-sm access-open', id: 'access-file-links-open', href: '#/myfiles' },
          sectionName('myfiles'))] : []));
      all.hidden = !data.members.length && !data.shares.length && !data.file_links;
      return true;
    } catch (err) {
      if (version === reading) say(note, words(err), 'error');
      return false;
    } finally {
      if (version === reading) refresh.disabled = false;
    }
  }

  reload();
  return el('section', { class: 'block access-block' }, el('h2', {}, tr("Доступы и ссылки")),
    el('p', { class: 'muted small' }, tr("Кому открыты проекты ваших компьютеров и какие чаты и записи доступны по ссылке. Доступ привязан к имени папки: удаление или переименование папки его не снимает — отзовите его здесь.")),
    el('h3', {}, tr("Участники проектов")), members,
    el('h3', {}, tr("Ссылки «Поделиться»")), shares,
    el('h3', {}, tr("Ссылки на файлы")), files,
    el('div', { class: 'row' }, refresh, all, note));
}

/** «Аварийный выход»: с паролем учётки завершить все сессии и отозвать ссылки, по выбору — ключи компьютеров. */
export function emergencyBlock(host) {
  const password = el('input', { class: 'input', id: 'emergency-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const agents = el('input', { type: 'checkbox', id: 'emergency-agents' });
  const note = status('emergency-status');
  const start = button(tr("Аварийный выход"), { class: 'btn btn-danger', id: 'emergency-start' }, () => {
    say(note, '');
    if (!password.value) {
      say(note, tr("Введите пароль учётки"), 'error');
      return;
    }
    const yes = button(tr("Выйти везде"), { class: 'btn btn-danger solid', id: 'emergency-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'emergency-dialog', title: tr("Выйти на всех устройствах?"), actions: [yes], body: [
      el('p', {}, tr("Все сессии, включая эту, завершатся; ссылки «Поделиться», ссылки на файлы, почтовые ссылки и коды отключатся; участники потеряют доступ к проектам ваших компьютеров; push и ntfy придётся включить заново. После выхода смените пароль: со старым можно войти снова.")
        + (agents.checked ? tr(" Компьютеры будут отвязаны — мосты войдут снова только с паролем.") : '')),
      fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('POST', '/api/me/emergency', { password: password.value, agents: agents.checked });
        password.value = '';
        document.dispatchEvent(new Event('studio-signed-out'));      // черновики «Агентов» (code-drafts.js)
        location.assign('/login');
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  });
  return el('section', { class: 'block emergency-block' }, el('h2', {}, tr("Аварийный выход")),
    el('p', { class: 'muted small' }, tr("Если телефон или пароль могли попасть в чужие руки: завершает все сессии, отключает ссылки «Поделиться», ссылки на файлы, почтовые ссылки и коды, доступ участников к проектам, push и ntfy. История и настройки остаются. Затем смените пароль.")),
    field(tr("Пароль учётки"), password, 'emergency-password'),
    el('label', { class: 'check' }, agents, tr("Отозвать и ключи компьютеров (мосты)")),
    el('div', { class: 'row' }, start, note));
}

/** Пароль учётки сбросил администратор (`me.password_reset`): один раз после входа — совет проверить сессии и
 * сделать аварийный выход. Окно закрылось любым путём — флаг снимается на сервере (DELETE /api/me/password-reset);
 * аварийный выход снимает его и сам. */
export function passwordResetNotice(me, navigate) {
  if (!me.password_reset) return null;
  const open = button(tr("Открыть «Защиту»"), { class: 'btn btn-accent', id: 'password-reset-open' });
  const dialog = openDialog(document.body, { id: 'password-reset-dialog', title: tr("Пароль сменил администратор"),
    cancelText: tr("Понятно"), actions: [open], body: [
      el('p', {}, tr("Пароль вашей учётки сбросил администратор {0}. Если вы об этом не просили, ", moment(me.password_reset))
        + tr("кто-то мог войти в учётку.")),
      el('p', {}, tr("Проверьте «Сессии» и сделайте «Аварийный выход»: он завершит все сессии, отключит ссылки «Поделиться», доступ участников к проектам, push и ntfy."))] });
  open.addEventListener('click', () => {
    dialog.close();
    navigate('settings/security');
  });
  dialog.addEventListener('close', () => { api('DELETE', '/api/me/password-reset').catch(() => null); });
  return dialog;
}

/** «Журнал безопасности»: входы, неудачи, отпирания, сессии и ссылки — новые сверху. */
export function securityEventsBlock() {
  const events = el('ul', { class: 'agent-events security-events', id: 'security-events' });
  const note = status('security-events-status');
  let next = null;
  const more = button(tr("Показать ещё"), { class: 'btn btn-sm', id: 'security-events-more', hidden: true },
    () => load(next));

  async function load(before) {
    more.disabled = true;
    try {
      const page = await api('GET', before ? `/api/me/security-events?before=${before}` : '/api/me/security-events');
      // свёрнутые повторы неудач: «×N, с <время первой>» (security_log.NOISY)
      const rows = page.items.map((e) => el('li', { 'data-kind': e.kind }, [moment(e.created),
        // session_end: подробность — признак завершённой сессии (`id` из «Сессий»), видно начало
        [EVENTS[e.kind] || e.kind, e.kind === 'session_end' && e.detail ? tr("сессия {0}", e.detail.slice(0, 8))
          : DETAILS[e.detail] ?? '',
          e.count > 1 ? tr("×{0}, с {1}", e.count, moment(e.first)) : ''].filter(Boolean).join(' '),
        e.current ? tr("это устройство") : DEVICES[e.device], e.ip].filter(Boolean).join(' · ')));
      if (before) events.append(...rows);
      else events.replaceChildren(...(rows.length ? rows : [el('li', {}, tr("Событий пока нет."))]));
      next = page.next;
      more.hidden = next === null;
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      more.disabled = false;
    }
  }

  load(null);
  return el('section', { class: 'block security-events-block' }, el('h2', {}, tr("Журнал безопасности")),
    el('p', { class: 'muted small' }, tr("Входы и неудачные попытки (и коды из письма), отпирания и настройка блокировки, PIN и ключи, завершённые сессии, ссылки «Поделиться» и ссылки на файлы за полгода; повторы неудач с одного адреса — одной строкой со счётчиком. «Удалить всю историю» его не стирает; он входит в выгрузку истории.")),
    events, note, el('div', { class: 'row' }, more));
}
