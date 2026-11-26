import { tr } from './i18n.js';
/* Название, дата и действия открытого диалога. */
import { el, words } from './ui.js';
import { iconButton } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { NAMES } from './sections-meta.js';
import { dateTime, dialogDateTime } from './time.js';

const COPIED_MS = 1500;          // как у «Копировать» ответа (chat.js)

export function chatTime(stamp) {
  const date = new Date(stamp * 1000);
  const visible = Number.isFinite(stamp) && stamp > 0 && Number.isFinite(date.getTime());
  return el('time', { class: 'chat-updated', hidden: !visible, datetime: visible ? date.toISOString() : null,
    title: visible ? dateTime(stamp) : null }, visible ? dialogDateTime(stamp) : '');
}

export function paintPin(button, pinned) {
  const label = pinned ? tr("Открепить") : tr("Закрепить");
  button.setAttribute('aria-pressed', String(!!pinned));
  button.setAttribute('aria-label', label);
  button.title = label;
}

export function chatTitle({ rename, remove, pinItem, said, prefix = 'chat', heading = 'h1',
  fallback = NAMES.chat, name = 'chat-title', href = (item) => `#/chat/${item.id}` }) {
  const id = `${prefix}-title`;
  const node = el(heading, { id, class: name }, fallback);
  const label = el('div', { class: 'chat-heading' }, node);
  let time = chatTime(null);
  label.append(time);
  const actions = iconButton('pencil', tr("Действия с диалогом"), { id: `${id}-actions`, class: 'icon-btn chat-title-actions',
    hidden: true });
  const pin = iconButton('star', tr("Закрепить"), { id: `${id}-pin`, class: 'icon-btn chat-title-pin', hidden: true });
  const menu = el('div', { id: `${id}-menu`, class: 'chat-menu', role: 'menu', 'aria-label': tr("Действия с диалогом") });
  // успех копирования — короткой видимой отметкой рядом с кнопкой, как «Копировать» → «Скопировано»
  const note = el('span', { id: `${id}-note`, class: 'chat-title-note muted small', role: 'status', hidden: true });
  let noteTimer = null;
  let current = null;
  pin.addEventListener('click', async () => {
    if (!current || pin.disabled) return;
    pin.disabled = true;
    try {
      await pinItem(current);
    } catch (err) {
      said(words(err));
    } finally {
      pin.disabled = false;
    }
  });
  const popup = popupMenu(actions, menu, { side: 'above', align: 'end', fill: (close) => {
    const item = current;
    const items = [
      menuItem(tr("Переименовать"), { attrs: { id: `${id}-rename` }, onSelect: () => { close(); rename(item); } }),
      menuItem(tr("Удалить"), { attrs: { id: `${id}-delete`, class: 'danger' }, onSelect: () => { close(); remove(item); } }),
      menuItem(tr("Скопировать адрес"), { attrs: { id: `${id}-copy` }, onSelect: async () => {
        close();
        try {
          await navigator.clipboard.writeText(new URL(href(item), location.origin).href);
        } catch {
          said(tr("Не удалось скопировать адрес"));      // отказ — видимой строкой раздела (#chat-error)
          return;
        }
        clearTimeout(noteTimer);
        note.textContent = tr("Адрес скопирован");
        note.hidden = false;
        noteTimer = setTimeout(() => { note.hidden = true; note.textContent = ''; }, COPIED_MS);
      } })];
    return [items, items];
  } });
  return {
    node, label, actions, pin, menu, note,
    /** Строка открытого диалога (`chat.chat`) или null — новый диалог. */
    set(chat) {
      if (current?.id !== chat?.id) {
        popup.close();
        clearTimeout(noteTimer);
        note.hidden = true;
        note.textContent = '';
      }
      if (current?.updated !== chat?.updated) {
        const next = chatTime(chat?.updated);
        time.replaceWith(next);
        time = next;
      }
      current = chat;
      const text = chat ? chat.title || tr("Новый диалог") : fallback;
      if (node.textContent !== text) node.textContent = text;
      if (chat) node.title = text;
      else node.removeAttribute('title');
      actions.hidden = !chat;
      pin.hidden = !chat || !pinItem;
      if (!pin.hidden) paintPin(pin, chat.pinned);
    },
    unmount() {
      clearTimeout(noteTimer);
      popup.unmount();
    },
  };
}
