import { language, tr } from './i18n.js';
/* Прямая доставка ntfy — отдельная настройка сессии устройства, по умолчанию выключена.
 * Настройка уходит вместе с сессией (выход, конец срока): сервер и тему последнего сохранения браузер помнит в
 * localStorage `studio-ntfy:<id учётки>` и подставляет в форму, пока у новой сессии настройки нет. Токен не
 * запоминается — его вводят заново. «Сбросить» забывает и запомненное. */
import { api, me } from './api.js';
import { el, field, words } from './ui.js';

const NTFY_KEY = 'studio-ntfy';
const RESTORED = tr("Сервер и тема — с прошлой настройки на этом устройстве. Токен, если он нужен, введите заново и сохраните.");

/** Запомненные `{server, topic}` этого браузера для учётки: прочесть, записать или забыть (`null`). */
function remembered(value) {
  const key = `${NTFY_KEY}:${me.id}`;
  try {
    if (value === undefined) {
      const saved = JSON.parse(localStorage.getItem(key));
      return saved && typeof saved.server === 'string' && typeof saved.topic === 'string' ? saved : null;
    }
    if (value) localStorage.setItem(key, JSON.stringify({ server: value.server, topic: value.topic }));
    else localStorage.removeItem(key);
  } catch { /* Закрытое или повреждённое хранилище — без подсказки. */ }
  return null;
}

export function ntfyBlock() {
  const enabled = el('input', { type: 'checkbox', id: 'ntfy-enabled' });
  const server = el('input', { type: 'url', id: 'ntfy-server', autocomplete: 'off', maxlength: 512 });
  const topic = el('input', { id: 'ntfy-topic', autocomplete: 'off', maxlength: 64, spellcheck: 'false' });
  const token = el('input', { type: 'password', id: 'ntfy-token', autocomplete: 'new-password', maxlength: 512 });
  const clearToken = el('input', { type: 'checkbox', id: 'ntfy-clear-token' });
  const clearLabel = el('label', { class: 'check', hidden: true }, clearToken, tr("Удалить сохранённый токен"));
  const note = el('p', { id: 'ntfy-status', role: 'status', 'aria-live': 'polite' });
  const save = el('button', { type: 'button', id: 'ntfy-save' }, tr("Сохранить"));
  const test = el('button', { type: 'button', id: 'ntfy-test' }, tr("Проверить"));
  const remove = el('button', { type: 'button', id: 'ntfy-remove' }, tr("Сбросить"));
  const subscribe = el('a', { id: 'ntfy-subscribe', hidden: true }, tr("Подписаться в приложении ntfy"));
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
    token.placeholder = value.has_token ? tr("Сохранён — оставьте пустым, чтобы сохранить") : tr("Необязательно, если сервер разрешает отправку без токена");
    clearLabel.hidden = !value.has_token;
    clearToken.checked = false;
    saved = !!value.configured;
    subscribe.hidden = !value.configured || !value.topic;
    if (value.topic) {
      const address = new URL(value.server);
      subscribe.href = `ntfy://${address.host}/${encodeURIComponent(value.topic)}`;
    } else subscribe.removeAttribute('href');
  }
  async function action(work) {
    note.textContent = '';
    busy(true);
    try { await work(); }
    catch (err) { note.textContent = words(err); }
    finally { busy(false); }
  }
  save.addEventListener('click', () => action(async () => {
    const value = { server: server.value.trim(), topic: topic.value.trim(), enabled: enabled.checked, lang: language() };
    if (clearToken.checked) value.token = '';
    else if (token.value) value.token = token.value;
    const answer = await api('PUT', '/api/me/ntfy', value);
    paint(answer);
    remembered(answer);
    note.textContent = tr("Настройки ntfy сохранены на этом устройстве");
  }));
  test.addEventListener('click', () => action(async () => {
    await api('POST', '/api/me/ntfy/test');
    note.textContent = tr("Уведомление отправлено в ntfy — проверьте телефон");
  }));
  remove.addEventListener('click', () => action(async () => {
    await api('DELETE', '/api/me/ntfy');
    remembered(null);
    paint(await api('GET', '/api/me/ntfy'));
    note.textContent = tr("Настройки ntfy сброшены");
  }));
  const block = el('section', { class: 'block', id: 'ntfy-settings' }, el('h2', {}, 'ntfy'),
    el('p', { class: 'muted small' }, tr("Уведомления агентов и готовности заданий без Google-сервисов. Включается отдельно на каждом устройстве, по умолчанию выключено. После выхода из учётки включите заново.")),
    el('label', { class: 'check' }, enabled, tr("Уведомления через ntfy на этом устройстве")),
    field(tr("Сервер ntfy"), server, server.id), field(tr("Тема"), topic, topic.id),
    field(tr("Токен доступа"), token, token.id), clearLabel,
    el('p', { class: 'muted small' }, tr("Для каждого устройства используйте отдельную тему: ntfy доставляет сообщение всем её подписчикам. Сохраните настройки, подпишитесь на тему в приложении ntfy на этом устройстве и разрешите ему работу в фоне.")), subscribe,
    el('p', { class: 'muted small' }, tr("Токен хранится зашифрованным. Сервер ntfy получает только уведомление и ссылку, без переписки. «Проверить» отправляет уведомление с сохранёнными настройками.")),
    el('div', { class: 'row' }, save, test, remove), note);
  busy(true);
  api('GET', '/api/me/ntfy').then((value) => {
    const last = value.configured ? null : remembered();
    // прежний общий токен сервер сохранит только для своего сервера (ntfy.save)
    paint(last ? { ...value, ...last, has_token: value.has_token && last.server === value.server } : value);
    if (last) note.textContent = RESTORED;
  }).catch((err) => { note.textContent = words(err); }).finally(() => busy(false));
  return block;
}
