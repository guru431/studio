import { api } from './api.js';
import { tr } from './i18n.js';
import { dateTime } from './time.js';
import { button, el, field, words } from './ui.js';

export function smtpDeliveryText(status) {
  if (!status.configured) return tr("Почта не настроена — Настройки → SMTP");
  if (!status.last) return tr("Почта настроена. Отправок пока не было.");
  const at = dateTime(status.last.at);
  return status.last.code === 'ok' ? tr("Почта работает. Последняя отправка: {0}.", at)
    : tr("Последняя отправка не удалась: {0} ({1}).", at, status.last.code);
}

export function smtpSettings() {
  const fields = {}, labels = { host: tr("Сервер SMTP"), port: tr("Порт"), tls: tr("Шифрование"), username: tr("Логин SMTP"),
    password: tr("Пароль SMTP"), sender: tr("Отправитель"), connect_ip: tr("IP для подключения (необязательно)") };
  for (const key of Object.keys(labels)) fields[key] = key === 'tls'
    ? el('select', { id: `smtp-${key}`, class: 'input' }, el('option', { value: 'starttls' }, 'STARTTLS'),
      el('option', { value: 'ssl' }, 'TLS / SSL'), el('option', { value: 'plain' }, tr("Без шифрования")))
    : el('input', { id: `smtp-${key}`, class: 'input', type: key === 'password' ? 'password' : key === 'port' ? 'number' : 'text', autocomplete: 'off' });
  fields.port.min = '1'; fields.port.max = '65535'; fields.host.required = fields.port.required = fields.sender.required = true;
  fields.password.placeholder = tr("Оставьте пустым, чтобы сохранить пароль");
  const clear = el('input', { id: 'smtp-clear-password', type: 'checkbox' });
  const note = el('p', { class: 'status', 'aria-live': 'polite', id: 'smtp-status' });
  const delivery = el('p', { class: 'muted small', id: 'smtp-delivery' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', type: 'submit', id: 'smtp-save', disabled: true });
  const check = button(tr("Проверить соединение"), { class: 'btn', id: 'smtp-check', disabled: true });
  const test = button(tr("Отправить тестовое письмо себе"), { class: 'btn', id: 'smtp-test', disabled: true });
  const busy = (value) => { save.disabled = check.disabled = test.disabled = value; };
  const form = el('form', { id: 'smtp-form' }, ...Object.keys(fields).map(key => field(labels[key], fields[key])),
    el('label', { class: 'check' }, clear, tr("Удалить сохранённый пароль")), el('div', { class: 'row' }, save, check, test), note);
  const root = el('section', { class: 'block', id: 'smtp-settings' }, el('h2', {}, 'SMTP'), el('p', { class: 'muted' },
    tr("Почта для приглашений, восстановления и кодов входа. Изменения действуют сразу. Проверка использует сохранённые настройки и не отправляет письмо.")),
    delivery, form);
  function fill(value) {
    for (const key of Object.keys(fields)) if (key !== 'password') fields[key].value = value[key] ?? '';
    fields.password.value = ''; clear.checked = false;
    delivery.textContent = value.delivery ? smtpDeliveryText(value.delivery) : '';
    note.textContent = value.reenter ? tr("Сохранённые настройки SMTP зашифрованы прежним ключом данных — введите их заново")
      : value.password_set ? tr("Пароль сохранён") : tr("Пароль не задан");
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); busy(true);
    const body = Object.fromEntries(Object.entries(fields).filter(([key]) => key !== 'password').map(([key, control]) => [key, control.value]));
    body.port = Number(body.port); if (fields.password.value || clear.checked) body.password = clear.checked ? '' : fields.password.value;
    try { fill(await api('PUT', '/api/admin/smtp', body)); note.textContent = tr("Настройки SMTP сохранены"); }
    catch (err) { note.textContent = words(err); } finally { busy(false); }
  });
  check.addEventListener('click', async () => {
    busy(true);
    try { await api('POST', '/api/admin/smtp/check'); note.textContent = tr("Соединение с SMTP установлено"); }
    catch (err) { note.textContent = words(err); } finally { busy(false); }
  });
  test.addEventListener('click', async () => {
    busy(true);
    try { await api('POST', '/api/admin/smtp/test'); note.textContent = tr("Тестовое письмо отправлено на вашу почту — проверьте, что оно пришло"); }
    catch (err) { note.textContent = words(err); }
    finally {
      const status = await api('GET', '/api/admin/smtp/status').catch(() => null);
      if (status) delivery.textContent = smtpDeliveryText(status);
      busy(false);
    }
  });
  api('GET', '/api/admin/smtp').then(value => { fill(value); busy(false); }).catch(err => { note.textContent = words(err); });
  return root;
}
