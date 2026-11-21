/* Формы MCP: HTTP-подключения Studio и stdio/HTTP/SSE проекта на мосту. */
import { api } from './api.js';
import { tr } from './i18n.js';
import { button, computerPin, el, field, openDialog, words } from './ui.js';
import { byteSize } from './chat-files.js';

const input = (id, type = 'text', value = '') => el('input', { id, type, value, class: 'input', autocomplete: 'off' });
const area = (id, text = '') => el('textarea', { id, class: 'input', rows: '4', spellcheck: 'false' }, text);
// Сохранённое значение заголовка Studio (`GET /api/mcp/{id}` отдаёт null): не ASCII — настоящим значением не бывает.
const MASK = '••••••';
const lines = values => Object.entries(values || {}).map(([k, v]) => `${k}=${v ?? MASK}`).join('\n');
function pairs(text, masked = false) {
  const entries = text.split(/\r?\n/).filter(s => s.trim()).map(line => {
    const at = line.indexOf('=');
    if (at < 1) throw new Error(tr("Каждая строка: ИМЯ=значение, без повторов"));
    const value = line.slice(at + 1);
    return [line.slice(0, at).trim(), masked && value === MASK ? null : value];   // null — оставить прежнее
  });
  if (new Set(entries.map(([key]) => key)).size !== entries.length) throw new Error(tr("Каждая строка: ИМЯ=значение, без повторов"));
  return Object.fromEntries(entries);
}

function editor(host, previous, { local = false, save }) {
  const old = previous || {}, oldHeaders = { ...old.headers };
  const auth = local && Object.keys(oldHeaders).find(k => k.toLowerCase() === 'authorization');
  if (auth) delete oldHeaders[auth];
  const name = input('mcp-name', 'text', old.name || ''); name.required = true; name.maxLength = local ? 64 : 80;
  const type = el('select', { id: 'mcp-type', class: 'input' }, ...['http', ...(local ? ['stdio', 'sse'] : [])]
    .map(t => el('option', { value: t }, t === 'http' ? 'HTTP' : t)));
  type.value = old.type || (old.command ? 'stdio' : 'http');
  const url = input('mcp-url', 'url', old.url || '');
  const token = input('mcp-token', 'password'); token.placeholder = tr("Оставьте пустым, чтобы сохранить токен");
  const clear = input('mcp-clear-token', 'checkbox'), headers = area('mcp-headers', lines(oldHeaders));
  const command = input('mcp-command', 'text', old.command || '');
  const args = area('mcp-args', (old.args || []).join('\n')), env = area('mcp-env', lines(old.env));
  const enabled = input('mcp-enabled', 'checkbox'); enabled.checked = !old.disabled && old.enabled !== false;
  const remote = el('div', {}, field(tr("Адрес MCP"), url), field(tr("Токен Bearer"), token),
    el('label', { class: 'check' }, clear, tr("Удалить сохранённый токен")),
    field(tr("HTTP-заголовки (ИМЯ=значение, по одному на строку)"), headers),
    ...(!local && Object.keys(oldHeaders).length
      ? [el('p', { class: 'muted' }, tr("Сохранённые значения скрыты: {0} оставляет прежнее значение заголовка", MASK))] : []));
  const stdio = el('div', {}, field(tr("Команда"), command), field(tr("Аргументы (по одному на строку)"), args),
    field(tr("Переменные окружения (ИМЯ=значение, по одной на строку)"), env));
  const repaint = () => { remote.hidden = type.value === 'stdio'; stdio.hidden = !remote.hidden;
    url.required = !remote.hidden; command.required = remote.hidden; };
  type.addEventListener('change', repaint); repaint();
  const note = el('p', { class: 'error', id: 'mcp-editor-status', 'aria-live': 'polite' });
  const submit = button(tr("Сохранить"), { type: 'submit', class: 'btn btn-accent', id: 'mcp-save' });
  const form = el('form', { id: 'mcp-form' }, field(tr("Имя сервера"), name), field(tr("Тип подключения"), type), remote,
    ...(local ? [stdio] : []), el('label', { class: 'check' }, enabled, tr("Включено")), note, submit);
  const dialog = openDialog(host, { id: 'mcp-editor', title: tr("Подключение MCP"), body: [form] });
  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true; note.textContent = '';
    try {
      const value = type.value === 'stdio'
        ? { type: 'stdio', command: command.value.trim(), args: args.value ? args.value.split(/\r?\n/) : [], env: pairs(env.value) }
        : { type: type.value, url: url.value.trim(), headers: pairs(headers.value, !local) };
      if (type.value !== 'stdio') {
        if (local) {
          if (token.value) value.headers.Authorization = `Bearer ${token.value}`;
          else if (auth && !clear.checked) value.headers[auth] = old.headers[auth];
        } else if (token.value || clear.checked) value.token = clear.checked ? '' : token.value;
      }
      await save(name.value.trim(), value, enabled.checked); dialog.close();
    } catch (err) { note.textContent = words(err); }
    finally { submit.disabled = false; }
  });
}

