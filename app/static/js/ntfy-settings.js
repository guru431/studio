/* Прямая доставка ntfy — настройка учётки. Сохранённый токен сервер не возвращает. */
import { api } from './api.js';
import { el, field, words } from './ui.js';

export function ntfyBlock() {
  const enabled = el('input', { type: 'checkbox', id: 'ntfy-enabled' });
  const server = el('input', { type: 'url', id: 'ntfy-server', autocomplete: 'off', maxlength: 512 });
  const topic = el('input', { id: 'ntfy-topic', autocomplete: 'off', maxlength: 64, spellcheck: 'false' });
  const token = el('input', { type: 'password', id: 'ntfy-token', autocomplete: 'new-password', maxlength: 512 });
  const clearToken = el('input', { type: 'checkbox', id: 'ntfy-clear-token' });
  const clearLabel = el('label', { class: 'check', hidden: true }, clearToken, 'Удалить сохранённый токен');
  const note = el('p', { id: 'ntfy-status', role: 'status', 'aria-live': 'polite' });
  const save = el('button', { type: 'button', id: 'ntfy-save' }, 'Сохранить');
  const test = el('button', { type: 'button', id: 'ntfy-test' }, 'Проверить');
  const remove = el('button', { type: 'button', id: 'ntfy-remove' }, 'Сбросить');
  let saved = false;
  const controls = [enabled, server, topic, token, clearToken, save, test, remove];
  function busy(on) {
    for (const control of controls) control.disabled = on;
    if (!on) test.disabled = remove.disabled = !saved;
  }
  function paint(value) {
    server.value = value.server;
    topic.value = value.topic;
    enabled.checked = !!value.enabled;
    token.value = '';
    token.placeholder = value.has_token ? 'Сохранён — оставьте пустым, чтобы сохранить' : 'Необязательно, если сервер разрешает отправку без токена';
    clearLabel.hidden = !value.has_token;
    clearToken.checked = false;
    saved = !!value.topic;
  }
  async function action(work) {
    note.textContent = '';
    busy(true);
    try { await work(); }
    catch (err) { note.textContent = words(err); }
    finally { busy(false); }
  }
  save.addEventListener('click', () => action(async () => {
    const value = { server: server.value.trim(), topic: topic.value.trim(), enabled: enabled.checked };
    if (clearToken.checked) value.token = '';
    else if (token.value) value.token = token.value;
    paint(await api('PUT', '/api/me/ntfy', value));
    note.textContent = 'Настройки ntfy сохранены';
  }));
  test.addEventListener('click', () => action(async () => {
    await api('POST', '/api/me/ntfy/test');
    note.textContent = 'Уведомление отправлено в ntfy — проверьте телефон';
  }));
  remove.addEventListener('click', () => action(async () => {
    await api('DELETE', '/api/me/ntfy');
    paint(await api('GET', '/api/me/ntfy'));
    note.textContent = 'Настройки ntfy сброшены';
  }));
  const block = el('section', { class: 'block', id: 'ntfy-settings' }, el('h2', {}, 'ntfy'),
    el('p', { class: 'muted small' }, 'Уведомления агентов и готовности заданий без Google-сервисов. '
      + 'На Android установите приложение ntfy, подпишитесь на ту же тему и разрешите работу в фоне. Настройка общая для учётки.'),
    el('label', { class: 'check' }, enabled, 'Отправлять уведомления в ntfy'),
    field('Сервер ntfy', server, server.id), field('Тема', topic, topic.id),
    field('Токен доступа', token, token.id), clearLabel,
    el('p', { class: 'muted small' }, 'Токен хранится зашифрованным. Сервер ntfy получает только уведомление и ссылку, без переписки. '
      + '«Проверить» отправляет уведомление с сохранёнными настройками.'),
    el('div', { class: 'row' }, save, test, remove), note);
  busy(true);
  api('GET', '/api/me/ntfy').then(paint).catch((err) => { note.textContent = words(err); }).finally(() => busy(false));
  return block;
}
