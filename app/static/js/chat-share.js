/* One current snapshot per chat. Every creation rotates both the link and viewer access. */
import { api } from './api.js';
import { button, el, field, openDialog, words } from './ui.js';

export const SHARE_ICON = '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m9 10 6-3M9 14l6 3"/>';

export function shareDialog(host, endpoint, title = 'Чат', request = api) {
  const pin = el('input', { id: 'chat-share-pin', class: 'input', type: 'password', inputmode: 'numeric',
    pattern: '[0-9]{4}', minlength: '4', maxlength: '4', autocomplete: 'off', required: true, disabled: true });
  const error = el('p', { class: 'error', role: 'alert' });
  const link = el('input', { id: 'chat-share-link', class: 'input', readonly: true, 'aria-label': 'Ссылка на чат' });
  const copy = button('Копировать ссылку', { id: 'chat-share-copy' });
  const output = el('div', { hidden: true }, field('Ссылка для чтения', link, 'share-link'), copy);
  const save = button('Создать ссылку', { id: 'chat-share-create', type: 'submit', class: 'btn btn-accent', disabled: true });
  const revoke = button('Отключить ссылку', { id: 'chat-share-revoke', class: 'btn btn-danger', hidden: true });
  const form = el('form', {}, field('PIN доступа · 4 цифры', pin, 'share-pin'), save);
  const dialog = openDialog(host, { id: 'chat-share-dialog', title: 'Поделиться чатом', cancelText: 'Закрыть',
    body: [el('p', {}, 'Получатель увидит снимок всей переписки и вложения, только для чтения. '
      + 'При каждом открытии нужен PIN этого чата. Передайте его отдельно от ссылки.'),
    el('p', { class: 'muted small' }, 'Новая ссылка отключит прежнюю. Новые сообщения в снимок не добавляются.'),
    form, output, error], actions: [revoke] });
  let busy = true;
  const state = (out) => {
    copy.textContent = 'Копировать ссылку';
    output.hidden = revoke.hidden = !out.enabled;
    link.value = out.url || '';
    save.textContent = out.enabled ? 'Создать новую ссылку' : 'Создать ссылку';
  };
  const pending = (value) => { busy = value; save.disabled = revoke.disabled = pin.disabled = value; };
  request('GET', endpoint).then((out) => {
    if (dialog.isConnected) state(out);
  }).catch((err) => {
    if (dialog.isConnected) {
      error.textContent = words(err);
      // Revocation requires ownership and remains available with an offline or locked bridge.
      revoke.hidden = false;
    }
  }).finally(() => {
    if (dialog.isConnected) {
      pending(false);
      if (matchMedia('(hover: hover) and (pointer: fine)').matches && dialog.matches(':modal')) pin.focus({ preventScroll: true });
    }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    error.textContent = '';
    pending(true);
    try {
      const out = await request('POST', endpoint, { pin: pin.value, title });
      if (!dialog.isConnected) return;
      pin.value = '';
      state(out);
      copy.focus({ preventScroll: true });
    } catch (err) {
      if (dialog.isConnected) error.textContent = words(err);
    } finally { if (dialog.isConnected) pending(false); }
  });
  revoke.addEventListener('click', async () => {
    if (busy) return;
    pending(true);
    error.textContent = '';
    try {
      await request('DELETE', endpoint);
      if (dialog.isConnected) { state({ enabled: false }); pin.value = ''; }
    } catch (err) {
      if (dialog.isConnected) error.textContent = words(err);
    } finally { if (dialog.isConnected) pending(false); }
  });
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(link.value); copy.textContent = 'Скопировано'; }
    catch { link.focus(); link.select(); error.textContent = 'Скопируйте выделенную ссылку'; }
  });
  dialog.addEventListener('close', () => { pin.value = ''; link.value = ''; });
  return dialog;
}
