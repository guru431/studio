/* One current snapshot per chat. Every creation rotates both the link and viewer access.
 * Окно владельца: PIN, срок ссылки (1/7/30 дней или бессрочно, по умолчанию 7), у обычного чата — показывать ли
 * рассуждение модели (по умолчанию нет); у созданной — срок, открытия, последний просмотр и неверные PIN. */
import { api } from './api.js';
import { selectedChatHTML } from './chat-selection.js';
import { svg } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, plural, words } from './ui.js';


/** Меню «Поделиться и экспортировать» — общий компонент menu.js: popover, стрелки, Escape только этого меню. */
export function shareMenu(host, { id, content = null, request = api, failed = () => {} }) {
  const trigger = button('', { id, class: 'icon-btn chat-share-trigger', 'aria-label': 'Поделиться и экспортировать чат',
    title: 'Поделиться и экспортировать чат', 'aria-haspopup': 'menu', 'aria-expanded': 'false', disabled: true });
  trigger.innerHTML = svg('share');   // константа icons.js
  const menu = el('div', { id: `${id}-menu`, class: 'chat-share-menu', role: 'menu', 'aria-label': 'Поделиться чатом' });
  host.append(menu);
  let context = null;
  let controller = null;
  let busy = false;
  let selected = null;
  const capture = () => {
    if (menu.matches(':popover-open')) return;
    if (document.querySelector(':popover-open') && !content?.contains(document.activeElement)) return;
    selected = context ? selectedChatHTML(content) : null;
    selectionItem.disabled = !selected;
  };
  // Capture before focus changes (also covers the Agents toolbar's overflow menu on phones).
  const pointer = (event) => { if (!menu.contains(event.target)) capture(); };
  const clear = () => { selected = null; selectionItem.disabled = true; };
  const close = () => popup.close();
  const items = [['link', 'Поделиться ссылкой'], ['pdf', 'Скачать PDF'],
    ['selection', 'Скачать выделенное в PDF'], ['md', 'Скачать Markdown']].map(([key, label]) => {
    const item = menuItem(label, { attrs: { 'data-action': key } });
    item.addEventListener('click', async () => {
      const current = context;
      if (!current || busy) return;
      const fragment = key === 'selection' ? selected : null;
      if (key === 'selection' && !fragment) return;
      close();
      if (key === 'link') { shareDialog(host, current.share, current.title, request); return; }
      busy = true;
      trigger.disabled = true;
      trigger.setAttribute('aria-busy', 'true');
      controller = new AbortController();
      try {
        const format = key === 'selection' ? 'pdf' : key;
        const response = await request('POST', current.export, { format, title: current.title,
          ...(fragment ? { selection: fragment } : {}) }, { signal: controller.signal });
        const blob = await response.blob();
        if (context !== current || !trigger.isConnected) return;
        const href = URL.createObjectURL(blob);
        const name = (current.title || 'Чат').replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, '_').slice(0, 100);
        const download = el('a', { href, download: `${name}${fragment ? ' — выделенное' : ''}.${format}` });
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
  const selectionItem = items[2];
  selectionItem.disabled = true;
  selectionItem.title = 'Выделите текст в ленте чата, затем откройте это меню';
  menu.append(...items);
  document.addEventListener('selectionchange', capture);
  document.addEventListener('pointerdown', pointer, true);
  document.addEventListener('visibilitychange', clear);
  document.addEventListener('studio-code-clear', clear);
  const popup = popupMenu(trigger, menu, { align: 'end', blocked: () => !context || busy });
  return { button: trigger, set(value) {
    if (context?.share === value?.share && context?.export === value?.export && context?.title === value?.title) return;
    close();
    host.querySelector('#chat-share-dialog')?.close();
    controller?.abort();
    clear();
    context = value;
    trigger.disabled = !context || busy;
  }, unmount() {
    clear();
    document.removeEventListener('selectionchange', capture);
    document.removeEventListener('pointerdown', pointer, true);
    document.removeEventListener('visibilitychange', clear);
    document.removeEventListener('studio-code-clear', clear);
    host.querySelector('#chat-share-dialog')?.close(); controller?.abort(); context = null;
    popup.unmount();
  } };
}

/** Срок ссылки (chat_shares.DAYS): неделя по умолчанию, бессрочная — только явным выбором. */
const LIFETIMES = [['1', '1 день'], ['7', '7 дней'], ['30', '30 дней'], ['', 'Бессрочно']];

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}

/** Сведения ссылки для владельца: срок, открытия по PIN и неверные PIN (их предел отключает ссылку). */
function facts(out) {
  return [out.expires ? `действует до ${when(out.expires)}` : 'бессрочная',
    `открыта ${out.views || 0} ${plural(out.views || 0, ['раз', 'раза', 'раз'])}`
      + (out.viewed ? `, последний — ${when(out.viewed)}` : ''),
    `неверных PIN: ${out.fails || 0}`].join(' · ');
}

export function shareDialog(host, endpoint, title = 'Чат', request = api) {
  const pin = el('input', { id: 'chat-share-pin', class: 'input', type: 'password', inputmode: 'numeric',
    pattern: '[0-9]{4}', minlength: '4', maxlength: '4', autocomplete: 'off', required: true, disabled: true });
  const error = el('p', { class: 'error', role: 'alert' });
  const link = el('input', { id: 'chat-share-link', class: 'input', readonly: true, 'aria-label': 'Ссылка на чат' });
  const copy = button('Копировать ссылку', { id: 'chat-share-copy' });
  const info = el('p', { class: 'muted small', id: 'chat-share-facts' });
  const broken = el('p', { class: 'error', id: 'chat-share-broken', hidden: true },
    'Ссылку больше не открыть: ключ данных студии сменили. Создайте новую или отключите эту.');
  const linkBox = el('div', {}, field('Ссылка для чтения', link, 'share-link'), copy);
  const output = el('div', { hidden: true }, linkBox, broken, info);
  const save = button('Создать ссылку', { id: 'chat-share-create', type: 'submit', class: 'btn btn-accent', disabled: true });
  const revoke = button('Отключить ссылку', { id: 'chat-share-revoke', class: 'btn btn-danger', hidden: true });
  const days = el('select', { id: 'chat-share-days', class: 'input', disabled: true },
    ...LIFETIMES.map(([value, label]) => el('option', { value, selected: value === '7' }, label)));
  // рассуждение модели есть только у обычного чата; в ссылку — лишь по явному выбору
  const regular = endpoint.startsWith('/api/chats/');
  const reasoning = el('input', { type: 'checkbox', id: 'chat-share-reasoning', disabled: true });
  const form = el('form', {}, field('PIN доступа · 4 цифры', pin, 'share-pin'), field('Срок ссылки', days, 'share-days'),
    regular ? el('label', { class: 'check' }, reasoning, 'Показать рассуждение модели') : null, save);
  const dialog = openDialog(host, { id: 'chat-share-dialog', title: 'Поделиться чатом', cancelText: 'Закрыть',
    body: [el('p', {}, 'Получатель увидит снимок всей переписки и вложения, только для чтения. '
      + 'При каждом открытии нужен PIN этого чата. Передайте его отдельно от ссылки.'),
    el('p', { class: 'muted small' }, 'Новая ссылка отключит прежнюю. Новые сообщения в снимок не добавляются. '
      + 'Снимок хранится зашифрованным и удаляется по истечении срока; после 50 неверных PIN ссылка отключится сама.'),
    form, output, error], actions: [revoke] });
  let busy = true;
  const state = (out) => {
    copy.textContent = 'Копировать ссылку';
    output.hidden = revoke.hidden = !out.enabled;
    link.value = out.url || '';
    linkBox.hidden = out.enabled && !out.url;
    broken.hidden = !(out.enabled && !out.url);
    info.textContent = out.enabled ? facts(out) : '';
    save.textContent = out.enabled ? 'Создать новую ссылку' : 'Создать ссылку';
  };
  const pending = (value) => {
    busy = value;
    save.disabled = revoke.disabled = pin.disabled = days.disabled = reasoning.disabled = value;
  };
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
      const out = await request('POST', endpoint, { pin: pin.value, title,
        days: days.value ? Number(days.value) : null, ...(regular ? { reasoning: reasoning.checked } : {}) });
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