/* Доступ к общему (панель администратора, access.py): null — все пользователи, [] — только администраторам,
 * [gid…] — участникам этих групп. Администраторам общее открыто всегда. */
function accessSummary(groups, chosen) {
  if (chosen === null) return tr("Все пользователи");
  const names = groups.filter(g => chosen.includes(g.id)).map(g => g.name);
  return names.length ? tr("Группам: {0}", names.join(', ')) : tr("Только администраторам");
}

function groupChecks(groups, chosen, onChange) {
  const box = el('div', { class: 'access-groups' }, ...(groups.length ? groups.map(g => {
    const control = el('input', { type: 'checkbox', class: 'access-group', value: String(g.id), checked: chosen.includes(g.id) });
    control.addEventListener('change', onChange);
    return el('label', { class: 'check' }, control, g.name);
  }) : [el('p', { class: 'muted small' }, tr("Групп пока нет — создайте их в блоке «Группы»"))]));
  return { el: box, value: () => [...box.querySelectorAll('input.access-group:checked')].map(c => Number(c.value)) };
}

/** «Все пользователи» / «Выбранные группы» для общего подключения или сервера MCP; value() — null или [gid]. */
export function accessPicker(groups, chosen) {
  const mode = value => el('input', { type: 'radio', name: 'access-mode', value, checked: (chosen === null) === (value === 'all') });
  const all = mode('all'), some = mode('groups');
  const summary = el('p', { class: 'muted small access-summary', 'aria-live': 'polite' });
  const checks = groupChecks(groups, chosen || [], paint);
  const value = () => all.checked ? null : checks.value();
  function paint() { checks.el.hidden = all.checked; summary.textContent = accessSummary(groups, value()); }
  all.addEventListener('change', paint); some.addEventListener('change', paint); paint();
  return { value, el: el('fieldset', { class: 'access-choice' }, el('legend', {}, tr("Кому доступно")),
    el('label', { class: 'check' }, all, tr("Все пользователи")), el('label', { class: 'check' }, some, tr("Выбранные группы")),
    checks.el, summary) };
}

/** Политика инструмента (admin.py, `mcp_tool_policy`): «интернет» и «подтверждение» сильнее подсказок сервера;
 * пустое значение — как у сервера. value() — { open_world: null | bool, confirm: null | 'always' | 'never' }. */
