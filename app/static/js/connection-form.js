/* Подключение: форма и окно правки — общие для настроек и панели администратора.
 *
 * connectionForm(initial?) → { el, value(), showError(err) }: название, вид (OpenAI-совместимый /
 * шлюз srvllm), адрес, токен (при правке пустой — «не менять»). Разделы подключения ставят
 * настройки раздела (settings.js), а не эта форма. Отказ сохранения — словами под полем: токен (в
 * том числе «новый адрес — введите токен заново») и название — под своими полями, прочее — под адресом.
 *
 * editConnection(host, base, initial, onSaved) — окно «Подключение»: `base` — '/api/connections'
 * своих или '/api/admin/users/<id>/connections' панели. */
import { api } from './api.js';
import { button, el, field, openDialog, showError } from './ui.js';

const KINDS = {
  openai: { label: 'OpenAI-совместимый', placeholder: 'адрес API, https, оканчивается на /v1' },
  srvllm: { label: 'Шлюз srvllm', placeholder: 'адрес шлюза, http, IP и порт, без пути' },
  cleaner: { label: 'Cleaner · защита данных', placeholder: 'адрес Cleaner, HTTPS, IP и порт, без пути' },
};
export const SECTION_NAMES = { chat: 'Чат', photo: 'Фото', video: 'Видео', speech: 'Речь',
  transcribe: 'Разговоры', ocr: 'Сканы', translate: 'Перевод', dictation: 'Голосовой ввод' };
const ERROR_FIELD = { name: 'name', token: 'token' };   // прочие коды — под адресом

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
  const cleanerNote = el('p', { class: 'muted small', hidden: true }, 'Cleaner: только чат и перевод через сервер. '
    + 'Нужен ключ Cleaner с доступом к подключению и моделям; адрес должен разрешить администратор.');
  const box = el('form', { class: 'conn-form', novalidate: true },
    field('Название', name, 'name'), field('Вид', kind, 'kind'),
    field('Адрес', url, 'base_url'),
    el('p', { class: 'muted small' }, 'Адреса из списка администратора студия зовёт сама, остальные — ваш браузер. '
      + 'Фото, видео и речь — только через студию.'),
    field('Токен', token, 'token'), cleanerNote);
  box.addEventListener('submit', (event) => event.preventDefault());

  kind.value = initial ? (initial.cleaner ? 'cleaner' : initial.kind) : 'openai';
  name.value = initial ? initial.name : '';
  url.value = initial ? initial.base_url : '';
  url.placeholder = KINDS[kind.value].placeholder;
  cleanerNote.hidden = kind.value !== 'cleaner';
  kind.addEventListener('change', () => {
    url.placeholder = KINDS[kind.value].placeholder;
    cleanerNote.hidden = kind.value !== 'cleaner';
  });

  return {
    el: box,
    value() {
      const out = { name: name.value.trim(), kind: kind.value === 'cleaner' ? 'openai' : kind.value,
        cleaner: kind.value === 'cleaner', base_url: url.value.trim() };
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
    if (event.key === 'Enter' && event.target.tagName === 'INPUT') submit();
  });
}
