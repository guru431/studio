import { tr } from './i18n.js';
import { api } from './api.js';
import { button, el, openDialog, words } from './ui.js';
import { mark, place } from './project-mirrors.js';

/* «Зеркала проектов» в «Настройки» → «Агенты»: все копии компьютеров учётки (GET /api/code/mirrors) по компьютерам —
 * проект (папка копии), «проекта больше нет» (папку переименовали или удалили), флажок «После хода»
 * (PUT /api/code/mirrors/<id>/auto) и «Отвязать» (DELETE /api/code/mirrors/<id>, папка остаётся); под проектом —
 * остальные копии группы с пометкой синхронизации с этой копией. */
export function mirrorsBlock(host) {
  const list = el('div', { id: 'mirror-list' });
  const note = el('p', { class: 'status', role: 'status' });

  function unlink(item) {
    const yes = button(tr("Отвязать"), { class: 'btn btn-danger', id: 'mirror-unlink-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'mirror-unlink-dialog', title: tr("Отвязать копию?"), actions: [yes], body: [
      el('p', {}, tr("Снимается только настройка зеркала: папка и файлы останутся на компьютере. Если в группе останется одна копия, зеркало исчезнет целиком.")),
      el('p', { class: 'muted' }, `«${place(item)}»`), fail] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `/api/code/mirrors/${item.id}`);
        dialog.close();
        load();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function copyRow(item) {
    const auto = el('input', { type: 'checkbox', class: 'mirror-auto', checked: item.auto });
    auto.addEventListener('change', async () => {
      auto.disabled = true;
      note.textContent = '';
      try {
        await api('PUT', `/api/code/mirrors/${item.id}/auto`, { auto: auto.checked });
      } catch (err) {
        auto.checked = !auto.checked;
        note.textContent = words(err);
      } finally {
        auto.disabled = false;
      }
    });
    const title = item.reenter ? tr("настройка недоступна") : item.name || tr("адрес копии уточнится при сравнении");
    return el('li', { class: 'mirror-copy', 'data-copy': String(item.id) },
      el('div', { class: 'mirror-copy-head' },
        el('b', { class: 'mirror-name' }, title),
        item.missing ? el('span', { class: 'tag mirror-missing' }, tr("проекта больше нет")) : null),
      item.folder ? el('div', { class: 'muted small mirror-folder' }, item.folder) : null,
      el('ul', { class: 'mirror-peers' }, ...item.peers.map((peer) =>
        el('li', { class: 'small' }, `${place(peer)} · ${mark(peer)}`))),
      el('div', { class: 'row' },
        el('label', { class: 'check', title: tr("После успешного хода в этом проекте отправлять изменения в остальные копии") },
          auto, tr("Отправлять после хода")),
        button(tr("Отвязать"), { class: 'btn btn-sm btn-danger mirror-unlink' }, () => unlink(item))));
  }

  async function load() {
    try {
      const { items } = await api('GET', '/api/code/mirrors');
      const computers = new Map();
      for (const item of items) {
        if (!computers.has(item.agent)) computers.set(item.agent, { name: item.computer, online: item.online, copies: [] });
        computers.get(item.agent).copies.push(item);
      }
      list.replaceChildren(...(items.length ? [...computers.values()].map((computer) =>
        el('section', { class: 'mirror-computer' },
          el('h3', {}, computer.name, ' ', el('span', { class: `tag${computer.online ? ' server' : ''}` },
            computer.online ? tr("на связи") : tr("не на связи"))),
          el('ul', { class: 'mirror-copies' }, ...computer.copies.map(copyRow))))
        : [el('p', { class: 'muted small' }, tr("Зеркал пока нет: их добавляют кнопкой «Зеркало» в шапке проекта."))]));
    } catch (err) {
      note.textContent = words(err);
    }
  }

  load();
  return el('section', { class: 'block mirrors-block' }, el('h2', {}, tr("Зеркала проектов")),
    el('p', { class: 'muted small' }, tr("Все копии проектов на компьютерах этой учётной записи. Отправка после хода переносит изменения сама, только если в копии нет своих изменений и ничего не удаляется.")),
    list, note);
}