function policySelects(name, rule = {}) {
  const option = (value, text) => el('option', { value }, text);
  const web = el('select', { class: 'input mcp-policy-web', 'aria-label': tr("Интернет для инструмента {0}", name) },
    option('', tr("Интернет: как у сервера")), option('yes', tr("Интернет: да")), option('no', tr("Интернет: нет")));
  const confirm = el('select', { class: 'input mcp-policy-confirm', 'aria-label': tr("Подтверждение для инструмента {0}", name) },
    option('', tr("Подтверждение: как у сервера")), option('always', tr("Подтверждение: всегда")),
    option('never', tr("Подтверждение: никогда")));
  web.value = rule.open_world === true ? 'yes' : rule.open_world === false ? 'no' : '';
  confirm.value = rule.confirm || '';
  return { el: el('div', { class: 'mcp-policy' }, web, confirm),
    value: () => ({ open_world: web.value ? web.value === 'yes' : null, confirm: confirm.value || null }) };
}

/** Строка инструмента: «Как у подключения» (правила нет) / «Выбранным группам» / «Только администраторам»;
 * под ней — политика «интернет» и «подтверждение». */
function toolRow(groups, name, chosen, missing, rule) {
  const select = el('select', { class: 'input access-tool-mode', 'aria-label': tr("Доступ к инструменту {0}", name) },
    el('option', { value: 'inherit' }, tr("Как у подключения")), el('option', { value: 'groups' }, tr("Выбранным группам")),
    el('option', { value: 'admins' }, tr("Только администраторам")));
  select.value = chosen === undefined ? 'inherit' : chosen.length ? 'groups' : 'admins';
  const summary = el('span', { class: 'muted small access-summary' });
  const checks = groupChecks(groups, chosen || [], paint);
  const value = () => select.value === 'inherit' ? undefined : select.value === 'admins' ? [] : checks.value();
  function paint() {
    checks.el.hidden = select.value !== 'groups';
    summary.textContent = select.value === 'inherit' ? '' : accessSummary(groups, value());
  }
  select.addEventListener('change', paint); paint();
  const policy = policySelects(name, rule);
  return { name, value, policy: policy.value, el: el('li', { class: 'access-tool', 'data-tool': name },
    el('div', { class: 'access-tool-head' }, el('code', {}, name),
      missing ? el('span', { class: 'tag access-missing' }, tr("нет у сервера")) : null),
    select, checks.el, summary, policy.el) };
}

/* «Доступ» общего MCP: сервер, правила инструментов и их политика «интернет» / «подтверждение» (PUT …/policy после
 * …/access). Список — живой ответ «Проверить» плюс имена из правил и политики, которых у сервера сейчас нет;
 * «Проверить» не ответил — правила всё равно показаны и сохраняются. */
function mcpAccessDialog(host, item, note) {
  const base = `/api/admin/shared/mcp/${item.id}`;
  const server = el('div', {}, el('p', { class: 'muted' }, tr("Загружаю…")));
  const status = el('p', { class: 'muted small', id: 'mcp-access-tools-status', 'aria-live': 'polite' });
  const list = el('ul', { class: 'access-tools', id: 'mcp-access-tools' });
  const fail = el('p', { class: 'error', id: 'mcp-access-status', 'aria-live': 'polite' });
  const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'mcp-access-save', disabled: true });
  const dialog = openDialog(host, { id: 'mcp-access-dialog', title: tr("Доступ: {0}", item.name), actions: [save], body: [server,
    el('h3', {}, tr("Инструменты")),
    el('p', { class: 'muted small' }, tr("Инструмент без своего правила доступен всем, кому открыто подключение.")),
    el('p', { class: 'muted small' }, tr("«Интернет» и «Подтверждение» заменяют пометки самого сервера: инструмент с интернетом работает только при включённом «Интернете» диалога, с подтверждением — по умолчанию выключен в диалогах.")),
    status, list, fail] });
  let picker = null, rows = [];
  save.addEventListener('click', async () => {
    save.disabled = true; fail.textContent = '';
    const tools = Object.fromEntries(rows.filter(r => r.value() !== undefined).map(r => [r.name, r.value()]));
    const policy = Object.fromEntries(rows.map(r => [r.name, r.policy()])
      .filter(([, p]) => p.open_world !== null || p.confirm !== null));
    try {
      await api('PUT', `${base}/access`, { groups: picker.value(), tools });
      await api('PUT', `${base}/policy`, { tools: policy });
      dialog.close(); note.textContent = tr("Доступ сохранён");
    } catch (err) { fail.textContent = words(err); save.disabled = false; }
  });
  const live = api('POST', `${base}/check`).then(({ tools }) => tools.filter(Boolean), err => {
    status.textContent = tr("Список инструментов сервера не получен: {0}. Правила показаны, их можно сохранить.", words(err));
    return null;
  });
  Promise.all([api('GET', '/api/admin/groups'), api('GET', `${base}/access`), api('GET', `${base}/policy`), live])
    .then(([{ items: groups }, rules, policy, tools]) => {
    if (!dialog.open) return;
    picker = accessPicker(groups, rules.groups);
    server.replaceChildren(picker.el);
    const names = [...new Set([...(tools || []), ...Object.keys(rules.tools), ...Object.keys(policy.tools)])];
    rows = names.map(name => toolRow(groups, name, rules.tools[name], tools !== null && !tools.includes(name), policy.tools[name]));
    list.replaceChildren(...(rows.length ? rows.map(r => r.el) : [el('li', { class: 'muted' }, tr("Инструментов нет"))]));
    save.disabled = false;
  }, err => { server.replaceChildren(); fail.textContent = words(err); });
}

