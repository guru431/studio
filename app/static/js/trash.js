import { tr } from './i18n.js';
/* «Корзина» (#/trash, меню аватара): удалённые записи всех разделов (GET /api/trash, trash.py) по разделам в порядке
 * рельса. «Вернуть» — POST /api/trash/<вид>/<id>/restore; «Удалить навсегда» — DELETE той же записи после окна
 * с паролем учётки; «Очистить корзину» — POST /api/trash/empty (паузы в обоих случаях — как у
 * выключения блокировки). Сервер прячет разделы, закрытые блокировкой; действия при запертом разделе — 423 словами. */
import { api } from './api.js';
import { SECTIONS, sectionName } from './sections-meta.js';
import { sizeText } from './settings.js';
import { dateTime } from './time.js';
import { button, el, field, openDialog, words } from './ui.js';

const TRASH_DAYS = 14;                       // trash.TRASH_SEC: запись удалится сама через столько дней
const KIND_TAGS = { chat_media: tr("файл диалога"), voices: tr("голос"), knowledge_bases: tr("база знаний"),
  kb_documents: tr("документ базы знаний") };
let controller = null;

const moment = (ts) => dateTime(ts, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });

export function mount(view) {
  controller = new AbortController();
  const { signal } = controller;
  const host = view;
  const note = el('p', { id: 'trash-status', class: 'status', role: 'status', 'aria-live': 'polite' });
  const list = el('div', { id: 'trash-list', class: 'trash-list' });
  const empty = button(tr("Очистить корзину"), { class: 'btn btn-danger', id: 'trash-empty', hidden: true },
    () => emptyDialog());
  view.append(el('header', { class: 'head' }, el('h1', {}, sectionName('trash'))),
    el('div', { class: 'page trash-page' },
      el('p', { class: 'muted small' }, tr("Удалённое лежит здесь {0} дней, потом удаляется само. Вернуть может только владелец записи.", TRASH_DAYS)),
      el('div', { class: 'row' }, empty), note, list));

  const say = (text, kind) => {
    note.textContent = text;
    note.classList.toggle('error', kind === 'error');
    note.classList.toggle('ok', kind === 'ok');
  };

  function row(item) {
    const path = `/api/trash/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}`;
    const title = item.title_default ? tr(item.title) : item.title;
    const actions = el('div', { class: 'session-actions' });
    const act = async (method, url, done) => {
      for (const b of actions.querySelectorAll('button')) b.disabled = true;
      say('');
      try {
        await api(method, url, undefined, { signal });
        say(done, 'ok');
        await load();
      } catch (err) {
        if (signal.aborted) return;
        say(words(err), 'error');
        for (const b of actions.querySelectorAll('button')) b.disabled = false;
      }
    };
    actions.append(
      button(tr("Вернуть"), { class: 'btn btn-sm trash-restore', 'aria-label': tr("Вернуть: {0}", title) },
        () => act('POST', `${path}/restore`, tr("Возвращено: {0}", title))),
      button(tr("Удалить навсегда"), { class: 'btn btn-sm btn-danger trash-forever',
        'aria-label': tr("Удалить навсегда: {0}", title) }, () => {
        passwordDialog('forever', tr("Удалить навсегда?"), tr("Удалить навсегда"),
          [el('p', { class: 'trash-forever-title' }, title),
            el('p', {}, tr("Запись и её файлы удалятся сразу. Отменить нельзя."))], async (password) => {
          await api('DELETE', path, { password }, { signal });
          say(tr("Удалено навсегда: {0}", title), 'ok');
        });
      }));
    const facts = [KIND_TAGS[item.kind], tr("удалено {0}", moment(item.deleted)),
      tr("удалится {0}", moment(item.deleted + TRASH_DAYS * 86400)), item.bytes ? sizeText(item.bytes) : null];
    return el('li', { class: 'agent session trash-item', 'data-kind': item.kind, 'data-id': item.id },
      el('div', { class: 'session-info' }, el('b', {}, title),
        el('p', { class: 'muted small' }, facts.filter(Boolean).join(' · '))), actions);
  }

  async function load() {
    let items;
    try {
      items = await api('GET', '/api/trash', undefined, { signal });
    } catch (err) {
      if (!signal.aborted) say(words(err), 'error');
      return;
    }
    if (signal.aborted) return;
    const order = [...SECTIONS.map((s) => s.id), ...new Set(items.map((i) => i.section))];
    const groups = [...new Set(order)].map((id) => [id, items.filter((i) => i.section === id)]).filter(([, rows]) => rows.length);
    list.replaceChildren(...(groups.length ? groups.map(([id, rows]) => el('section', { class: 'trash-group', 'data-section': id },
      el('h2', {}, sectionName(id)), el('ul', { class: 'agents' }, ...rows.map(row))))
      : [el('p', { class: 'muted', id: 'trash-none' }, tr("Корзина пуста"))]));
    empty.hidden = !items.length;
  }

  function emptyDialog() {
    passwordDialog('empty', tr("Очистить корзину?"), tr("Очистить навсегда"),
      [el('p', {}, tr("Все записи корзины и их файлы удалятся сразу. Отменить нельзя. Введите пароль учётки."))],
      async (password) => {
        const { purged, left } = await api('POST', '/api/trash/empty', { password }, { signal });
        say(left ? tr("Удалено навсегда: {0}; не удалось сейчас: {1}", purged, left)
          : tr("Корзина очищена: {0}", purged), left ? 'error' : 'ok');
      });
  }

  function passwordDialog(kind, title, label, body, submitPassword) {
    say('');
    const password = el('input', { class: 'input', id: kind === 'empty' ? 'trash-password' : 'trash-item-password', type: 'password',
      autocomplete: 'current-password', maxlength: '1024' });
    const fail = el('p', { class: 'error', 'aria-live': 'polite' });
    const yes = button(label, { class: 'btn btn-danger solid', id: `trash-${kind}-confirm` });
    const dialog = openDialog(host, { id: `trash-${kind}-dialog`, title, actions: [yes],
      body: [...body, field(tr("Пароль учётки"), password, 'password'), fail] });
    const submit = async () => {
      if (yes.disabled) return;
      fail.textContent = '';
      if (!password.value) {
        fail.textContent = tr("Введите пароль учётки");
        password.focus();
        return;
      }
      yes.disabled = true;
      try {
        await submitPassword(password.value);
        password.value = '';
        dialog.close();
        await load();
      } catch (err) {
        if (signal.aborted) return;
        fail.textContent = words(err);
        yes.disabled = false;
      }
    };
    yes.addEventListener('click', submit);
    password.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    });
    password.focus();
  }

  load();
}

export function unmount() {
  controller?.abort();
  controller = null;
}
