import { tr } from './i18n.js';
/* MCP в диалоге (mcp_sources.py, спека 2026-11-21-studio-source-foundation-design §4 и §7a п. 2): окно
 * `#chat-mcp-dialog` с серверами учётки из `GET /api/mcp/catalog` («Обновить список» — `?refresh=1`).
 *
 * Сервер — переключатель `.chat-mcp-server`: общий включён по умолчанию, выключение пишет его в `mcp_off`; свой —
 * только вручную, в `mcp_ids`. Внутри `<details>` — инструменты с переключателями `.chat-mcp-tool`: выключенный —
 * в `mcp_tools_off`; инструмент «с подтверждением» по умолчанию выключен, включение пишет его в `mcp_tools_on`.
 * У инструмента — описание и метки «интернет» (`open_world`: только при включённом «Интернете») и «с подтверждением»;
 * правила сервера (`instructions`) — свёрнутым «Правила сервера»; недоступный сервер — с его словами.
 *
 * Состояние окна — { ids, off, toolsOff, toolsOn } (как `_chat_view` диалога: `mcp_ids` — действующий набор);
 * «Сохранить» отдаёт `save(тело PATCH)` — полной заменой. Нового диалога ещё нет: раздел держит тело черновиком
 * (`draftCount` — число серверов для чипа `MCP (n)`; черновика нет — набор по умолчанию). */
import { api } from './api.js';
import { button, el, openDialog, words } from './ui.js';

const HINT = tr("Модель сможет вызывать инструменты выбранных серверов и передавать им данные диалога.");
const MAX_SERVERS = 16;   // как mcp_client.MAX_SERVERS

/** Пустое состояние: новый диалог без черновика — общие серверы по умолчанию. */
export const DEFAULTS = Object.freeze({ ids: [], off: [], toolsOff: {}, toolsOn: {} });

/** Состояние окна из диалога сервера (`_chat_view`) или черновика (тело PATCH). */
export function stateOf(source) {
  if (!source) return DEFAULTS;
  return { ids: source.mcp_ids || [], off: source.mcp_off || [], toolsOff: source.mcp_tools_off || {},
    toolsOn: source.mcp_tools_on || {} };
}

/** Число действующих серверов черновика нового диалога (как mcp_sources.dialog_servers): явные плюс общие
 *  включённые, кроме выключенных в диалоге. `servers` — ответ `GET /api/mcp`. */
export function draftCount(draft, servers) {
  const { ids, off } = stateOf(draft);
  const defaults = servers.filter((s) => s.shared && s.enabled && !off.includes(s.id) && !ids.includes(s.id));
  return Math.min(ids.length + defaults.length, MAX_SERVERS);
}

/** Черновик из диалога сервера (первый ход не прошёл, диалог убран): общие серверы снова — по умолчанию. */
export function draftOf(view, servers) {
  const shared = new Set(servers.filter((s) => s.shared).map((s) => s.id));
  return { mcp_ids: (view.mcp_ids || []).filter((id) => !shared.has(id)), mcp_off: view.mcp_off || [],
    mcp_tools_off: view.mcp_tools_off || {}, mcp_tools_on: view.mcp_tools_on || {} };
}

const sets = (map) => new Map(Object.entries(map).map(([sid, names]) => [Number(sid), new Set(names)]));
const plain = (map) => Object.fromEntries([...map].filter(([, names]) => names.size).map(([sid, names]) => [String(sid), [...names].sort()]));

