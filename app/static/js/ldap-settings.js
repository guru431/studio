import { api } from './api.js';
import { tr } from './i18n.js';
import { button, el, field, words } from './ui.js';

export function ldapSettings(onSave) {
  const endpoint = '/api/admin/ldap';
  const input = (key, attrs = {}) => el('input', { id: `ldap-config-${key}`, class: 'input', autocomplete: 'off', ...attrs });
  const enabled = input('enabled', { type: 'checkbox', class: null });
  const password = input('bind-password', { type: 'password', maxlength: '8192', autocomplete: 'new-password',
    placeholder: tr("Оставьте пустым, чтобы сохранить пароль") });
  const adminPassword = input('admin-password', { type: 'password', maxlength: '1024', autocomplete: 'current-password' });
  const fields = {
    url: input('url', { maxlength: '512', placeholder: 'ldaps://dc.example.com', autocapitalize: 'none', spellcheck: 'false' }),
    base: input('base', { maxlength: '1024', placeholder: 'DC=example,DC=com', spellcheck: 'false' }),
    group: input('group', { maxlength: '1024', spellcheck: 'false' }),
    bind_dn: input('bind-dn', { maxlength: '1024', spellcheck: 'false' }),
    ca_pem: el('textarea', { id: 'ldap-config-ca', class: 'input', rows: '4', maxlength: '65536', spellcheck: 'false' })
  };
  const note = el('p', { class: 'status', 'aria-live': 'polite', id: 'ldap-config-status' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', type: 'submit', id: 'ldap-config-save', disabled: true });
  const check = button(tr("Проверить соединение"), { class: 'btn', id: 'ldap-config-check', disabled: true });
  const busy = (waiting) => { save.disabled = check.disabled = waiting; };
  const label = el('label', { class: 'check' }, enabled, tr("Включить вход через LDAP"));
  const form = el('form', { id: 'ldap-config-form' }, label,
    field(tr("Сервер LDAP"), fields.url), field(tr("База поиска (DN)"), fields.base),
    field(tr("Группа доступа (DN)"), fields.group), field(tr("Служебная учётка (DN)"), fields.bind_dn),
    field(tr("Пароль служебной учётки"), password), field(tr("Сертификаты CA в PEM (необязательно)"), fields.ca_pem),
    field(tr("Ваш пароль администратора"), adminPassword), el('div', { class: 'row' }, save, check), note);
  const root = el('details', { class: 'block', id: 'ldap-settings' }, el('summary', {}, 'LDAP'),
    el('p', { class: 'muted' }, tr("Укажите адрес ldaps:// или ldap:// для StartTLS. Проверка использует поля формы. Отключение LDAP завершает сессии и отключает связанные учётки.")),
    form);

  function fill(value) {
    for (const [key, control] of Object.entries(fields)) control.value = value[key] ?? '';
    enabled.checked = value.enabled;
    password.value = '';
    note.textContent = value.reenter ? tr("Настройки LDAP нужно ввести заново")
      : value.password_set ? tr("Пароль сохранён") : tr("Пароль не задан");
  }

  async function send(saveChanges) {
    if (!adminPassword.value) {
      note.textContent = tr("Введите пароль учётки");
      adminPassword.focus();
      return;
    }
    busy(true);
    const body = { ...Object.fromEntries(Object.entries(fields).map(([key, control]) => [key, control.value])),
      enabled: enabled.checked, admin_password: adminPassword.value };
    if (password.value) body.bind_password = password.value;
    try {
      const value = await api(saveChanges ? 'PUT' : 'POST', saveChanges ? endpoint : `${endpoint}/check`, body);
      if (saveChanges) {
        fill(value);
        onSave();
      }
      note.textContent = saveChanges ? tr("Настройки LDAP сохранены") : tr("Соединение с LDAP установлено");
    } catch (error) {
      note.textContent = words(error);
    } finally {
      adminPassword.value = '';
      busy(false);
    }
  }

  form.addEventListener('submit', event => { event.preventDefault(); send(true); });
  check.addEventListener('click', () => send(false));
  api('GET', endpoint).then(value => { fill(value); busy(false); }).catch(error => { note.textContent = words(error); });
  return root;
}
