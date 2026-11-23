import { tr } from './i18n.js';
import { api, me } from './api.js';
import { button, el, field, words } from './ui.js';

export function emailBlock() {
  const enabled = el('input', { type: 'checkbox', id: 'email-codes', checked: me.email_codes });
  const password = el('input', { class: 'input', id: 'email-codes-password', type: 'password',
    autocomplete: 'current-password', maxlength: '1024', required: true });
  const note = el('p', { id: 'email-codes-status', role: 'status', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn', type: 'submit', id: 'email-codes-save' });
  const form = el('form', {}, el('label', { class: 'check' }, enabled, tr("Присылать код на почту при входе")),
    field(tr("Текущий пароль"), password, password.id), save, note);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = true;
    try {
      const result = await api('PUT', '/api/me/email-codes', { enabled: enabled.checked, password: password.value });
      me.email_codes = result.enabled;
      password.value = '';
      note.className = 'ok'; note.textContent = result.enabled ? tr("Коды входа включены") : tr("Коды входа выключены");
    } catch (err) { enabled.checked = me.email_codes; note.className = 'error'; note.textContent = words(err); }
    finally { save.disabled = false; }
  });
  // письмо о входе с нового устройства (email_accounts.notice_due): сохраняется сразу, без пароля
  const notice = el('input', { type: 'checkbox', id: 'login-notice', checked: me.login_notice });
  const noticeNote = el('p', { id: 'login-notice-status', role: 'status', 'aria-live': 'polite' });
  notice.addEventListener('change', async () => {
    notice.disabled = true;
    try {
      me.login_notice = (await api('PUT', '/api/me/login-notice', { enabled: notice.checked })).enabled;
      noticeNote.className = 'ok'; noticeNote.textContent = tr("Сохранено");
    } catch (err) { notice.checked = me.login_notice; noticeNote.className = 'error'; noticeNote.textContent = words(err); }
    finally { notice.disabled = false; }
  });
  return el('section', { class: 'block' }, el('h2', {}, tr("Электронная почта")), el('p', { id: 'account-email' }, me.email),
    el('a', { class: 'btn', href: '/account/email' }, tr("Изменить почту")),
    el('p', { class: 'muted small' }, tr("Коды входа по умолчанию выключены. При включении после пароля потребуется код из письма.")), form,
    el('label', { class: 'check' }, notice, tr("Письмо о входе с нового устройства")),
    el('p', { class: 'muted small' }, tr("Если вход идёт с устройства, браузера или адреса, которых не было за полгода, на почту придёт письмо: время, устройство, браузер и адрес, без ссылок.")),
    noticeNote);
}
