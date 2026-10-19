/* Подключение: форма, окно правки и список — общие для профиля и панели администратора.
 *
 * connectionForm(initial?) → { el, value(), showError(err) }: название, вид (OpenAI-совместимый /
 * шлюз srvllm), адрес, токен (при правке пустой — «не менять»), разделы галочками по виду.
 * Отказ сохранения — словами под полем: токен (в том числе «новый адрес — введите токен заново»),
 * название и разделы — под своими полями, прочее — под адресом.
 *
 * connectionsBlock(host, base, onChange) → { el, reload() → items }: список `.conn` (название, адрес, разделы,
 * метка «через сервер» / «из браузера» по `proxied` ответа, «Изменить», «Удалить») и `#conn-add`.
 * `base` — '/api/connections' профиля или '/api/admin/users/<id>/connections' панели; после
 * сохранения или удаления — onChange(items). */
import { api } from './api.js';
import { button, el, field, openDialog, showError, words } from './ui.js';

const KINDS = {
  openai: { label: 'OpenAI-совместимый', sections: ['chat'],
    placeholder: 'адрес API, https, оканчивается на /v1' },
  srvllm: { label: 'Шлюз srvllm', sections: ['chat', 'photo', 'video'],
    placeholder: 'адрес шлюза, http, IP и порт, без пути' },
};
export const SECTION_NAMES = { chat: 'Чат', photo: 'Фото', video: 'Видео' };
const ERROR_FIELD = { name: 'name', token: 'token', sections: 'sections' };   // прочие коды — под адресом

export function sectionsText(sections) {
  return Object.keys(SECTION_NAMES).filter((s) => sections.includes(s)).map((s) => SECTION_NAMES[s]).join(', ');
}

export function connectionForm(initial) {
  const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off', required: true });
  const kind = el('select', { class: 'input', name: 'kind' },
    ...Object.entries(KINDS).map(([value, k]) => el('option', { value }, k.label)));
  const url = el('input', { class: 'input', name: 'base_url', maxlength: '2048', autocomplete: 'off',
    inputmode: 'url', spellcheck: 'false', autocapitalize: 'none' });
  const token = el('input', { class: 'input', name: 'token', type: 'password', maxlength: '512',
    autocomplete: 'new-password', spellcheck: 'false', autocapitalize: 'none',
    placeholder: initial ? `не менять (${initial.token_hint})` : 'ключ API' });
  const sections = el('div', { class: 'checks' });
  const box = el('form', { class: 'conn-form', novalidate: true },
    field('Название', name, 'name'), field('Вид', kind, 'kind'),
    field('Адрес', url, 'base_url'),
    el('p', { class: 'muted small' }, 'Адреса из списка администратора студия зовёт сама, остальные — ваш браузер.'),
    field('Токен', token, 'token'),
    el('div', { class: 'field' }, el('span', { class: 'label' }, 'Разделы'), sections,
      el('p', { class: 'error', 'data-for': 'sections', 'aria-live': 'polite' })));
  box.addEventListener('submit', (event) => event.preventDefault());

  function drawSections(checked) {
    sections.replaceChildren(...KINDS[kind.value].sections.map((s) => el('label', { class: 'check' },
      el('input', { type: 'checkbox', name: 'section', value: s, checked: checked.includes(s) }), SECTION_NAMES[s])));
  }

  kind.value = initial ? initial.kind : 'openai';
  name.value = initial ? initial.name : '';
  url.value = initial ? initial.base_url : '';
  url.placeholder = KINDS[kind.value].placeholder;
  drawSections(initial ? initial.sections : KINDS[kind.value].sections);
  kind.addEventListener('change', () => {
    url.placeholder = KINDS[kind.value].placeholder;
    drawSections(KINDS[kind.value].sections);
  });

  return {
    el: box,
    value() {
      const out = { name: name.value.trim(), kind: kind.value, base_url: url.value.trim(),
        sections: [...sections.querySelectorAll('input:checked')].map((c) => c.value) };
      if (!initial || token.value.trim()) out.token = token.value.trim();
      return out;
    },
    showError(err) { showError(box, err, ERROR_FIELD, 'base_url'); },
  };
}

/** Окно правки: «Сохранить» (`#conn-save`) шлёт POST или PATCH, отказ — в форму, успех — onSaved(conn). */
export function editConnection(host, base, initial, onSaved) {
  const form = connectionForm(initial);
  const save = button('Сохранить', { class: 'btn btn-accent', id: 'conn-save' });
  const dialog = openDialog(host, { id: 'conn-dialog', title: initial ? 'Подключение' : 'Новое подключение',
    body: [form.el], actions: [save] });
  form.el.querySelector('[name=name]').focus();
  async function submit() {
    if (save.disabled) return;                  // Enter во время сохранения — не второй запрос
    save.disabled = true;
    try {
      const conn = initial ? await api('PATCH', `${base}/${initial.id}`, form.value())
        : await api('POST', base, form.value());
      dialog.close();
      onSaved(conn);
    } catch (err) {
      form.showError(err);
    } finally {
      save.disabled = false;
    }
  }
  save.addEventListener('click', submit);
  form.el.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target.tagName === 'INPUT' && event.target.type !== 'checkbox') submit();
  });
}

export function connectionsBlock(host, base, onChange) {
  const list = el('ul', { class: 'conn-list' });
  const status = el('p', { class: 'error', 'aria-live': 'polite' });
  const add = button('Добавить подключение', { class: 'btn', id: 'conn-add' },
    () => editConnection(host, base, null, changed));
  const root = el('div', { class: 'conns' }, list, status, add);

  async function changed() {
    const items = await reload();
    if (onChange) onChange(items);
  }

  function remove(conn) {
    const yes = button('Удалить', { class: 'btn btn-danger', id: 'conn-delete-confirm' });
    const fail = el('p', { class: 'error' });
    const dialog = openDialog(host, { id: 'conn-delete-dialog', title: 'Удалить подключение?',
      body: [el('p', {}, `«${conn.name}» — ${conn.base_url}`), fail], actions: [yes] });
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await api('DELETE', `${base}/${conn.id}`);
        dialog.close();
        changed();
      } catch (err) {
        fail.textContent = words(err);
        yes.disabled = false;
      }
    });
  }

  function item(conn) {
    return el('li', { class: 'conn', 'data-id': String(conn.id) },
      el('div', { class: 'conn-main' },
        el('b', { class: 'conn-name' }, conn.name),
        el('span', { class: 'conn-url' }, conn.base_url),
        el('span', { class: 'conn-meta' },
          el('span', { class: `tag conn-tag${conn.proxied ? ' server' : ''}` }, conn.proxied ? 'через сервер' : 'из браузера'),
          el('span', { class: 'conn-sections' }, sectionsText(conn.sections)))),
      el('div', { class: 'conn-actions' },
        button('Изменить', { class: 'btn btn-sm conn-edit' }, () => editConnection(host, base, conn, changed)),
        button('Удалить', { class: 'btn btn-sm btn-danger conn-delete' }, () => remove(conn))));
  }

  async function reload() {
    try {
      const { items } = await api('GET', base);
      status.textContent = '';
      list.replaceChildren(...(items.length ? items.map(item)
        : [el('li', { class: 'muted conn-empty' }, 'Подключений пока нет')]));
      return items;
    } catch (err) {
      status.textContent = words(err);
      return [];
    }
  }

  return { el: root, reload };
}
