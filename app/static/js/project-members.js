import { tr } from './i18n.js';
/* Доступ к одному проекту по логину или email существующей учётки. */
import { api } from './api.js';
import { el, words } from './ui.js';
import { popupPanel } from './menu.js';
import { dateTime } from './time.js';

/** Роль участника: соавтор работает в проекте, наблюдатель только смотрит (сервер не пустит изменения). */
const ROLES = [['editor', "Соавтор"], ['viewer', "Наблюдатель"]];
const roleSelect = (attrs) => el('select', { class: 'input', ...attrs },
  ...ROLES.map(([value, label]) => el('option', { value }, tr(label))));

export function projectMembers(host, trigger, aid, project, onChange) {
  const url = `/api/code/agents/${aid}/projects/${project.pid}/members`;
  const login = el('input', { class: 'input', type: 'text', autocomplete: 'off', autocapitalize: 'none',
    spellcheck: 'false', maxlength: '254', required: true, 'aria-label': tr("Логин или email пользователя Студии"),
    placeholder: tr("Логин или email пользователя Студии") });
  const add = el('button', { class: 'btn', type: 'submit' }, tr("Дать доступ"));
  const role = roleSelect({ id: 'project-member-role', 'aria-label': tr("Роль участника") });
  const term = el('select', { class: 'input', id: 'project-member-term', 'aria-label': tr("Срок доступа") },
    el('option', { value: '' }, tr("Бессрочно")), el('option', { value: 'day' }, tr("На день")),
    el('option', { value: 'week' }, tr("На неделю")), el('option', { value: 'month' }, tr("На месяц")));
  const status = el('p', { class: 'muted', role: 'status', 'aria-live': 'polite' });
  const list = el('ul', { class: 'project-members-list', 'aria-label': tr("Участники проекта") });
  const close = el('button', { class: 'btn', type: 'button' }, tr("Закрыть"));
  const form = el('form', {}, login, el('div', { class: 'project-members-options' }, role, term), add);
  const panel = el('div', { class: 'project-members', id: `project-members-${aid}-${project.pid}` },
    el('h3', {}, tr("Доступ · {0}", project.name)),
    el('p', { class: 'muted small' }, tr("Участник видит все чаты и файлы этого проекта, может менять файлы и запускать агентов. Агенты работают с правами и подключениями владельца компьютера.")),
    el('p', { class: 'muted small' }, tr("Наблюдатель только читает чаты, файлы и Git: писать агентам, отвечать на их вопросы и менять файлы он не может.")),
    el('p', { class: 'muted small' }, tr("Доступ привязан к имени папки. Все выданные доступы — «Настройки» → «Защита» → «Доступы и ссылки».")),
    form, status, list, close);
  host.append(panel);
  const popup = popupPanel(trigger, panel, { label: tr("Доступ к проекту {0}", project.name), autoFocus: false,
    followViewport: true, align: 'end' });
  let alive = true, busy = false;
  function pending(value) {
    busy = value;
    form.inert = value;
    panel.setAttribute('aria-busy', String(value));
    for (const button of list.querySelectorAll('button, select')) button.disabled = value;
  }
  async function load() {
    const data = await api('GET', url);
    if (!alive) return;
    list.replaceChildren(...data.items.map(user => {
      const remove = el('button', { class: 'btn', type: 'button', 'aria-label': tr("Отозвать доступ · {0}", user.login) }, tr("Отозвать"));
      remove.addEventListener('click', () => change(() => api('DELETE', `${url}/${user.id}`), tr("Доступ отозван")));
      const kind = roleSelect({ class: 'input project-member-role', 'aria-label': tr("Роль · {0}", user.login) });
      kind.value = user.role === 'viewer' ? 'viewer' : 'editor';
      kind.addEventListener('change', () => change(() => api('PATCH', `${url}/${user.id}`, { role: kind.value }),
        tr("Роль изменена")));
      const until = user.expires ? tr("до {0}", dateTime(user.expires, { day: 'numeric', month: 'long', hour: '2-digit',
        minute: '2-digit' })) : tr("бессрочно");
      return el('li', {}, el('span', {}, el('span', {}, user.login + (user.disabled ? tr(" · заблокирован") : '')),
        el('span', { class: 'muted small project-member-term' }, until)), kind, remove);
    }));
    if (!data.items.length) list.append(el('li', { class: 'muted' }, tr("Доступ есть только у владельца")));
    onChange(data.items.length);
  }
  async function change(action, message) {
    if (busy || !alive) return;
    pending(true);
    status.textContent = tr("Сохраняю…");
    try {
      await action();
      login.value = '';
      await load();
      if (alive) {
        status.textContent = message;
        if (popup.isOpen()) close.focus({ preventScroll: true });
      }
    } catch (err) { if (alive) status.textContent = words(err); }
    finally { if (alive) pending(false); }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    const value = login.value.trim();
    if (value) change(() => api('POST', url, { login: value, role: role.value, term: term.value || null }),
      tr("Доступ предоставлен"));
  });
  close.addEventListener('click', () => popup.close(true));
  trigger.addEventListener('click', async () => {
    if (!popup.isOpen() || busy) return;
    pending(true);
    status.textContent = tr("Загружаю участников…");
    try { await load(); if (alive) status.textContent = ''; }
    catch (err) { if (alive) status.textContent = words(err); }
    finally { if (alive) pending(false); }
  });
  return { unmount() { alive = false; popup.close(); panel.remove(); } };
}
