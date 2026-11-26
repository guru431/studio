import { api } from './api.js';
import { tr } from './i18n.js';
import { dateTime } from './time.js';
import { button, el, field, words } from './ui.js';

async function encode(file) {
  if (!file) return '';
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  }
  return btoa(binary);
}

export function httpsSettings() {
  const path = '/api/admin/https';
  const domain = el('input', { class: 'input', type: 'text', required: true, maxlength: 253, autocapitalize: 'off',
    spellcheck: 'false', placeholder: 'studio.example.com' });
  const certificate = el('input', { class: 'input', type: 'file', required: true, accept: '.crt,.cer,.der,.pem,.p7b' });
  const key = el('input', { class: 'input', type: 'file', required: true, accept: '.key,.pem,.der' });
  const chain = el('input', { class: 'input', type: 'file', accept: '.crt,.cer,.der,.pem,.p7b' });
  const password = el('input', { class: 'input', type: 'password', required: true, autocomplete: 'current-password' });
  const save = button(tr("Применить сертификат"), { class: 'btn btn-accent', type: 'submit' });
  const note = el('p', { class: 'status', 'aria-live': 'polite' });
  const active = el('p', { class: 'muted' });
  const controls = el('fieldset', { disabled: true }, field(tr("Доменное имя"), domain),
    field(tr("Сертификат сайта (.crt, .der, .pem, .p7b)"), certificate),
    field(tr("Закрытый ключ (.key)"), key), field(tr("Цепочка сертификатов (необязательно)"), chain),
    field(tr("Пароль учётной записи"), password), el('div', { class: 'row' }, save));
  const form = el('form', {}, controls, note);

  function show(value) {
    domain.value = value.domain || '';
    if (value.origin) {
      active.replaceChildren(el('a', { href: value.origin, target: '_blank', rel: 'noopener' }, value.origin), ' · ',
        tr("Действует до {0}", dateTime(Date.parse(value.expires) / 1000)));
    } else {
      active.textContent = tr("Домен ещё не настроен. Порт HTTPS — {0}.", value.port);
    }
  }

  api('GET', path).then(value => { show(value); controls.disabled = false; }).catch(err => { note.textContent = words(err); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const files = [certificate, key, chain].map(input => input.files[0]);
    if (files.reduce((total, file) => total + (file?.size || 0), 0) > 180 * 1024) {
      note.textContent = tr("Размер файлов не должен превышать 180 КиБ.");
      return;
    }
    controls.disabled = true;
    note.textContent = tr("Проверяем HTTPS…");
    try {
      const [certData, keyData, chainData] = await Promise.all(files.map(encode));
      const value = await api('PUT', path, { domain: domain.value.trim(), certificate: certData, key: keyData,
        chain: chainData, password: password.value }, { unlock: false });
      form.reset();
      show(value);
      note.textContent = tr("Сертификат применён. Новые письма используют этот адрес.");
    } catch (err) {
      note.textContent = words(err);
      const value = await api('GET', path).catch(() => null);
      if (value) {
        const pending = domain.value;
        show(value);
        domain.value = pending;
      }
    } finally {
      password.value = '';
      controls.disabled = false;
    }
  });
  return el('section', { class: 'block', id: 'https-settings' }, el('h2', {}, tr("Домен и HTTPS")),
    el('p', { class: 'muted' }, tr("Направьте домен на этот сервер в DNS. После применения сертификата ссылки в письмах будут вести на этот домен.")),
    active, form);
}