/* Файлы пользователя сервера (`studio/user-files`, mcp_contracts.py): «Загрузить» и «Убрать» по каждому файлу,
 * который объявил сервер. Файл уходит в личную папку на сервере MCP; Studio его не хранит и модели не передаёт.
 * Сервер без контракта или не ответивший — блока нет. */
function userFilesBlock(item) {
  const base = `/api/mcp/${item.id}/user-files`;
  const list = el('div', { class: 'mcp-user-file-list' });
  const note = el('p', { class: 'status', 'aria-live': 'polite' });
  const box = el('div', { class: 'mcp-user-files', 'data-mcp-user-files': String(item.id), hidden: true },
    el('h3', {}, tr("Файлы для инструментов сервера")),
    el('p', { class: 'muted small' }, tr("Файл уходит в вашу папку на сервере MCP. Studio его не хранит и модели не передаёт.")),
    list, note);
  function row(file) {
    const picker = el('input', { type: 'file', class: 'sr', 'data-key': file.key, ...(file.accept ? { accept: file.accept } : {}) });
    const upload = el('label', { class: 'btn' }, picker, tr("Загрузить"));
    const remove = button(tr("Убрать"), { class: 'btn', hidden: !file.present }, () => run(async () => {
      await api('DELETE', `${base}/${encodeURIComponent(file.key)}`);
      note.textContent = tr("Файл «{0}» убран", file.title);
    }));
    picker.addEventListener('change', () => {
      const chosen = picker.files[0];
      picker.value = '';
      if (!chosen) return;
      if (!chosen.size || chosen.size > file.max_bytes) {
        note.textContent = tr("Файл должен быть непустым и не больше {0}", byteSize(file.max_bytes));
        return;
      }
      run(async () => {
        await api('PUT', `${base}/${encodeURIComponent(file.key)}`, chosen,
          { headers: { 'Content-Type': 'application/octet-stream' } });
        note.textContent = tr("Файл «{0}» загружен", file.title);
      });
    });
    return el('div', { class: 'row mcp-user-file', 'data-key': file.key }, el('b', {}, file.title),
      el('span', { class: 'muted' }, file.present ? tr("загружен, {0}", byteSize(file.size)) : tr("не загружен")),
      upload, remove);
  }
  async function run(action) {
    note.textContent = '';
    try { await action(); await load(); } catch (err) { note.textContent = words(err); }
  }
  async function load() {
    const { items } = await api('GET', base);
    list.replaceChildren(...items.map(row));
    box.hidden = !items.length;
  }
  load().catch(() => {});
  return box;
}

