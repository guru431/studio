/* Безопасность учётки (account.py): сессии с «Завершить», аварийный выход, журнал безопасности. Три
 * самостоятельных блока без привязки к месту в «Настройках» — их вызывает settings.js.
 *
 * Сессия — признак сервера (не дайджест и не cookie), своё имя, браузер, ОС, доступная модель, последний IP,
 * время входа и активности, push и ntfy. Текущую закрывает обычный «Выйти». Аварийный выход — с паролем
 * учётки: все сессии, ссылки «Поделиться», push и ntfy, по выбору ключи компьютеров; затем страница входа.
 * Журнал — новые сверху страницами сервера, «Показать ещё» — по `next`. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, words } from './ui.js';

const DEVICES = { phone: 'Телефон', computer: 'Компьютер' };
const EVENTS = { login: 'вход', login_fail: 'неверный пароль', logout: 'выход', unlock: 'открыто',
  unlock_fail: 'не открыто', pin_exhausted: 'PIN исчерпан', password: 'пароль сменён', session_end: 'сессия завершена',
  emergency: 'аварийный выход', share_create: 'ссылка создана', share_revoke: 'ссылка отозвана',
  share_pin_fail: 'неверный PIN ссылки', share_locked: 'ссылка остановлена', pin_set: 'PIN задан',
  pin_delete: 'PIN убран', passkey_add: 'ключ добавлен', passkey_delete: 'ключ удалён',
  password_reset: 'пароль сброшен администратором' };
const DETAILS = { password: 'паролем', pin: 'PIN', passkey: 'ключом', bridge: 'мост', login: '', chat: 'чат',
  code: 'агенты', agents: 'и компьютеры', pause: 'на 15 минут', revoked: '— отключена' };

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
  const refresh = button('Обновить', { class: 'btn btn-sm', id: 'sessions-refresh' }, () => reload());
  let reading = 0;

  function title(s) {
    return s.label || s.model || DEVICES[s.device] || 'Устройство';
  }

  function rename(session) {
    const input = el('input', { class: 'input', id: 'session-name', maxlength: '80', type: 'text',
      autocomplete: 'off', value: session.label || '', placeholder: 'Например, рабочий ноутбук' });
    const save = button('Сохранить', { class: 'btn btn-accent', id: 'session-name-save', type: 'submit', form: 'session-name-form' });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const form = el('form', { id: 'session-name-form' }, field('Имя устройства', input, 'session-name'), fail);
    const dialog = openDialog(host, { id: 'session-name-dialog', title: 'Назвать устройство', actions: [save], body: [
      el('p', { tabindex: '-1', autofocus: true }, 'Имя видно на всех ваших устройствах. Оставьте поле пустым, чтобы вернуть автоматическое название.'),
      form] });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (save.disabled) return;
      save.disabled = true;
      try {
        await api('PATCH', `/api/me/sessions/${encodeURIComponent(session.id)}`, { label: input.value.trim() });
        dialog.close();
        if (await reload()) say(note, 'Имя устройства сохранено', 'ok');
        list.querySelector(`[data-id="${session.id}"] .session-rename`)?.focus({ preventScroll: true });
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    });
  }

  function end(session) {
    const yes = button('Завершить', { class: 'btn btn-danger', id: 'session-end-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'session-end-dialog', title: 'Завершить сессию?', actions: [yes], body: [
      el('p', { class: 'session-description' }, [title(session), session.browser, session.os, session.ip,
        `сессия ${session.id.slice(0, 8)}`].filter(Boolean).join(' · ')),
      el('p', {}, 'Устройство сразу потеряет доступ, push и ntfy на нём перестанут приходить. Войти снова можно паролем.'),
      fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/me/sessions/${encodeURIComponent(session.id)}`);
        dialog.close();
        if (await reload()) say(note, 'Сессия завершена', 'ok');
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
      list.replaceChildren(...items.map((s) => el('li', { class: 'agent session', 'data-id': s.id },
        el('div', { class: 'session-info' },
          el('div', { class: 'row' }, el('b', { class: 'session-device' }, title(s)),
            s.current ? el('span', { class: 'tag session-current' }, 'Это устройство') : null,
            s.push ? el('span', { class: 'tag' }, 'push') : null,
            s.ntfy ? el('span', { class: 'tag' }, 'ntfy') : null),
          el('p', { class: 'small session-description' },
            [s.label ? s.model : null, s.browser, s.os].filter(Boolean).join(' · ')
              || 'Сведения появятся, когда Студия снова откроется на этом устройстве.'),
          el('p', { class: 'muted small session-address' }, `IP: ${s.ip || 'пока неизвестен'} · сессия ${s.id.slice(0, 8)}`),
          el('p', { class: 'muted small session-times' },
            el('span', {}, `Вход: ${moment(s.created)}`), el('span', {}, `Активность: ${moment(s.active)}`))),
        el('div', { class: 'session-actions' },
          button('Назвать', { class: 'btn btn-sm session-rename', 'aria-label': `Назвать устройство: ${title(s)}` }, () => rename(s)),
          s.current ? null : button('Завершить', { class: 'btn btn-sm btn-danger session-end',
            'aria-label': `Завершить сессию: ${title(s)}` }, () => end(s))))));
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
  return el('section', { class: 'block sessions-block' }, el('h2', {}, 'Сессии'),
    el('p', { class: 'muted small' }, 'Где открыта ваша учётка. Незнакомое устройство завершите здесь, а затем смените пароль.'),
    el('p', { class: 'muted small' }, 'Браузер может скрывать модель и точную версию ОС. Задайте своё имя, чтобы различать одинаковые устройства. '
      + 'IP может совпадать у устройств в одной сети или меняться при VPN.'),
    list, el('div', { class: 'row' }, refresh, note));
}

/** «Аварийный выход»: с паролем учётки завершить все сессии и отозвать ссылки, по выбору — ключи компьютеров. */
export function emergencyBlock(host) {
  const password = el('input', { class: 'input', id: 'emergency-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024' });
  const agents = el('input', { type: 'checkbox', id: 'emergency-agents' });
  const note = status('emergency-status');
  const start = button('Аварийный выход', { class: 'btn btn-danger', id: 'emergency-start' }, () => {
    say(note, '');
    if (!password.value) {
      say(note, 'Введите пароль учётки', 'error');
      return;
    }
    const yes = button('Выйти везде', { class: 'btn btn-danger solid', id: 'emergency-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'emergency-dialog', title: 'Выйти на всех устройствах?', actions: [yes], body: [
      el('p', {}, 'Все сессии, включая эту, завершатся; ссылки «Поделиться» отключатся; push и ntfy придётся включить заново.'
        + (agents.checked ? ' Компьютеры будут отвязаны — мосты войдут снова только с паролем.' : '')),
      fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('POST', '/api/me/emergency', { password: password.value, agents: agents.checked });
        password.value = '';
        location.assign('/login');
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  });
  return el('section', { class: 'block emergency-block' }, el('h2', {}, 'Аварийный выход'),
    el('p', { class: 'muted small' }, 'Если телефон или пароль могли попасть в чужие руки: завершает все сессии, '
      + 'отключает ссылки «Поделиться», push и ntfy. История и настройки остаются.'),
    field('Пароль учётки', password, 'emergency-password'),
    el('label', { class: 'check' }, agents, 'Отозвать и ключи компьютеров (агенты)'),
    el('div', { class: 'row' }, start, note));
}

/** Пароль учётки сбросил администратор (`me.password_reset`): один раз после входа — совет проверить сессии и
 * сделать аварийный выход. Окно закрылось любым путём — флаг снимается на сервере (DELETE /api/me/password-reset);
 * аварийный выход снимает его и сам. */
export function passwordResetNotice(me, navigate) {
  if (!me.password_reset) return null;
  const open = button('Открыть «Защиту»', { class: 'btn btn-accent', id: 'password-reset-open' });
  const dialog = openDialog(document.body, { id: 'password-reset-dialog', title: 'Пароль сменил администратор',
    cancelText: 'Понятно', actions: [open], body: [
      el('p', {}, `Пароль вашей учётки сбросил администратор ${moment(me.password_reset)}. Если вы об этом не просили, `
        + 'кто-то мог войти в учётку.'),
      el('p', {}, 'Проверьте «Сессии» и сделайте «Аварийный выход»: он завершит все сессии, отключит ссылки '
        + '«Поделиться», push и ntfy.')] });
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
  const more = button('Показать ещё', { class: 'btn btn-sm', id: 'security-events-more', hidden: true },
    () => load(next));

  async function load(before) {
    more.disabled = true;
    try {
      const page = await api('GET', before ? `/api/me/security-events?before=${before}` : '/api/me/security-events');
      // свёрнутые повторы неудач: «×N, с <время первой>» (security_log.NOISY)
      const rows = page.items.map((e) => el('li', { 'data-kind': e.kind }, [moment(e.created),
        [EVENTS[e.kind] || e.kind, DETAILS[e.detail] ?? '',
          e.count > 1 ? `×${e.count}, с ${moment(e.first)}` : ''].filter(Boolean).join(' '),
        e.current ? 'это устройство' : DEVICES[e.device], e.ip].filter(Boolean).join(' · ')));
      if (before) events.append(...rows);
      else events.replaceChildren(...(rows.length ? rows : [el('li', {}, 'Событий пока нет.')]));
      next = page.next;
      more.hidden = next === null;
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      more.disabled = false;
    }
  }

  load(null);
  return el('section', { class: 'block security-events-block' }, el('h2', {}, 'Журнал безопасности'),
    el('p', { class: 'muted small' }, 'Входы и неудачные попытки, отпирания, PIN и ключи, завершённые сессии и ссылки '
      + '«Поделиться» за полгода; повторы неудач с одного адреса — одной строкой со счётчиком. «Удалить всю историю» его не стирает; он входит в выгрузку истории.'),
    events, note, el('div', { class: 'row' }, more));
}
