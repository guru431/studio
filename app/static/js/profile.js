/* Раздел «Профиль» (#/profile): подключения, чат по умолчанию, пароль, удаление истории
 * (тема — кнопкой в рельсе, app.js).
 *
 * Модели чата по умолчанию: подключение через сервер — GET /api/connections/<id>/models; прямое —
 * браузер сам зовёт адрес моделей из /api/connections/<id>/token (токен и адрес — из одного ответа,
 * не из списка, который мог устареть). Токен живёт только в памяти этого запроса. Сервис без CORS (fetch бросает TypeError) — словами «подключите через сервер». */
import { api, loadMe, me } from './api.js';
import { connectionsBlock } from './connection-form.js';
import { button, el, field, openDialog, words } from './ui.js';

export const CORS = 'Сервис не разрешает запросы из браузера (CORS) — подключите его через сервер';

function block(title, ...children) {
  return el('section', { class: 'block' }, el('h2', {}, title), ...children);
}

function status(id) {
  return el('p', { class: 'status', id, 'aria-live': 'polite' });
}

function say(node, text, kind) {
  node.textContent = text;
  node.className = `status${kind ? ` ${kind}` : ''}`;
}

/* ---- чат по умолчанию ------------------------------------------------------- */

async function directModels(conn) {
  let token;
  let url;
  try {
    ({ token, models_url: url } = await api('GET', `/api/connections/${conn.id}/token`));
  } catch (err) {
    if (err.status === 403 || err.status === 404) {
      throw new Error('Токен этого подключения браузеру не выдаётся — впишите модель вручную');
    }
    throw err;
  }
  let response;
  try {
    response = await fetch(url, { headers: { Authorization: `Bearer ${token}` },
      credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
  } catch (err) {
    if (err instanceof TypeError) throw new Error(CORS);
    throw err;
  }
  if (response.status === 401 || response.status === 403) throw new Error(`Сервис не принял токен (${response.status})`);
  if (!response.ok) throw new Error(`Сервис не отдал список моделей (${response.status})`);
  let data;
  try {
    data = (await response.json()).data;
  } catch {
    throw new Error('Сервис ответил не списком моделей');
  }
  return (Array.isArray(data) ? data : []).map((m) => m && m.id).filter((id) => typeof id === 'string');
}

/** Имена моделей подключения (его же берёт чип модели чата); отказ — Error со словами. */
export async function modelsOf(conn) {
  if (conn.proxied) return (await api('GET', `/api/connections/${conn.id}/models`)).items.map((m) => m.id);
  return directModels(conn);
}

function defaultsBlock() {
  const select = el('select', { class: 'input', id: 'default-conn' });
  const model = el('input', { class: 'input', id: 'default-model', list: 'default-models', maxlength: '200',
    autocomplete: 'off', spellcheck: 'false', placeholder: 'имя модели' });
  const options = el('datalist', { id: 'default-models' });
  const note = el('p', { class: 'muted small', id: 'default-note', 'aria-live': 'polite' });
  const saved = status('default-status');
  const save = button('Сохранить', { class: 'btn btn-accent', id: 'default-save' });
  let conns = [];
  let asked = 0;          // медленный список прежнего подключения не перебьёт новый

  async function showModels() {
    const conn = conns.find((c) => String(c.id) === select.value);
    const mine = ++asked;
    options.replaceChildren();
    if (!conn) {
      note.textContent = '';
      return;
    }
    note.textContent = 'Загружаю список моделей…';
    note.className = 'muted small';
    try {
      const ids = await modelsOf(conn);
      if (mine !== asked) return;
      options.replaceChildren(...ids.map((id) => el('option', { value: id })));
      note.textContent = ids.length ? `Моделей: ${ids.length}` : 'Сервис не назвал ни одной модели';
    } catch (err) {
      if (mine !== asked) return;
      note.textContent = words(err);
      note.className = 'error small';
    }
  }

  select.addEventListener('change', () => {
    say(saved, '');
    showModels();
  });
  model.addEventListener('input', () => say(saved, ''));
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await api('PATCH', '/api/me', { default_connection_id: select.value ? Number(select.value) : null,
        default_model: model.value.trim() || null });
      await loadMe();
      say(saved, 'Сохранено', 'ok');
    } catch (err) {
      say(saved, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });

  return {
    el: block('Чат по умолчанию',
      el('div', { class: 'grid2' }, field('Подключение', select, 'conn'), field('Модель', model, 'model')),
      options, note, el('div', { class: 'row' }, save, saved)),
    fill(items) {
      conns = items.filter((c) => c.sections.includes('chat'));
      const chosen = select.value || (me.default_connection_id === null ? '' : String(me.default_connection_id));
      select.replaceChildren(el('option', { value: '' }, conns.length ? 'первое с чатом' : 'нет подключений с чатом'),
        ...conns.map((c) => el('option', { value: String(c.id) }, c.name)));
      select.value = conns.some((c) => String(c.id) === chosen) ? chosen : '';
      if (!model.value) model.value = me.default_model || '';
      showModels();
    },
  };
}

/* ---- пароль, история -------------------------------------------------------- */

function passwordBlock() {
  const input = (id, autocomplete) => el('input', { class: 'input', id, type: 'password', autocomplete, maxlength: '1024' });
  const current = input('pw-current', 'current-password');
  const fresh = input('pw-new', 'new-password');
  const repeat = input('pw-repeat', 'new-password');
  const note = status('password-status');
  const save = button('Сменить пароль', { class: 'btn', id: 'password-save' });
  save.addEventListener('click', async () => {
    if (fresh.value !== repeat.value) {
      say(note, 'Пароли не совпадают', 'error');
      return;
    }
    save.disabled = true;
    try {
      await api('POST', '/api/me/password', { current: current.value, new: fresh.value });
      current.value = fresh.value = repeat.value = '';
      say(note, 'Пароль сменён, другие сессии закрыты', 'ok');
    } catch (err) {
      say(note, words(err), 'error');
    } finally {
      save.disabled = false;
    }
  });
  return block('Пароль', el('div', { class: 'grid3' }, field('Текущий', current, 'current'),
    field('Новый', fresh, 'new'), field('Повтор нового', repeat, 'repeat')), el('div', { class: 'row' }, save, note));
}

function wipeBlock(host) {
  const note = status('wipe-status');
  const start = button('Удалить всю историю', { class: 'btn btn-danger', id: 'history-wipe' }, () => {
    say(note, '');
    const text = el('p', {}, 'Все чаты, фото и видео удалятся безвозвратно. Подключения и настройки останутся.');
    const fail = el('p', { class: 'error' });
    const next = button('Удалить', { class: 'btn btn-danger', id: 'wipe-next' });
    const confirm = button('Да, удалить навсегда', { class: 'btn btn-danger solid', id: 'wipe-confirm', hidden: true });
    const dialog = openDialog(host, { id: 'wipe-dialog', title: 'Удалить всю историю?', body: [text, fail],
      actions: [next, confirm] });
    next.addEventListener('click', () => {
      text.textContent = 'Точно? Отменить удаление нельзя.';
      next.hidden = true;
      confirm.hidden = false;
      confirm.focus();
    });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await api('DELETE', '/api/history');
        dialog.close();
        say(note, 'История удалена', 'ok');
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
      }
    });
  });
  return block('История', el('p', { class: 'muted small' }, 'Чаты, фото и видео этого профиля. Подключения не удаляются.'),
    el('div', { class: 'row' }, start, note));
}

/* ---- раздел ----------------------------------------------------------------- */

export function mount(view) {
  const root = el('div', { class: 'page' });
  const defaults = defaultsBlock();
  const conns = connectionsBlock(root, '/api/connections', async (items) => {
    await loadMe().catch(() => null);           // возможности разделов сменились — роутер читает me
    defaults.fill(items);
  });
  root.append(block('Подключения', conns.el), defaults.el, passwordBlock(), wipeBlock(root));
  view.append(el('header', { class: 'head' }, el('h1', {}, 'Профиль')), root);
  conns.reload().then((items) => defaults.fill(items));
}

export function unmount() {}
