/* Формы MCP: HTTP-подключения Studio и stdio/HTTP/SSE проекта на мосту. */
import { api } from './api.js';
import { tr } from './i18n.js';
import { button, computerPin, el, field, openDialog, words } from './ui.js';

const HINT = tr("Модель сможет вызывать инструменты выбранных серверов и передавать им данные диалога.");
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

export function chatMcpBlock() {
  const list = el('div', { id: 'mcp-servers' }), note = el('p', { class: 'status', 'aria-live': 'polite' });
  const root = el('section', { class: 'block', id: 'chat-mcp-settings' }, el('h2', {}, 'MCP'),
    el('p', { class: 'muted' }, tr("Подключения для чата и других разделов Studio. Поддерживаемые инструменты появятся в своих разделах. Адрес должен разрешить администратор Studio.")), list, note);
  async function run(action) { try { await action(); } catch (err) { note.textContent = words(err); } }
  async function reload() {
    const { items } = await api('GET', '/api/mcp');
    if (!root.isConnected) return;
    list.replaceChildren(...items.map(item => el('div', { class: 'block', 'data-mcp-id': item.id },
      el('b', {}, item.name), el('p', { class: 'muted' }, item.url),
      el('p', {}, item.enabled ? tr("Включено") : tr("Выключено")), el('div', { class: 'row' },
        button(tr("Изменить"), { class: 'btn' }, () => run(async () => edit(await api('GET', `/api/mcp/${item.id}`)))),
        button(tr("Проверить"), { class: 'btn' }, event => run(async () => {
          const control = event.currentTarget; control.disabled = true;
          try { const { tools } = await api('POST', `/api/mcp/${item.id}/check`); note.textContent = tr("Подключено, инструментов: {0}", tools.length); }
          finally { control.disabled = false; }
        })), button(tr("Удалить"), { class: 'btn' }, () => {
          const remove = button(tr("Удалить"), { class: 'btn btn-danger' });
          const dialog = openDialog(root, { id: 'mcp-remove', title: tr("Удалить подключение MCP?"), body: [el('p', {}, item.name)], actions: [remove] });
          remove.addEventListener('click', () => run(async () => {
            remove.disabled = true;
            try { await api('DELETE', `/api/mcp/${item.id}`); dialog.close(); await reload(); }
            finally { remove.disabled = false; }
          }));
        })))));
    if (!items.length) list.append(el('p', { class: 'muted' }, tr("Подключений MCP пока нет")));
  }
  function edit(item) {
    editor(root, item, { save: async (name, value, enabled) => {
      const { type, ...fields } = value;
      await api(item ? 'PUT' : 'POST', item ? `/api/mcp/${item.id}` : '/api/mcp', { ...fields, name, enabled }); await reload();
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

export async function chooseMcp(host, ids, save) {
  const { items } = await api('GET', '/api/mcp'); if (!host.isConnected) return;
  const choices = items.map(item => {
    const control = input(`chat-mcp-${item.id}`, 'checkbox'); control.checked = ids.includes(item.id); control.disabled = !item.enabled;
    return { control, id: item.id, node: el('label', { class: 'check' }, control, item.name) };
  });
  const note = el('p', { class: 'error', 'aria-live': 'polite' });
  const submit = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'chat-mcp-save' });
  const dialog = openDialog(host, { id: 'chat-mcp-dialog', title: tr("Инструменты MCP"), body: [el('p', {}, HINT),
    ...choices.map(c => c.node), el('a', { href: '#/settings/chat' }, tr("Настроить подключения MCP")), note], actions: [submit] });
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try { await save(choices.filter(c => c.control.checked && !c.control.disabled).map(c => c.id)); dialog.close(); }
    catch (err) { note.textContent = words(err); } finally { submit.disabled = false; }
  });
}
