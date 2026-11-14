import { tr } from './i18n.js';
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
import { NAMES, sectionPairs } from './sections-meta.js';
import { button, el, field, openDialog, showError } from './ui.js';

const KINDS = {
  openai: { label: tr("OpenAI-совместимый"), placeholder: tr("адрес API, https, оканчивается на /v1") },
  srvllm: { label: tr("Шлюз srvllm"), placeholder: tr("адрес шлюза, http, IP и порт, без пути") },
  s06: { label: tr("Шлюз s06 · Qwen, WhisperX, PaddleOCR"), placeholder: tr("адрес шлюза, IP и порт, без пути /v1") },
  cleaner: { label: tr("Cleaner · очистка данных"), placeholder: tr("адрес Cleaner, HTTPS, IP и порт, без пути") },
};
/** Разделы подключений (все, кроме «Агентов» — им модели сообщает мост) в порядке рельса и голосовой ввод. */
export const SECTION_NAMES = { ...Object.fromEntries(sectionPairs().filter(([id]) => id !== 'code')),
  dictation: NAMES.dictation };
const ERROR_FIELD = { name: 'name', token: 'token' };   // прочие коды — под адресом

export function sectionsText(sections) {
  return Object.keys(SECTION_NAMES).filter((s) => sections.includes(s)).map((s) => SECTION_NAMES[s]).join(', ');
}

export function connectionForm(initial, { cleaner = false } = {}) {
  const name = el('input', { class: 'input', name: 'name', maxlength: '64', autocomplete: 'off', required: true });
  const kind = el('select', { class: 'input', name: 'kind' },
    ...Object.entries(KINDS).filter(([value]) => !cleaner || value === 'cleaner')
      .map(([value, k]) => el('option', { value }, k.label)));
  const url = el('input', { class: 'input', name: 'base_url', maxlength: '2048', autocomplete: 'off',
    inputmode: 'url', spellcheck: 'false', autocapitalize: 'none' });
  const token = el('input', { class: 'input', name: 'token', type: 'password', maxlength: '512',
    autocomplete: 'new-password', spellcheck: 'false', autocapitalize: 'none',
    placeholder: initial ? tr("не менять ({0})", initial.token_hint) : tr("ключ API") });
  const cleanerNote = el('p', { class: 'muted small', hidden: true }, tr("Cleaner: только чат и перевод через сервер. Нужен ключ Cleaner с доступом к подключению и моделям; адрес должен разрешить администратор."));
  const s06Note = el('p', { class: 'muted small', hidden: true }, tr("s06: чат и перевод Qwen, разговоры WhisperX, сканы PaddleOCR и голосовой ввод. Озвучки и координат текста для перевода снимка у этого шлюза нет."));
  const box = el('form', { class: 'conn-form', novalidate: true },
    field(tr("Название"), name, 'name'), field(tr("Вид"), kind, 'kind'),
    field(tr("Адрес"), url, 'base_url'),
    el('p', { class: 'muted small' }, tr("Адреса из списка администратора студия зовёт сама, остальные — ваш браузер. Фото, видео и речь — только через студию.")),
    field(tr("Токен"), token, 'token'), cleanerNote, s06Note);
  box.addEventListener('submit', (event) => event.preventDefault());

  kind.value = initial ? (initial.cleaner ? 'cleaner' : initial.s06 ? 's06' : initial.kind) : (cleaner ? 'cleaner' : 'openai');
  name.value = initial ? initial.name : (cleaner ? 'Cleaner' : '');
  url.value = initial ? initial.base_url : '';
  url.placeholder = KINDS[kind.value].placeholder;
  cleanerNote.hidden = kind.value !== 'cleaner';
  s06Note.hidden = kind.value !== 's06';
  kind.addEventListener('change', () => {
    url.placeholder = KINDS[kind.value].placeholder;
    cleanerNote.hidden = kind.value !== 'cleaner';
    s06Note.hidden = kind.value !== 's06';
  });

  return {
    el: box,
    value() {
      const out = { name: name.value.trim(), kind: ['cleaner', 's06'].includes(kind.value) ? 'openai' : kind.value,
        cleaner: kind.value === 'cleaner', s06: kind.value === 's06', base_url: url.value.trim() };
      if (!initial || token.value.trim()) out.token = token.value.trim();
      return out;
    },
    showError(err) { showError(box, err, ERROR_FIELD, 'base_url'); },
  };
}

/** Окно правки: «Сохранить» (`#conn-save`) шлёт POST или PATCH, отказ — в форму, успех — onSaved(conn). */
export function editConnection(host, base, initial, onSaved) {
  const form = connectionForm(initial);
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'conn-save' });
  const dialog = openDialog(host, { id: 'conn-dialog', title: initial ? tr("Подключение") : tr("Новое подключение"),
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
