/* Безопасность учётки (account.py): сессии с «Завершить», аварийный выход, журнал безопасности. Три
 * самостоятельных блока без привязки к месту в «Настройках» — их вызывает settings.js.
 *
 * Сессия — признак сервера (не дайджест и не cookie), вид устройства грубо (телефон/компьютер), когда создана,
 * последняя активность, включены ли push и ntfy. Текущую закрывает обычный «Выйти». Аварийный выход — с паролем
 * учётки: все сессии, ссылки «Поделиться», push и ntfy, по выбору ключи компьютеров; затем страница входа.
 * Журнал — новые сверху страницами сервера, «Показать ещё» — по `next`. */
import { api } from './api.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, words } from './ui.js';

const DEVICES = { phone: 'Телефон', computer: 'Компьютер' };
const EVENTS = { login: 'вход', login_fail: 'неверный пароль', logout: 'выход', unlock: 'открыто',
  unlock_fail: 'не открыто', pin_exhausted: 'PIN исчерпан', password: 'пароль сменён', session_end: 'сессия завершена',
  emergency: 'аварийный выход', share_create: 'ссылка создана', share_revoke: 'ссылка отозвана',
  share_pin_fail: 'неверный PIN ссылки', share_locked: 'ссылка на паузе' };
const DETAILS = { password: 'паролем', pin: 'PIN', passkey: 'ключом', bridge: 'мост', login: '', chat: 'чат',
  code: 'агенты', agents: 'и компьютеры' };

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

  function end(session) {
    const yes = button('Завершить', { class: 'btn btn-danger', id: 'session-end-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'session-end-dialog', title: 'Завершить сессию?', actions: [yes], body: [
      el('p', {}, 'Устройство сразу потеряет доступ, push и ntfy на нём перестанут приходить. Войти снова можно паролем.'),
      fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/me/sessions/${encodeURIComponent(session.id)}`);
        dialog.close();
        say(note, 'Сессия завершена', 'ok');
        reload();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  async function reload() {
    try {
      const { items } = await api('GET', '/api/me/sessions');
      list.replaceChildren(...items.map((s) => el('li', { class: 'agent session', 'data-id': s.id },
        el('b', { class: 'session-device' }, s.current ? 'Это устройство' : DEVICES[s.device] || 'Устройство'),
        el('span', { class: 'muted small session-times' },
          `вход ${moment(s.created)} · активность ${moment(s.active)}`),
        s.push ? el('span', { class: 'tag' }, 'push') : null,
        s.ntfy ? el('span', { class: 'tag' }, 'ntfy') : null,
        s.current ? null : button('Завершить', { class: 'btn btn-sm btn-danger agent-revoke session-end' }, () => end(s)))));
    } catch (err) {
      say(note, words(err), 'error');
    }
  }

  reload();
  return el('section', { class: 'block sessions-block' }, el('h2', {}, 'Сессии'),
    el('p', { class: 'muted small' }, 'Где открыта ваша учётка. Незнакомое устройство завершите здесь, а затем смените пароль.'),
    list, note);
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
      const rows = page.items.map((e) => el('li', { 'data-kind': e.kind }, [moment(e.created),
        [EVENTS[e.kind] || e.kind, DETAILS[e.detail] ?? ''].filter(Boolean).join(' '),
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
    el('p', { class: 'muted small' }, 'Входы и неудачные попытки, отпирания, завершённые сессии и ссылки «Поделиться» '
      + 'за полгода. «Удалить всю историю» его не стирает; он входит в выгрузку истории.'),
    events, note, el('div', { class: 'row' }, more));
}
