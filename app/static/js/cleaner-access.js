import { api } from './api.js';
import { tr } from './i18n.js';
import { el, field, words } from './ui.js';

export function cleanerAccessBlock() {
  const endpoint = '/api/cleaner/manual/access';
  const address = el('input', { class: 'input', required: true, type: 'url', name: 'address', autocomplete: 'off',
    id: 'cleaner-manual-address' });
  const key = el('input', { class: 'input', required: true, type: 'password', name: 'key', autocomplete: 'new-password',
    id: 'cleaner-manual-key' });
  const save = el('button', { class: 'btn btn-accent', type: 'submit', disabled: true }, tr("Проверить и сохранить"));
  const status = el('p', { class: 'muted small', role: 'status' });
  const form = el('form', { class: 'cleaner-manual-form' }, field(tr("Адрес Cleaner"), address),
    field(tr("Личный ключ Cleaner"), key), el('div', { class: 'row' }, save), status);
  const panel = el('section', { class: 'block' }, el('h2', {}, tr("Ручная очистка")),
    el('p', {}, tr("Создайте ключ в Cleaner в разделе «Ключи» и выберите «Нет (только API)». Затем введите адрес Cleaner и ключ.")),
    form, el('a', { href: '#/cleaner' }, tr("Открыть «Очистку»")));

  function ready(data) {
    address.value = data.base_url;
    key.required = !data.configured;
    key.placeholder = data.configured ? tr("Пустое поле сохраняет ключ") : '';
  }
  api('GET', endpoint).then(ready).catch((err) => { status.textContent = words(err); })
    .finally(() => { save.disabled = false; });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = true;
    status.textContent = '';
    try {
      const data = await api('PUT', endpoint, { base_url: address.value, key: key.value });
      key.value = '';
      ready(data);
      status.textContent = tr("Ручная очистка подключена");
    } catch (err) {
      status.textContent = words(err);
    } finally {
      save.disabled = false;
    }
  });
  return panel;
}
