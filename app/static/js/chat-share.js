/* One current snapshot per chat. Every creation rotates both the link and viewer access. */
import { api } from './api.js';
import { button, el, field, openDialog, words } from './ui.js';

export const SHARE_ICON = '<path d="M8 9H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-2M12 15V3m-4 4 4-4 4 4"/>';

/** Native popover supplies outside/Escape dismissal; menu keys work across pointer and touch. */
export function shareMenu(host, { id, request = api, failed = () => {} }) {
  const trigger = button('', { id, class: 'icon-btn chat-share-trigger', 'aria-label': 'Поделиться и экспортировать чат',
    title: 'Поделиться и экспортировать чат', 'aria-haspopup': 'menu', 'aria-expanded': 'false', disabled: true });
  trigger.innerHTML = `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${SHARE_ICON}</svg>`;
  const menu = el('div', { id: `${id}-menu`, class: 'chat-share-menu', popover: 'auto', role: 'menu',
    'aria-label': 'Поделиться чатом' });
  trigger.setAttribute('aria-controls', menu.id);
  host.append(menu);
  let context = null;
  let controller = null;
  let busy = false;
  const close = () => { if (menu.matches(':popover-open')) menu.hidePopover(); };
  const items = [['link', 'Поделиться ссылкой'], ['pdf', 'Скачать PDF'], ['md', 'Скачать Markdown']].map(([key, label]) => {
    const item = button(label, { class: 'menu-item', role: 'menuitem', 'data-action': key, tabindex: '-1' });
    item.addEventListener('click', async () => {
      const current = context;
      if (!current || busy) return;
      close();
      if (key === 'link') { shareDialog(host, current.share, current.title, request); return; }
      busy = true;
      trigger.disabled = true;
      trigger.setAttribute('aria-busy', 'true');
      controller = new AbortController();
      try {
        const response = await request('POST', current.export, { format: key, title: current.title }, { signal: controller.signal });
        const blob = await response.blob();
        if (context !== current || !trigger.isConnected) return;
        const href = URL.createObjectURL(blob);
        const name = (current.title || 'Чат').replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, '_').slice(0, 100);
        const download = el('a', { href, download: `${name}.${key}` });
        document.body.append(download);
        download.click();
        download.remove();
        setTimeout(() => URL.revokeObjectURL(href), 60000);
      } catch (err) {
        if (context === current && err.name !== 'AbortError') failed(err);
      } finally {
        busy = false;
        controller = null;
        trigger.disabled = !context;
        trigger.removeAttribute('aria-busy');
      }
    });
    return item;
  });
  menu.append(...items);
  const open = (last = false) => {
    if (!context || busy || menu.matches(':popover-open')) return;
    const box = trigger.getBoundingClientRect();
    menu.style.top = '8px';
    menu.style.left = '8px';
    menu.style.right = 'auto';
    menu.showPopover();
    const { width, height } = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(box.right - width, innerWidth - width - 8))}px`;
    menu.style.top = `${box.bottom + height + 6 <= innerHeight - 8
      ? box.bottom + 6 : Math.max(8, box.top - height - 6)}px`;
    (last ? items.at(-1) : items[0]).focus({ preventScroll: true });
  };
  trigger.addEventListener('click', () => menu.matches(':popover-open') ? close() : open());
  trigger.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open(event.key === 'ArrowUp');
    }
  });
  menu.addEventListener('keydown', (event) => {
    const here = items.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (here + 1) % items.length;
    else if (event.key === 'ArrowUp') next = (here + items.length - 1) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else if (event.key === 'Escape' || event.key === 'Tab') { close(); trigger.focus({ preventScroll: true }); }
    else return;
    if (event.key !== 'Tab') event.preventDefault();
    if (next !== undefined) items[next].focus({ preventScroll: true });
  });
  menu.addEventListener('toggle', () => trigger.setAttribute('aria-expanded', String(menu.matches(':popover-open'))));
  window.addEventListener('resize', close);
  return { button: trigger, set(value) {
    if (context?.share === value?.share && context?.export === value?.export && context?.title === value?.title) return;
    close();
    host.querySelector('#chat-share-dialog')?.close();
    controller?.abort();
    context = value;
    trigger.disabled = !context || busy;
  }, unmount() {
    close(); host.querySelector('#chat-share-dialog')?.close(); controller?.abort(); context = null;
    window.removeEventListener('resize', close);
    menu.remove();
  } };
}

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