export function chatMcpBlock({ admin = false } = {}) {
  const base = admin ? '/api/admin/shared/mcp' : '/api/mcp';
  const list = el('div', { id: 'mcp-servers' }), note = el('p', { class: 'status', 'aria-live': 'polite' });
  const root = el('section', { class: 'block', id: admin ? 'admin-shared-mcp' : 'chat-mcp-settings' },
    el('h2', {}, admin ? tr("Общие подключения MCP") : 'MCP'), el('p', { class: 'muted' }, admin
      ? tr("Пользователи вызывают инструменты с правами этого подключения; кому доступны сервер и его инструменты — кнопка «Доступ». Адрес должен быть в списке разрешённых.")
      : tr("Подключения для чата и других разделов Studio. Поддерживаемые инструменты появятся в своих разделах. Адрес должен разрешить администратор Studio.")), list, note);
  async function run(action) { try { await action(); } catch (err) { note.textContent = words(err); } }
  async function reload() {
    const { items } = await api('GET', base);
    if (!root.isConnected) return;
    list.replaceChildren(...items.map(item => el('div', { class: 'block', 'data-mcp-id': item.id },
      el('b', {}, item.name), item.url ? el('p', { class: 'muted' }, item.url) : el('p', { class: 'muted' }, tr("Общее подключение")),
      el('p', {}, item.enabled ? tr("Включено") : tr("Выключено")), item.shared && !admin ? null : el('div', { class: 'row' },
        button(tr("Изменить"), { class: 'btn' }, () => run(async () => edit(await api('GET', `${base}/${item.id}`)))),
        button(tr("Проверить"), { class: 'btn' }, event => run(async () => {
          const control = event.currentTarget; control.disabled = true;
          try { const { tools } = await api('POST', `${base}/${item.id}/check`); note.textContent = tr("Подключено, инструментов: {0}", tools.length); }
          finally { control.disabled = false; }
        })), admin ? button(tr("Доступ"), { class: 'btn mcp-access' }, () => mcpAccessDialog(root, item, note)) : null,
        button(tr("Удалить"), { class: 'btn' }, () => {
          const remove = button(tr("Удалить"), { class: 'btn btn-danger' });
          const dialog = openDialog(root, { id: 'mcp-remove', title: tr("Удалить подключение MCP?"), body: [el('p', {}, item.name)], actions: [remove] });
          remove.addEventListener('click', () => run(async () => {
            remove.disabled = true;
            try { await api('DELETE', `${base}/${item.id}`); dialog.close(); await reload(); }
            finally { remove.disabled = false; }
          }));
        })), !admin && item.enabled ? userFilesBlock(item) : null)));
    if (!items.length) list.append(el('p', { class: 'muted' }, tr("Подключений MCP пока нет")));
  }
  function edit(item) {
    editor(root, item, { save: async (name, value, enabled) => {
      const { type, ...fields } = value;
      await api(item ? 'PUT' : 'POST', item ? `${base}/${item.id}` : base, { ...fields, name, enabled }); await reload();
    } });
  }
  root.append(button(tr("Добавить MCP"), { class: 'btn', id: 'mcp-add' }, () => edit(null)));
  queueMicrotask(() => run(reload));
  return root;
}