/** Окно MCP диалога; `save(body)` — Promise, отказ — словами в окне. */
export function chooseDialogMcp(host, state, save) {
  const explicit = new Set(state.ids);
  const off = new Set(state.off);
  const toolsOff = sets(state.toolsOff);
  const toolsOn = sets(state.toolsOn);
  let items = [];

  const list = el('div', { id: 'chat-mcp-servers', class: 'chat-mcp-servers' });
  const status = el('p', { class: 'muted small', id: 'chat-mcp-status', 'aria-live': 'polite' }, tr("Загружаю…"));
  const fail = el('p', { class: 'error', 'aria-live': 'polite' });
  const refresh = button(tr("Обновить список"), { class: 'btn', id: 'chat-mcp-refresh', disabled: true }, () => load(true));
  const submit = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'chat-mcp-save', disabled: true });
  const dialog = openDialog(host, { id: 'chat-mcp-dialog', title: tr("Инструменты MCP"), actions: [refresh, submit], body: [
    el('p', {}, HINT), el('p', { class: 'muted small' }, tr("Общие серверы включены по умолчанию, свои — включайте сами.")),
    status, list, el('a', { href: '#/settings/chat' }, tr("Настроить подключения MCP")), fail] });

  const named = (map, sid) => {
    if (!map.has(sid)) map.set(sid, new Set());
    return map.get(sid);
  };
  const serverOn = (s) => s.state !== 'off' && (s.shared ? !off.has(s.id) : explicit.has(s.id));
  const toolOn = (s, t) => !named(toolsOff, s.id).has(t.name) && (!t.confirm || named(toolsOn, s.id).has(t.name));

  function setServer(s, on) {
    if (s.shared) {
      if (on) off.delete(s.id);
      else off.add(s.id);
    } else if (on) explicit.add(s.id);
    else explicit.delete(s.id);
  }

  function setTool(s, t, on) {
    if (on) {
      named(toolsOff, s.id).delete(t.name);
      if (t.confirm) named(toolsOn, s.id).add(t.name);
    } else if (t.confirm) named(toolsOn, s.id).delete(t.name);
    else named(toolsOff, s.id).add(t.name);
  }

  function toolRow(s, t, summary) {
    const control = el('input', { type: 'checkbox', role: 'switch', class: 'chat-mcp-tool', 'data-tool': t.name,
      'aria-label': tr("Инструмент {0}", t.title || t.name) });
    control.checked = toolOn(s, t);
    control.disabled = !serverOn(s);
    control.addEventListener('change', () => {
      setTool(s, t, control.checked);
      summary();
    });
    return el('li', { class: 'chat-mcp-tool-row', 'data-tool': t.name },
      el('label', { class: 'check chat-mcp-tool-head' }, control, el('span', { class: 'chat-mcp-tool-name' }, t.title || t.name),
        t.open_world ? el('span', { class: 'tag chat-mcp-web', title: tr("Работает только при включённом «Интернете»") }, tr("интернет")) : null,
        t.confirm ? el('span', { class: 'tag chat-mcp-confirm', title: tr("По умолчанию выключен: включите, если доверяете ему") },
          tr("с подтверждением")) : null),
      t.title && t.title !== t.name ? el('code', { class: 'chat-mcp-tool-code' }, t.name) : null,
      t.description ? el('p', { class: 'muted small chat-mcp-tool-text' }, t.description) : null);
  }

  function serverRow(s) {
    const control = el('input', { type: 'checkbox', role: 'switch', class: 'chat-mcp-server', id: `chat-mcp-${s.id}` });
    control.checked = serverOn(s);
    control.disabled = s.state === 'off';
    const count = el('span', { class: 'chat-mcp-count' });
    const summary = () => {
      count.textContent = tr("Инструменты: {0} из {1}", s.tools.filter((t) => toolOn(s, t)).length, s.tools.length);
    };
    const tools = s.tools.map((t) => toolRow(s, t, summary));
    summary();
    control.addEventListener('change', () => {
      setServer(s, control.checked);
      for (const row of tools) row.querySelector('.chat-mcp-tool').disabled = !control.checked;
    });
    return el('section', { class: 'chat-mcp-item', 'data-sid': String(s.id) },
      el('label', { class: 'check chat-mcp-head' }, control, el('b', { class: 'chat-mcp-name' }, s.name),
        s.shared ? el('span', { class: 'tag' }, tr("общий")) : null),
      s.state === 'error' ? el('p', { class: 'error small chat-mcp-error' }, words(s.message)) : null,
      s.state === 'off' ? el('p', { class: 'muted small' }, tr("Выключен в настройках подключения")) : null,
      s.trimmed ? el('p', { class: 'muted small chat-mcp-trimmed' }, tr("Сервер предлагает больше 128 инструментов — показаны первые 128")) : null,
      s.instructions ? el('details', { class: 'chat-mcp-rules' }, el('summary', {}, tr("Правила сервера")),
        el('p', { class: 'small chat-mcp-instructions' }, s.instructions)) : null,
      tools.length ? el('details', { class: 'chat-mcp-tools' }, el('summary', {}, count), el('ul', {}, ...tools)) : null);
  }

  async function load(again = false) {
    refresh.disabled = true;
    status.textContent = tr("Загружаю…");
    try {
      const out = await api('GET', `/api/mcp/catalog${again ? '?refresh=1' : ''}`);
      if (!dialog.open) return;
      items = out.items.map((s) => ({ tools: [], ...s }));    // недоступный сервер приходит без списка инструментов
      list.replaceChildren(...items.map(serverRow));
      status.textContent = items.length ? '' : tr("Подключений MCP пока нет");
      submit.disabled = false;
    } catch (err) {
      status.textContent = words(err);
    } finally {
      refresh.disabled = false;
    }
  }

  submit.addEventListener('click', async () => {
    if (submit.disabled) return;
    const known = new Map(items.map((s) => [s.id, s]));
    // свой — в `mcp_ids`; общий там — только если сервер сейчас выключен (иначе он и так действует по умолчанию)
    const ids = [...explicit].filter((id) => known.has(id) && (!known.get(id).shared || known.get(id).state === 'off'));
    submit.disabled = true;
    fail.textContent = '';
    try {
      await save({ mcp_ids: ids, mcp_off: [...off], mcp_tools_off: plain(toolsOff), mcp_tools_on: plain(toolsOn) });
      dialog.close();
    } catch (err) {
      fail.textContent = words(err);
      submit.disabled = false;
    }
  });

  load();
  return dialog;
}
