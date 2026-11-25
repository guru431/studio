import { language, tr } from './i18n.js';
/* One current snapshot per chat. Every creation rotates both the link and viewer access.
 * Окно владельца: PIN, срок ссылки (1/7/30 дней или бессрочно, по умолчанию 7), у обычного чата — показывать ли
 * рассуждение модели (по умолчанию нет); у созданной — срок, открытия, последний просмотр и неверные PIN. */
import { api } from './api.js';
import { selectedChatHTML } from './chat-selection.js';
import { svg } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, plural, words } from './ui.js';


/** Меню «Поделиться и экспортировать» — общий компонент menu.js: popover, стрелки, Escape только этого меню.
 * `archive` — пункт «архив ZIP с файлами» (обычный чат): POST `context.archive` → токен, загрузка — ссылкой по нему.
 * `context.blocked` — причина словами, почему ни поделиться, ни скачать нельзя (сработал защищённый источник —
 * сервер ответил бы 409 `protected`): кнопка выключена, причина — в подсказке. */
const TRIGGER_TITLE = tr("Поделиться и экспортировать чат");

export function shareMenu(host, { id, content = null, request = api, failed = () => {}, archive = false }) {
  const trigger = button('', { id, class: 'icon-btn chat-share-trigger', 'aria-label': TRIGGER_TITLE,
    title: TRIGGER_TITLE, 'aria-haspopup': 'menu', 'aria-expanded': 'false', disabled: true });
  trigger.innerHTML = svg('share');   // константа icons.js
  const menu = el('div', { id: `${id}-menu`, class: 'chat-share-menu', role: 'menu', 'aria-label': tr("Поделиться чатом") });
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
  const items = [['link', tr("Поделиться ссылкой")], ['pdf', tr("Скачать PDF")],
    ['selection', tr("Скачать выделенное в PDF")], ['md', tr("Скачать Markdown")],
    ...(archive ? [['zip', tr("Скачать архив ZIP с файлами")]] : [])].map(([key, label]) => {
    const item = menuItem(label, { attrs: { 'data-action': key } });
    item.addEventListener('click', async () => {
      const current = context;
      if (!current || busy) return;
      const fragment = key === 'selection' ? selected : null;
      if ((key === 'selection' && !fragment) || (key === 'link' && !current.share) || (key === 'zip' && !current.archive)) return;
      close();
      if (key === 'link') { shareDialog(host, current.share, current.title, request, { kind: current.kind ?? null }); return; }
      busy = true;
      trigger.disabled = true;
      trigger.setAttribute('aria-busy', 'true');
      controller = new AbortController();
      try {
        if (key === 'zip') {
          // архив собирается потоком при загрузке: здесь — только токен (отказ — словами), дальше — ссылка
          const { token } = await request('POST', current.archive, { lang: language() }, { signal: controller.signal });
          if (context !== current || !trigger.isConnected) return;
          const download = el('a', { href: `/api/chats/archive/${encodeURIComponent(token)}`, download: '' });
          document.body.append(download);
          download.click();
          download.remove();
          return;
        }
        const format = key === 'selection' ? 'pdf' : key;
        const response = await request('POST', current.export, { format, title: current.title, lang: language(),
          ...(fragment ? { selection: fragment } : {}) }, { signal: controller.signal });
        const blob = await response.blob();
        if (context !== current || !trigger.isConnected) return;
        const href = URL.createObjectURL(blob);
        const name = (current.title || tr("Чат")).replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, '_').slice(0, 100);
        const download = el('a', { href, download: `${name}${fragment ? tr(" — выделенное") : ''}.${format}` });
        document.body.append(download);
        download.click();
        download.remove();
        setTimeout(() => URL.revokeObjectURL(href), 60000);
      } catch (err) {
        if (context === current && err.name !== 'AbortError') failed(err);
      } finally {
        busy = false;
        controller = null;
        trigger.disabled = !context || !!context.blocked;
        trigger.removeAttribute('aria-busy');
      }
    });
    return item;
  });
  const selectionItem = items[2];
  selectionItem.disabled = true;
  selectionItem.title = tr("Выделите текст в ленте чата, затем откройте это меню");
  menu.append(...items);
  document.addEventListener('selectionchange', capture);
  document.addEventListener('pointerdown', pointer, true);
  document.addEventListener('visibilitychange', clear);
  document.addEventListener('studio-code-clear', clear);
  const popup = popupMenu(trigger, menu, { align: 'end', blocked: () => !context || busy || !!context.blocked });
  return { button: trigger, set(value) {
    if (context?.share === value?.share && context?.export === value?.export && context?.title === value?.title
      && context?.archive === value?.archive && context?.blocked === value?.blocked) return;
    close();
    host.querySelector('#chat-share-dialog')?.close();
    controller?.abort();
    clear();
    context = value;
    // без `share` — только экспорт: участник проекта видит чаты владельца, но ссылкой делится владелец
    items[0].hidden = !context?.share;
    trigger.disabled = !context || busy || !!context.blocked;
    trigger.title = context?.blocked || TRIGGER_TITLE;
    trigger.setAttribute('aria-label', context?.blocked ? `${TRIGGER_TITLE}: ${context.blocked}` : TRIGGER_TITLE);
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
const LIFETIMES = [['1', tr("1 день")], ['7', tr("7 дней")], ['30', tr("30 дней")], ['', tr("Бессрочно")]];

function when(ts) {
  return dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}

/** Сведения ссылки для владельца: срок, открытия по PIN и неверные PIN (их предел отключает ссылку). */
function facts(out) {
  return [out.expires ? tr("действует до {0}", when(out.expires)) : tr("бессрочная"),
    tr("открыта {0} {1}", out.views || 0, plural(out.views || 0, [tr("раз"), tr("раза"), tr("раз")]))
      + (out.viewed ? tr(", последний — {0}", when(out.viewed)) : ''),
    tr("неверных PIN: {0}", out.fails || 0)].join(' · ');
}

/** Записи других разделов (chat_shares.ITEMS): заголовок окна, что увидит получатель и, где есть исходный файл,
 * подпись «приложить его» (в ссылку — только по выбору). */
const KINDS = {
  transcripts: { title: tr("Поделиться расшифровкой"), source: tr("Приложить запись разговора"),
    intro: tr("Получатель увидит текст расшифровки с собеседниками и временем, только для чтения. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
  translations: { title: tr("Поделиться переводом"), source: tr("Приложить исходный файл"),
    intro: tr("Получатель увидит исходный текст и перевод, только для чтения. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
  scans: { title: tr("Поделиться сканом"), source: tr("Приложить исходный файл"),
    intro: tr("Получатель увидит распознанный текст страниц, только для чтения. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
  images: { title: tr("Поделиться картинкой"),
    intro: tr("Получатель увидит картинку и её промпт, только для чтения. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
  videos: { title: tr("Поделиться роликом"),
    intro: tr("Получатель увидит ролик и его промпт, только для чтения. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
  speech_chats: { title: tr("Поделиться чатом озвучки"),
    intro: tr("Получатель увидит тексты готовых озвучек по порядку, сможет их прослушать и скачать, только для чтения. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
  owui: { title: tr("Поделиться чатом Open WebUI"),
    intro: tr("Получатель увидит снимок переписки, только для чтения; файлы — только именами, сами они остаются в Open WebUI. При каждом открытии нужен PIN. Передайте его отдельно от ссылки.") },
};

/** Окно ссылки. `kind` — запись раздела (KINDS), `source` — есть ли у неё исходный файл, который можно приложить.
 * «Обновить снимок» (PUT) — то же содержимое заново под тем же адресом, сроком и PIN; «Поделиться…» — системное окно
 * (`navigator.share`), только где оно есть. */
export function shareDialog(host, endpoint, title = tr("Чат"), request = api, { kind = null, source = false } = {}) {
  const pin = el('input', { id: 'chat-share-pin', class: 'input', type: 'password', inputmode: 'numeric',
    pattern: '[0-9]{4}', minlength: '4', maxlength: '4', autocomplete: 'off', required: true, disabled: true });
  const error = el('p', { class: 'error', role: 'alert' });
  const link = el('input', { id: 'chat-share-link', class: 'input', readonly: true,
    'aria-label': kind ? tr("Ссылка на запись") : tr("Ссылка на чат") });
  const copy = button(tr("Копировать ссылку"), { id: 'chat-share-copy' });
  const native = typeof navigator.share === 'function'
    ? button(tr("Поделиться…"), { id: 'chat-share-native' }) : null;
  const info = el('p', { class: 'muted small', id: 'chat-share-facts' });
  const broken = el('p', { class: 'error', id: 'chat-share-broken', hidden: true },
    tr("Ссылку больше не открыть: ключ данных студии сменили. Создайте новую или отключите эту."));
  const linkBox = el('div', {}, field(tr("Ссылка для чтения"), link, 'share-link'), el('div', { class: 'row' }, copy, native));
  const output = el('div', { hidden: true }, linkBox, broken, info);
  const save = button(tr("Создать ссылку"), { id: 'chat-share-create', type: 'submit', class: 'btn btn-accent', disabled: true });
  const refresh = button(tr("Обновить снимок"), { id: 'chat-share-refresh', class: 'btn', hidden: true });
  const revoke = button(tr("Отключить ссылку"), { id: 'chat-share-revoke', class: 'btn btn-danger', hidden: true });
  const days = el('select', { id: 'chat-share-days', class: 'input', disabled: true },
    ...LIFETIMES.map(([value, label]) => el('option', { value, selected: value === '7' }, label)));
  // рассуждение модели есть только у обычного чата; в ссылку — лишь по явному выбору
  const regular = endpoint.startsWith('/api/chats/');
  const reasoning = el('input', { type: 'checkbox', id: 'chat-share-reasoning', disabled: true });
  const text = KINDS[kind];
  const attach = el('input', { type: 'checkbox', id: 'chat-share-source', disabled: true });
  const options = () => ({ title, ...(regular ? { reasoning: reasoning.checked } : {}),
    ...(text?.source && source ? { source: attach.checked } : {}) });
  const form = el('form', {}, field(tr("PIN доступа · 4 цифры"), pin, 'share-pin'), field(tr("Срок ссылки"), days, 'share-days'),
    regular ? el('label', { class: 'check' }, reasoning, tr("Показать рассуждение модели")) : null,
    text?.source && source ? el('label', { class: 'check' }, attach, text.source) : null, save);
  const dialog = openDialog(host, { id: 'chat-share-dialog', title: text ? text.title : tr("Поделиться чатом"), cancelText: tr("Закрыть"),
    body: [el('p', {}, text ? text.intro : tr("Получатель увидит снимок всей переписки и вложения, только для чтения. При каждом открытии нужен PIN этого чата. Передайте его отдельно от ссылки.")),
    el('p', { class: 'muted small' }, tr("Новая ссылка отключит прежнюю. «Обновить снимок» заменит содержимое под тем же адресом и PIN, само оно не обновляется. Снимок хранится зашифрованным и удаляется по истечении срока; после 50 неверных PIN ссылка отключится сама.")),
    form, output, error], actions: [refresh, revoke] });
  let busy = true;
  const state = (out) => {
    copy.textContent = tr("Копировать ссылку");
    output.hidden = revoke.hidden = !out.enabled;
    refresh.hidden = !(out.enabled && out.url);
    link.value = out.url || '';
    linkBox.hidden = out.enabled && !out.url;
    broken.hidden = !(out.enabled && !out.url);
    info.textContent = out.enabled ? facts(out) : '';
    save.textContent = out.enabled ? tr("Создать новую ссылку") : tr("Создать ссылку");
  };
  const pending = (value) => {
    busy = value;
    save.disabled = revoke.disabled = refresh.disabled = pin.disabled = days.disabled = reasoning.disabled
      = attach.disabled = value;
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
      const out = await request('POST', endpoint, { pin: pin.value, days: days.value ? Number(days.value) : null,
        ...options() });
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
  refresh.addEventListener('click', async () => {
    if (busy) return;
    pending(true);
    error.textContent = '';
    try {
      const out = await request('PUT', endpoint, options());
      if (!dialog.isConnected) return;
      state(out);
      info.textContent = `${tr("Снимок обновлён")} · ${info.textContent}`;
    } catch (err) {
      if (dialog.isConnected) error.textContent = words(err);
    } finally { if (dialog.isConnected) pending(false); }
  });
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(link.value); copy.textContent = tr("Скопировано"); }
    catch { link.focus(); link.select(); error.textContent = tr("Скопируйте выделенную ссылку"); }
  });
  // системное «Поделиться»: отмена пользователем (AbortError) — не ошибка
  native?.addEventListener('click', async () => {
    try { await navigator.share({ title: title || tr("Чат"), url: link.value }); }
    catch (err) { if (err.name !== 'AbortError') error.textContent = tr("Не удалось открыть окно «Поделиться»"); }
  });
  dialog.addEventListener('close', () => { pin.value = ''; link.value = ''; });
  return dialog;
}