export function agentMcpBlock(agents) {
  const computers = el('select', { class: 'input', id: 'mcp-computer' }), projects = el('select', { class: 'input', id: 'mcp-project' });
  const engine = el('select', { class: 'input', id: 'mcp-engine' },
    ...[['claude', 'Claude Code'], ['codex', 'Codex'], ['opencode', 'OpenCode']].map(([value, name]) => el('option', { value }, name)));
  const pin = computerPin({ id: 'mcp-pin' });
  const note = el('p', { class: 'status', 'aria-live': 'polite', id: 'agent-mcp-status' }), list = el('div', { id: 'agent-mcp-servers' });
  const add = button(tr("Добавить MCP"), { class: 'btn', id: 'agent-mcp-add', disabled: true }, () => edit());
  const root = el('section', { class: 'block', id: 'agent-mcp-settings' }, el('h2', {}, tr("MCP агентов")),
    el('p', { class: 'muted' }, tr("MCP запускается на выбранном компьютере для этого проекта и движка. Изменения действуют со следующего хода.")),
    field(tr("Компьютер"), computers), field(tr("Проект"), projects), field(tr("Движок"), engine), list, add, note);
  const unlock = el('div', { hidden: true }, field('PIN', pin), button(tr("Открыть компьютер"), { class: 'btn' }, async () => {
    try { await api('POST', `/api/code/agents/${computers.value}/open`, { pin: pin.value || null }); pin.value = ''; await loadProjects(); }
    catch (err) { note.textContent = words(err); }
  })); root.append(unlock);
  let config = { mcpServers: {} }, generation = 0;
  const base = () => `/api/code/agents/${computers.value}/projects/${projects.value}/mcp?engine=${engine.value}`;
  async function loadConfig() {
    const mine = ++generation; add.disabled = true; list.replaceChildren(); note.textContent = '';
    if (!computers.value || !projects.value) return;
    try {
      const value = await api('GET', base());
      if (mine !== generation || !root.isConnected) return;
      config = value; add.disabled = false;
      list.replaceChildren(...Object.entries(config.mcpServers).map(([name, value]) => el('div', { class: 'block' },
        el('b', {}, name), el('p', { class: 'muted' }, value.url || value.command),
        el('p', {}, value.disabled ? tr("Выключено") : tr("Включено")),
        button(tr("Изменить"), { class: 'btn' }, () => edit(name, value)), button(tr("Удалить"), { class: 'btn' }, async () => {
          const next = { mcpServers: { ...config.mcpServers } }; delete next.mcpServers[name];
          try { await api('PUT', base(), { config: next }); await loadConfig(); } catch (err) { note.textContent = words(err); }
        }))));
    } catch (err) { if (mine === generation) note.textContent = words(err); }
  }
  function edit(oldName, value) {
    const url = base(), mine = generation;
    editor(root, { ...value, name: oldName }, { local: true, save: async (name, fields, enabled) => {
      if (mine !== generation) throw new Error(tr("Выбран другой проект — откройте настройки заново"));
      if (name !== oldName && Object.hasOwn(config.mcpServers, name)) throw new Error(tr("Сервер с таким именем уже есть"));
      const next = { mcpServers: { ...config.mcpServers } }; if (oldName) delete next.mcpServers[oldName];
      Object.defineProperty(next.mcpServers, name, { value: { ...fields, disabled: !enabled }, enumerable: true });
      await api('PUT', url, { config: next }); await loadConfig();
    } });
  }
  async function loadProjects() {
    const mine = ++generation; add.disabled = true; projects.replaceChildren(); list.replaceChildren(); unlock.hidden = true;
    if (!computers.value) return;
    try {
      const { items } = await api('GET', `/api/code/agents/${computers.value}/projects`);
      if (mine !== generation || !root.isConnected) return;
      projects.replaceChildren(...items.map(p => el('option', { value: p.pid }, p.name))); await loadConfig();
    } catch (err) { if (mine === generation) { note.textContent = words(err); unlock.hidden = err.code !== 'closed'; } }
  }
  computers.addEventListener('change', loadProjects); projects.addEventListener('change', loadConfig); engine.addEventListener('change', loadConfig);
  agents().then(({ items }) => {
    if (!root.isConnected) return;
    const online = items.filter(a => !a.shared && a.online);
    computers.replaceChildren(...online.filter(a => a.mcp).map(a => el('option', { value: a.id }, a.name)));
    if (!computers.options.length) note.textContent = online.length
      ? tr("Обновите мост на компьютере для настройки MCP") : tr("Компьютер не на связи — запустите мост");
    loadProjects();
  }).catch(err => { note.textContent = words(err); });
  return root;
}
